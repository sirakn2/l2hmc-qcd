"""
gauge_model_main.py

Main method implementing the L2HMC algorithm for a 2D U(1) lattice gauge theory
with periodic boundary conditions.

Following an object oriented approach, there are separate classes responsible
for each major part of the algorithm:

    (1.) Creating the loss function to be minimized during training and
    building the corresponding TensorFlow graph.

        - This is done using the `GaugeModel` class, found in
        `models/gauge_model.py`.

        - The `GaugeModel` class depends on the `Dynamics` class
        (found in `dynamics/gauge_dynamics.py`) that performs the augmented
        leapfrog steps outlined in the original paper.

    (2.) Training the model by minimizing the loss function over both the
    target and initialization distributions.
        - This is done using the `GaugeModelTrainer` class, found in
        `trainers/gauge_model_trainer.py`.

    (3.) Running the trained sampler to generate statistics for lattice
    observables.
        - This is done using the `GaugeModelRunner` class, found in
        `runners/gauge_model_runner.py`.

Author: Sam Foreman (github: @saforem2)
Date: 04/10/2019
"""
from __future__ import absolute_import, division, print_function

import os
import time
import pickle

from collections import namedtuple

import numpy as np
import tensorflow as tf

# pylint:disable=import-error
# pylint:disable=unused-import
# pylint:disable=too-many-statements
# pylint:disable=no-name-in-module, invalid-name
# pylint:disable=redefined-outer-name
from tensorflow.python import debug as tf_debug
from tensorflow.python.client import timeline

import config as cfg
import utils.file_io as io

from config import NetWeights
from seed_dict import seeds, vnet_seeds, xnet_seeds
from runners.runner_np import _get_eps
from models.gauge_model import GaugeModel
from loggers.train_logger import TrainLogger
from utils.file_io import timeit
from utils.parse_args import parse_args
from plotters.plot_utils import plot_singular_values, weights_hist
from plotters.train_plots import plot_train_data
from trainers.trainer import Trainer
from trainers.train_setup import (check_reversibility, count_trainable_params,
                                  create_config, get_net_weights, train_setup)

if cfg.HAS_HOROVOD:
    import horovod.tensorflow as hvd

try:
    tf.logging.set_verbosity(tf.logging.INFO)
except AttributeError:
    pass

#  os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

SEP_STR = 80 * '-'  # + '\n'

NP_FLOAT = cfg.NP_FLOAT

Weights = namedtuple('Weights', ['w', 'b'])

PI = np.pi
TWO_PI = 2 * PI


# pylint:disable=too-many-statements

def log_params(params):
    io.log(SEP_STR + '\nL2HMC PARAMETERS:\n')
    for key, val in params.items():
        io.log(f' - {key} : {val}\n')
    io.log(SEP_STR)


def create_sess(**sess_kwargs):
    global_var_init = tf.global_variables_initializer()
    local_var_init = tf.local_variables_initializer()
    uninited = tf.report_uninitialized_variables()
    sess = tf.train.MonitoredTrainingSession(**sess_kwargs)
    tf.keras.backend.set_session(sess)
    sess.run([global_var_init, local_var_init])
    uninited_out = sess.run(uninited)
    io.log(f'tf.report_uninitialized_variables() len = {uninited_out}')

    return sess


def _get_global_var(name):
    try:
        var = [i for i in tf.global_variables() if name in i.name][0]
    except IndexError:
        var = None
    return var


def get_global_vars(names):
    global_vars = {name: _get_global_var(name) for name in names}
    for k, v in global_vars:
        if v is None:
            _ = global_vars.pop(k)
    return global_vars


def pkl_dump(d, pkl_file):
    """Dump `d` to `pkl_file`."""
    with open(pkl_file, 'wb') as f:
        pickle.dump(d, f)


def save_params(model):
    """Save model parameters to `.z` files.

    Additionally, write out all trainable parameters (w/ sizes) to `.txt` file.
    """

    #  dynamics_dir = os.path.join(model.log_dir, 'dynamics')
    #  io.check_else_make_dir(dynamics_dir)
    #  out_file = os.path.join(dynamics_dir, 'dynamics_params.z')
    #  io.savez(model.dynamics.params, out_file)

    out_file = os.path.join(model.log_dir, 'trainable_params.txt')
    count_trainable_params(out_file)
    io.savez(model.params, os.path.join(os.getcwd(), 'params.z'))


def save_masks(model, sess):
    """Save `model.dynamics.masks` for inference."""
    masks_file = os.path.join(model.log_dir, 'dynamics_mask.z')
    masks_file_ = os.path.join(model.log_dir, 'dynamics_mask.np')
    masks = sess.run(model.dynamics.masks)
    np.array(masks).tofile(masks_file_)
    io.log(f'dynamics.masks:\n\t {masks}')
    io.savez(masks, masks_file)


def save_seeds(model):
    """Save network seeds for reproducibility."""
    io.save_dict(seeds, out_dir=model.log_dir, name='seeds')
    io.save_dict(xnet_seeds, out_dir=model.log_dir, name='xnet_seeds')
    io.save_dict(vnet_seeds, out_dir=model.log_dir, name='vnet_seeds')


def save_weights(model, sess):
    """Save network weights to `.z` file."""
    xw_file = os.path.join(model.log_dir, 'xnet_weights.z')
    xnet_weights = model.dynamics.xnet.save_weights(sess, xw_file)

    vw_file = os.path.join(model.log_dir, 'vnet_weights.z')
    vnet_weights = model.dynamics.vnet.save_weights(sess, vw_file)
    model_weights = {
        'xnet': xnet_weights,
        'vnet': vnet_weights,
    }
    io.savez(model_weights, os.path.join(model.log_dir, 'weights.z'))


def save_eps(model, sess):
    """Save final value of `eps` (step size) at the end of training."""
    eps_np = sess.run(model.dynamics.eps)
    eps_dict = {'eps': eps_np}
    io.savez(eps_dict, os.path.join(model.log_dir, 'eps_np.z'))



@timeit
def train(FLAGS, log_file=None):
    """Train L2HMC sampler and log/plot results."""
    start_time = time.time()
    tf.keras.backend.set_learning_phase(True)

    log_dir = FLAGS.log_dir
    if log_dir is None:
        log_dir = io.create_log_dir(FLAGS,
                                    run_str=True,
                                    log_file=log_file,
                                    model_type='GaugeModel')

    checkpoint_dir = os.path.join(log_dir, 'checkpoints')
    io.check_else_make_dir(checkpoint_dir)

    #  eps = FLAGS.eps
    #  current_step = 0

    params = dict(FLAGS.__dict__)

    params['log_dir'] = log_dir
    params['summaries'] = not FLAGS.no_summaries
    params['save_steps'] = FLAGS.train_steps // 4
    params['keep_data'] = not FLAGS.clear_data

    hooks = []
    if FLAGS.horovod:
        params['using_hvd'] = True
        params['num_workers'] = hvd.size()
        hooks += [hvd.BroadcastGlobalVariablesHook(0)]

    IS_CHIEF = (
        not FLAGS.horovod
        or FLAGS.horovod and hvd.rank() == 0
    )

    # If resuming training,
    # update params w/ previous state
    if FLAGS.restore and IS_CHIEF:
        params = io.loadz(os.path.join(log_dir, 'parameters.z'))
        state_file = os.path.join(FLAGS.log_dir, 'training', 'current_state.z')
        state = io.loadz(state_file)
        #  current_step = state['step']
        #  eps = state['dynamics_eps']
        params['lr_init'] = state['lr']
        params['beta_init'] = state['beta']

    model = GaugeModel(params)

    train_logger = None
    if IS_CHIEF:
        train_logger = TrainLogger(model, log_dir, params)

    # Create `tf.ConfigProto()`
    config, params = create_config(params)

    net_weights_init = NetWeights(FLAGS.x_scale_weight,
                                  FLAGS.x_translation_weight,
                                  FLAGS.x_transformation_weight,
                                  FLAGS.v_scale_weight,
                                  FLAGS.v_translation_weight,
                                  FLAGS.v_transformation_weight)

    sess = create_sess(hooks=hooks,
                       config=config,
                       save_summaries_secs=None,
                       save_summaries_steps=None,
                       checkpoint_dir=checkpoint_dir,
                       save_checkpoint_steps=params['save_steps'])

    #  x_shape = (FLAGS.batch_size, model.lattice.num_links)
    x_init = TWO_PI * np.random.uniform(size=(FLAGS.batch_size,
                                              model.lattice.num_links)) - PI
    if FLAGS.restore:
        x_init = state['x_in']
        restore_ops = [model.global_step_setter, model.eps_setter]
        sess.run(restore_ops, feed_dict={
            model.global_step_ph: state['step'],
            model.eps_ph: state['dynamics_eps'],
        })
        try:
            train_logger.restore_train_data()
        except (AttributeError, FileNotFoundError):
            io.log(f'No training data found! Continuing...')

    trainer = Trainer(sess, model, train_logger, params)
    trainer.train(samples=x_init,
                  beta=params['beta_init'],
                  net_weights=net_weights_init)

    dataset = None
    if IS_CHIEF:
        save_masks(model, sess)
        save_params(model)
        save_seeds(model)
        save_weights(model, sess)
        save_eps(model, sess)
        plot_singular_values(model.log_dir)
        if not FLAGS.clear_data:
            dataset = plot_train_data(train_logger.train_data, params)

        train_logger.write_train_strings()
        if FLAGS.save_train_data and not FLAGS.clear_data:
            io.log(f'Saving train data!')
            train_logger.save_train_data()
        # wfile = os.path.join(model.log_dir, 'dynamics_weights.h5')
        # model.dynamics.save_weights(wfile)
        #  io.save_dict(model.params, os.path.join(os.getcwd()), 'params.z')

    # close MonitoredTrainingSession and reset the default graph
    sess.close()
    tf.compat.v1.reset_default_graph()
    io.log(f'{SEP_STR}\n training took:'
           f'{time.time()-start_time:.3g}s \n{SEP_STR}')

    return model, train_logger, dataset


@timeit
def train_l2hmc(FLAGS, log_file=None):
    """Create, train, and run L2HMC sampler on 2D U(1) gauge model."""
    start_time = time.time()
    tf.keras.backend.set_learning_phase(True)

    current_state = None
    if FLAGS.restore and FLAGS.log_dir is not None:
        params = io.load_params(FLAGS.log_dir)
        if FLAGS.horovod and params['using_hvd']:  # should be the same
            num_workers = hvd.size()
            # XXX: is this necessary???
            #  assert num_workers == params['num_workers']
            hooks = [hvd.BroadcastGlobalVariablesHook(0)]
            params['logging_steps'] *= num_workers
        else:
            hooks = []

    else:
        params, hooks = train_setup(FLAGS, log_file)

    # ---------------------------------------------------------------
    # NOTE: Conditionals required for file I/O if we're not using
    #       Horovod, `is_chief` should always be True otherwise,
    #       if using Horovod, we only want to perform file I/O
    #       on hvd.rank() == 0, so check that first.
    # ---------------------------------------------------------------
    condition1 = not params['using_hvd']
    condition2 = params['using_hvd'] and hvd.rank() == 0
    is_chief = condition1 or condition2

    save_steps = max((FLAGS.train_steps, params['train_steps'])) // 4
    params['save_steps'] = save_steps
    params['zero_masks'] = FLAGS.zero_masks
    params['print_steps'] = FLAGS.print_steps
    params['beta_fixed'] = (FLAGS.beta_final == FLAGS.beta_init)

    if is_chief:
        log_dir = params['log_dir']
        checkpoint_dir = os.path.join(log_dir, 'checkpoints/')
        io.check_else_make_dir(checkpoint_dir)
        log_params(params)
        current_state_file = os.path.join(log_dir, 'training',
                                          'current_state.z')
        if os.path.isfile(current_state_file):
            current_state = io.loadz(current_state_file)
    else:
        log_dir = None
        checkpoint_dir = None
        current_state = None

    if FLAGS.restore:
        if current_state is not None:
            params['lr_init'] = current_state['lr']
            params['eps'] = current_state['dynamics_eps']
            if FLAGS.restart_beta > 0:
                params['beta_init'] = FLAGS.restart_beta
            else:
                params['beta_init'] = current_state['beta']

        else:
            params['lr_init'] = FLAGS.lr_init
            params['beta_init'] = FLAGS.beta_init
            params['beta_final'] = FLAGS.beta_final
            params['eps'] = FLAGS.eps

        #  params['train_steps'] += FLAGS.train_steps

    # --------------------------------------------------------
    # Create model and train_logger
    # --------------------------------------------------------
    model = GaugeModel(params)

    # Only create `TrainLogger` if `hvd.rank == 0`
    if is_chief:
        keep_data = not FLAGS.clear_data
        logging_steps = params.get('logging_steps', 10)
        train_logger = TrainLogger(model, log_dir,
                                   save_steps=save_steps,
                                   logging_steps=logging_steps,
                                   keep_data=keep_data,
                                   print_steps=FLAGS.print_steps,
                                   summaries=params['summaries'])
    else:
        train_logger = None

    # -------------------------------------------------------
    # Setup `tf.ConfigProto` object for `tf.Session`
    # -------------------------------------------------------
    config, params = create_config(params)

    net_weights_init = cfg.NetWeights(
        x_scale=FLAGS.x_scale_weight,
        x_translation=FLAGS.x_translation_weight,
        x_transformation=FLAGS.x_transformation_weight,
        v_scale=FLAGS.v_scale_weight,
        v_translation=FLAGS.v_translation_weight,
        v_transformation=FLAGS.v_transformation_weight,
    )

    # ----------------------------------------------------------------
    #  Create MonitoredTrainingSession
    #
    #  NOTE: The MonitoredTrainingSession takes care of session
    #        initialization, restoring from a checkpoint, saving to a
    #        checkpoint, and closing when done or an error occurs.
    # ----------------------------------------------------------------
    #  save_steps = FLAGS.save_steps
    #  scaffold=scaffold,
    sess = create_sess(hooks=hooks,
                       config=config,
                       save_summaries_secs=None,
                       save_summaries_steps=None,
                       save_checkpoint_steps=save_steps,
                       checkpoint_dir=checkpoint_dir)
    #
    #  current_state_file = os.path.join(model.log_dir, 'training',
    #                                    'current_state.z')
    #  if os.path.isfile(current_state_file):
    #      current_state = io.loadz(current_state_file)
    #      model.lr = current_state['lr']
    #      samples_init = current_state['x_in']
    #
    #      if FLAGS.restart_beta > 0:
    #          beta_init = FLAGS.restart_beta
    #      else:
    #          beta_init = current_state['beta']
    #
    if current_state is not None:
        samples_init = current_state['x_in']
        model.beta_init = current_state['beta']

        is_finished = getattr(current_state, 'is_finished', False)
        almost_finished = (params['train_steps'] - current_state['step'] < 20)
        if is_finished or almost_finished:
            train_steps = params['train_steps'] + FLAGS.train_steps
            model.train_steps = train_steps
            params['train_steps'] = train_steps

        ops = [model.global_step_setter, model.eps_setter]
        feed_dict = {
            model.global_step_ph: current_state['step'],
            model.eps_ph: current_state['dynamics_eps'],
        }
        sess.run(ops, feed_dict=feed_dict)

    else:
        rand_unif = np.random.uniform(
            size=(FLAGS.batch_size, model.lattice.num_links)
            #  size=(model.lattice.samples_array.shape)
        )
        samples_init = 2 * np.pi * rand_unif - np.pi
        beta_init = model.beta_init

    # TODO: Can these be safely deleted???
    #  sess.run([
    #      model.dynamics.xnet.generic_net.coeff_scale.initializer,
    #      model.dynamics.vnet.generic_net.coeff_scale.initializer,
    #      model.dynamics.xnet.generic_net.coeff_transformation.initializer,
    #      model.dynamics.vnet.generic_net.coeff_transformation.initializer,
    #  ])
    if FLAGS.restore and is_chief and not FLAGS.clear_data:
        try:
            train_logger.restore_train_data()
        except FileNotFoundError:
            io.log(f'No training data found! Continuing...')

    # ----------------------------------------------------------
    #                       TRAINING
    # ----------------------------------------------------------
    trainer = Trainer(sess, model, train_logger, params)
    trainer.train(beta=beta_init,
                  samples=samples_init,
                  net_weights=net_weights_init)

    dataset = None
    if is_chief:
        save_masks(model, sess)
        save_params(model)
        save_seeds(model)
        save_weights(model, sess)
        save_eps(model, sess)
        plot_singular_values(model.log_dir)
        if not FLAGS.clear_data:
            dataset = plot_train_data(train_logger.train_data, params)

        train_logger.write_train_strings()
        if FLAGS.save_train_data and not FLAGS.clear_data:
            io.log(f'Saving train data!')
            train_logger.save_train_data()
        # wfile = os.path.join(model.log_dir, 'dynamics_weights.h5')
        # model.dynamics.save_weights(wfile)
        #  io.save_dict(model.params, os.path.join(os.getcwd()), 'params.z')

    # close MonitoredTrainingSession and reset the default graph
    sess.close()
    tf.reset_default_graph()
    io.log(f'{SEP_STR}\n training took:'
           f'{time.time()-start_time:.3g}s \n{SEP_STR}')

    return model, train_logger, dataset


@timeit
def main(FLAGS):
    """Main method for creating/training/running L2HMC for U(1) gauge model."""
    log_file = 'output_dirs.txt'

    using_hvd = getattr(FLAGS, 'horovod', False)
    if cfg.HAS_HOROVOD and using_hvd:
        io.log("INFO: USING HOROVOD")
        hvd.init()
        rank = hvd.rank()
        print(f'Setting seed from rank: {rank}')
        # multiply the global seed by the rank so each rank gets diff seed
        tf.set_random_seed(rank * seeds['global_tf'])

    #  model, _, _ = train_l2hmc(FLAGS, log_file)
    model, _, _ = train(FLAGS, log_file)


if __name__ == '__main__':
    FLAGS = parse_args()
    #  USING_HVD = getattr(FLAGS, 'horovod', False)
    if not FLAGS.horovod:
        if not tf.executing_eagerly():
            tf.commpat.v1.set_random_seed(seeds['global_tf'])
    main(FLAGS)
