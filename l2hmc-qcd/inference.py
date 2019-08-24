"""
gauge_model_inference.py

Runs inference using the trained L2HMC sampler contained in a saved model.

This is done by reading in the location of the saved model from a .txt file
containing the location of the checkpoint directory.

 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  NOTE:
 ------------------------------------------------------------
   If `--plot_lf` CLI argument passed, create the
   following plots:

     * The metric distance observed between individual
       leapfrog steps and complete molecular dynamics
       updates.

     * The determinant of the Jacobian for each leapfrog
       step and the sum of the determinant of the Jacobian
       (sumlogdet) for each MD update.
 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Author: Sam Foreman (github: @saforem2)
Date: 07/08/2019
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import pickle
import memory_profiler
import tensorflow as tf
import numpy as np


import utils.file_io as io

from config import PARAMS, NP_FLOAT, HAS_HOROVOD, HAS_MATPLOTLIB
from update import set_precision
#  from main import create_config
from tensorflow.core.protobuf import rewriter_config_pb2
#  from gauge_model_main import create_config

#  from utils.parse_args import parse_args
from utils.parse_inference_args import parse_args
from models.model import GaugeModel
from loggers.summary_utils import create_summaries
from loggers.run_logger import RunLogger
from plotters.gauge_model_plotter import GaugeModelPlotter
#  from plotters.plot_utils import plot_plaq_diffs_vs_net_weights
from plotters.leapfrog_plotters import LeapfrogPlotter
from runners.runner import GaugeModelRunner

if HAS_HOROVOD:
    import horovod.tensorflow as hvd

#  if HAS_MATPLOTLIB:
#      import matplotlib.pyplot as plt

if float(tf.__version__.split('.')[0]) <= 2:
    tf.logging.set_verbosity(tf.logging.INFO)

#  import memory_profiler

SEP_STR = 80 * '-'  # + '\n'


def create_config(params):
    """Helper method for creating a tf.ConfigProto object."""
    config = tf.ConfigProto(allow_soft_placement=True)
    if params['time_size'] > 8:
        off = rewriter_config_pb2.RewriterConfig.OFF
        config_attrs = config.graph_options.rewrite_options
        config_attrs.arithmetic_optimization = off

    if params['gpu']:
        # Horovod: pin GPU to be used to process local rank 
        # (one GPU per process)
        config.gpu_options.allow_growth = True
        #  config.allow_soft_placement = True
        if HAS_HOROVOD and params['horovod']:
            config.gpu_options.visible_device_list = str(hvd.local_rank())

    if HAS_MATPLOTLIB:
        params['_plot'] = True

    if params['theta']:
        params['_plot'] = False
        io.log("Training on Theta @ ALCF...")
        params['data_format'] = 'channels_last'
        os.environ["KMP_BLOCKTIME"] = str(0)
        os.environ["KMP_AFFINITY"] = (
            "granularity=fine,verbose,compact,1,0"
        )
        # NOTE: KMP affinity taken care of by passing -cc depth to aprun call
        OMP_NUM_THREADS = 62
        config.allow_soft_placement = True
        config.intra_op_parallelism_threads = OMP_NUM_THREADS
        config.inter_op_parallelism_threads = 0

    return config, params


def parse_flags(FLAGS, log_file=None):
    """Parse command line FLAGS and create dictionary of parameters."""
    try:
        kv_pairs = FLAGS.__dict__.items()
    except AttributeError:
        kv_pairs = FLAGS.items()

    params = {}
    for key, val in kv_pairs:
        params[key] = val

    params['log_dir'] = io.create_log_dir(FLAGS, log_file=log_file)
    params['summaries'] = not FLAGS.no_summaries
    if 'no_summaries' in params:
        del params['no_summaries']

    if FLAGS.save_steps is None and FLAGS.train_steps is not None:
        params['save_steps'] = params['train_steps'] // 4

    if FLAGS.float64:
        io.log(f'INFO: Setting floating point precision to `float64`.')
        set_precision('float64')

    return params


def load_params(params_pkl_file=None, log_file=None):
    if params_pkl_file is None:
        params_pkl_file = os.path.join(os.getcwd(), 'params.pkl')

    if os.path.isfile(params_pkl_file):
        with open(params_pkl_file, 'rb') as f:
            params = pickle.load(f)
    else:
        io.log(f'INFO: Unable to locate: {params_pkl_file}.\n'
               f'INFO: Using default parameters '
               f'(PARAMS defined in `config.py`.')

        params = PARAMS.copy()
        params['log_dir'] = io.create_log_dir(params, log_file=log_file)

    return params


def set_model_weights(model, dest='rand'):
    """Randomize model weights."""
    if dest == 'rand':
        io.log('Randomizing model weights...')
    elif 'zero' in dest:
        io.log(f'Zeroing model weights...')

    xnet = model.dynamics.x_fn
    vnet = model.dynamics.v_fn

    for xblock, vblock in zip(xnet.layers, vnet.layers):
        for xlayer, vlayer in zip(xblock.layers, vblock.layers):
            try:
                print(f'xlayer.name: {xlayer.name}')
                print(f'vlayer.name: {vlayer.name}')
                kx, bx = xlayer.get_weights()
                kv, bv = vlayer.get_weights()
                if dest == 'rand':
                    kx_new = np.random.randn(*kx.shape)
                    bx_new = np.random.randn(*bx.shape)
                    kv_new = np.random.randn(*kv.shape)
                    bv_new = np.random.randn(*bv.shape)
                elif 'zero' in dest:
                    kx_new = np.zeros(kx.shape)
                    bx_new = np.zeros(bx.shape)
                    kv_new = np.zeros(kv.shape)
                    bv_new = np.zeros(bv.shape)

                xlayer.set_weights([kx_new, bx_new])
                vlayer.set_weights([kv_new, bv_new])
            except ValueError:
                print(f'Unable to set weights for: {xlayer.name}')
                print(f'Unable to set weights for: {vlayer.name}')

    return model


def log_plaq_diffs(run_logger, net_weights_arr, avg_plaq_diff_arr):
    """Log the average values of the plaquette differences.

    NOTE: If inference was performed with either the `--loop_net_weights` 
          or `--loop_transl_weights` flags passed, we want to see how the
          difference between the observed and expected value of the average
          plaquette varies with different values of the net weights, so save
          this data to `.pkl` file and  plot the results.
    """
    pd_tup = [
        (nw, md) for nw, md in zip(net_weights_arr, avg_plaq_diff_arr)
    ]
    pd_out_file = os.path.join(run_logger.log_dir, 'plaq_diffs_data.pkl')
    with open(pd_out_file, 'wb') as f:
        pickle.dump(pd_tup, f)


def log_mem_usage(run_logger, m_arr):
    """Log memory usage."""
    m_pkl_file = os.path.join(run_logger.log_dir, 'memory_usage.pkl')
    m_txt_file = os.path.join(run_logger.log_dir, 'memory_usage.txt')
    with open(m_pkl_file, 'wb') as f:
        pickle.dump(m_arr, f)
    with open(m_txt_file, 'w') as f:
        for i in m_arr:
            _ = [f.write(f'{j}\n') for j in i]


def inference_setup(kwargs):
    """Set up relevant (initial) values to use when running inference."""
    if kwargs['loop_net_weights']:  # loop over different values of [S, T, Q]
        #  net_weights_arr = np.zeros((9, 3), dtype=NP_FLOAT)
        w = np.random.randn(3) + 1.
        net_weights_arr = np.array([[0.00, 0.00, 0.00],   # set weights to 0.
                                    # -----------------
                                    [0.00, 0.00, 0.25],   # loop over Q weights
                                    [0.00, 0.00, 0.50],
                                    [0.00, 0.00, 0.75],
                                    [0.00, 0.00, 1.00],
                                    [0.00, 0.00, 2.00],
                                    # -----------------
                                    [0.00, 0.25, 0.00],   # loop over T weights
                                    [0.00, 0.50, 0.00],
                                    [0.00, 0.75, 0.00],
                                    [0.00, 1.00, 0.00],
                                    [0.00, 2.00, 0.00],
                                    # -----------------
                                    [0.25, 0.00, 0.00],   # loop over S weights
                                    [0.50, 0.00, 0.00],
                                    [0.75, 0.00, 0.00],
                                    [1.00, 0.00, 0.00],
                                    [2.00, 0.00, 0.00],
                                    # -----------------
                                    [0.00, 1.00, 1.00],   # [ , T, Q]
                                    [1.00, 0.00, 1.00],   # [S,  , Q]
                                    [1.00, 1.00, 0.00],   # [S, T,  ]
                                    # -----------------
                                    [w[0], w[1], w[2]],   # randomize weights
                                    # -----------------
                                    [1.00, 1.00, 1.00]])  # set weights to 1.

    elif kwargs['loop_transl_weights']:
        net_weights_arr = np.array([[1.0, 0.00, 1.0],
                                    [1.0, 0.10, 1.0],
                                    [1.0, 0.25, 1.0],
                                    [1.0, 0.50, 1.0],
                                    [1.0, 0.75, 1.0],
                                    [1.0, 1.00, 1.0]], dtype=NP_FLOAT)

    else:  # set [S, T, Q] = [1, 1, 1]
        net_weights_arr = np.array([[1, 1, 1]], dtype=NP_FLOAT)

    # if a value has been passed in `kwargs['beta_inference']` use it
    # otherwise, use `model.beta_final`
    beta_final = kwargs.get('beta_final', None)
    beta_inference = kwargs.get('beta_inference', None)
    beta = beta_final if beta_inference is None else beta_inference
    #  betas = [beta_final if beta_inference is None else beta_inference]

    inference_dict = {
        'net_weights_arr': net_weights_arr,
        'beta': beta,
        #  'charge_weight': kwargs.get('charge_weight', 1.),
        'run_steps': kwargs.get('run_steps', 5000),
        'plot_lf': kwargs.get('plot_lf', False),
        'loop_net_weights': kwargs['loop_net_weights'],
        'loop_transl_weights': kwargs['loop_transl_weights']
    }

    return inference_dict


def run_hmc(FLAGS, log_file=None):
    """Run inference using generic HMC."""
    condition1 = not FLAGS.horovod
    condition2 = FLAGS.horovod and hvd.rank() == 0
    is_chief = condition1 or condition2
    if not is_chief:
        return -1

    FLAGS.hmc = True
    FLAGS.log_dir = io.create_log_dir(FLAGS, log_file=log_file)

    params = parse_flags(FLAGS)
    params['hmc'] = True
    params['use_bn'] = False
    params['log_dir'] = FLAGS.log_dir
    params['loop_net_weights'] = False
    params['loop_transl_weights'] = False
    params['plot_lf'] = False

    figs_dir = os.path.join(params['log_dir'], 'figures')
    io.check_else_make_dir(figs_dir)

    io.log(SEP_STR)
    io.log('HMC PARAMETERS:')
    for key, val in params.items():
        io.log(f'  {key}: {val}')
    io.log(SEP_STR)

    config, params = create_config(params)
    tf.reset_default_graph()

    model = GaugeModel(params=params)

    sess = tf.Session(config=config)
    sess.run(tf.global_variables_initializer())

    run_ops = tf.get_collection('run_ops')
    inputs = tf.get_collection('inputs')
    run_summaries_dir = os.path.join(model.log_dir, 'summaries', 'run')
    io.check_else_make_dir(run_summaries_dir)
    # create summary objects without having to train model
    _, _ = create_summaries(model, run_summaries_dir, training=False)
    run_logger = RunLogger(params, inputs, run_ops, save_lf_data=False)
    plotter = GaugeModelPlotter(params, run_logger.figs_dir)

    inference_dict = inference_setup(params)

    # --------------------------------------
    # Create GaugeModelRunner for inference
    # --------------------------------------
    runner = GaugeModelRunner(sess, params, inputs, run_ops, run_logger)
    run_inference(inference_dict, runner, run_logger, plotter)


def inference(runner, run_logger, plotter, **kwargs):
    """Perform an inference run, if it hasn't been ran previously.

    Args:
        runner: GaugeModelRunner object, responsible for performing inference.
        run_logger: RunLogger object, responsible for running `tf.summary`
            operations and accumulating/saving run statistics.
        plotter: GaugeModelPlotter object responsible for plotting lattice
            observables from inference run.

    NOTE: If inference hasn't been run previously with the param values passed,
        return `avg_plaq_diff`, i.e. the average value of the difference
        between the expected and observed value of the average plaquette.

    Returns:
        avg_plaq_diff: If run hasn't been completed previously, else None
    """
    run_steps = kwargs.get('run_steps', 5000)
    beta = kwargs.get('beta', 5.)
    kwargs['eps'] = runner.eps

    run_str = run_logger._get_run_str(**kwargs)
    kwargs['run_str'] = run_str

    if not run_logger.existing_run(run_str):
        run_logger.reset(**kwargs)
        t0 = time.time()
        runner.run(**kwargs)
        io.log(SEP_STR)
        run_time = time.time() - t0
        io.log(SEP_STR + f'\nTook: {run_time}s to complete run.\n' + SEP_STR)
        avg_plaq_diff = plotter.plot_observables(run_logger.run_data, **kwargs)

        if kwargs.get('plot_lf', False):
            lf_plotter = LeapfrogPlotter(plotter.out_dir, run_logger)
            num_samples = runner.params.get('num_samples', 20)
            lf_plotter.make_plots(run_logger.run_dir,
                                  num_samples=num_samples)

        return avg_plaq_diff

    else:
        nw = kwargs.get('net_weights', [1., 1., 1.])
        io.log(SEP_STR)
        io.log('\n Inference has already been completed for:\n'
               f'\t net_weights: [{nw[0]}, {nw[1]}, {nw[2]}]\n'
               f'\t run_steps: {run_steps}\n'
               f'\t eps: {runner.eps}\n'
               f'\t beta: {beta}\n'
               f' Continuing...\n')
        io.log(SEP_STR)


def run_inference(runner, run_logger=None, plotter=None, **kwargs):
    """Run inference.

    Args:
        inference_dict: Dictionary containing parameters to use for inference.
        runner: GaugeModelRunner object that actually performs the inference.
        run_logger: RunLogger object that logs observables and other data
            generated during inference.
        plotter: GaugeModelPlotter object responsible for plotting observables
            generated during inference.
    """
    if plotter is None or run_logger is None:
        return

    dir_append = kwargs.get('dir_append', None)
    net_weights_arr = kwargs.get('net_weights_arr', [1., 1., 1.])
    avg_plaq_diff_arr = []
    m_arr = []
    for net_weights in net_weights_arr:
        usage0 = memory_profiler.memory_usage()
        m_arr.append(usage0)
        run_logger.clear()
        usage1 = memory_profiler.memory_usage()
        m_arr.append(usage1)
        kwargs.update({
            'net_weights': net_weights,
            'dir_append': dir_append
        })
        inference(runner, run_logger, plotter, **kwargs)

    log_mem_usage(run_logger, m_arr)
    if kwargs['loop_net_weights']:
        log_plaq_diffs(run_logger, net_weights_arr, avg_plaq_diff_arr)


def main_inference(inference_kwargs):
    """Perform inference using saved model."""
    params_file = inference_kwargs.get('params_file', None)
    params = load_params(params_file)  # load params used during training

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # NOTE: We want to restrict all communication (file I/O) to only be
    # performed on rank 0 (i.e. `is_chief`) so there are two cases:
    #    1. We're using Horovod, so we have to check hvd.rank() explicitly.
    #    2. We're not using Horovod, in which case `is_chief` is always True.
    # -----------------------------------------------------------------------
    condition1 = not params['using_hvd']
    condition2 = params['using_hvd'] and hvd.rank() == 0
    is_chief = condition1 or condition2
    if not is_chief:
        return
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    checkpoint_dir = os.path.join(params['log_dir'], 'checkpoints/')
    assert os.path.isdir(checkpoint_dir)
    checkpoint_file = tf.train.latest_checkpoint(checkpoint_dir)

    config, params = create_config(params)
    sess = tf.Session(config=config)
    saver = tf.train.import_meta_graph(f'{checkpoint_file}.meta')
    saver.restore(sess, checkpoint_file)

    run_ops = tf.get_collection('run_ops')
    inputs = tf.get_collection('inputs')

    run_logger = RunLogger(params, inputs, run_ops, save_lf_data=False)
    plotter = GaugeModelPlotter(params, run_logger.figs_dir)

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  Set up relevant values to use for inference (parsed from kwargs)
    #
    #  NOTE: We are only interested in the command line arguments that 
    #        were passed to `inference.py` (i.e. those contained in kwargs)
    inference_kwargs['loop_net_weights'] = True
    inference_dict = inference_setup(inference_kwargs)
    if inference_dict['beta'] is None:
        inference_dict['beta'] = params['beta_final']
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    net_weights_file = os.path.join(params['log_dir'], 'net_weights.txt')
    np.savetxt(net_weights_file, inference_dict['net_weights_arr'],
               delimiter=', ', newline='\n', fmt='%-.4g')

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # Create GaugeModelRunner for inference
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    runner = GaugeModelRunner(sess, params, inputs, run_ops, run_logger)
    run_inference(runner, run_logger, plotter, **inference_dict)

    ##########################################################################
    #  NOTE: THE FOLLOWING WONT WORK WHEN RESTORING FROM CHECKPOINT (FOR
    #        INFERENCE) UNLESS `GaugeModel` IS ENTIRELY REBUILT:
    # ------------------------------------------------------------------------
    # set 'net_weights_arr' = [1., 1., 1.] so each Q, S, T contribute
    #  inference_dict['net_weights_arr'] = np.array([[1, 1, 1]],
    #                                               dtype=NP_FLOAT)
    # set 'betas' to be a single value
    #  inference_dict['betas'] = inference_dict['betas'][-1]
    #
    #
    #  # randomize the model weights and run inference using these weights
    #  runner.model = set_model_weights(runner.model, dest='rand')
    #  run_inference(inference_dict,
    #                runner, run_logger,
    #                plotter, dir_append='_rand')
    #
    #  #  zero the model weights and run inference using these weights
    #  runner.model = set_model_weights(runner.model, dest='zero')
    #  run_inference(inference_dict,
    #                runner, run_logger,
    #                plotter, dir_append='_zero')
    ##########################################################################


if __name__ == '__main__':
    FLAGS = parse_args()

    t0 = time.time()
    log_file = 'output_dirs.txt'
    kwargs = FLAGS.__dict__

    main_inference(kwargs)

    io.log('\n\n' + SEP_STR)
    io.log(f'Time to complete: {time.time() - t0:.4g}')
