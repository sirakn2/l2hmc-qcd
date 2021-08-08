# noqa: F401
# pylint: disable=unused-import,invalid-name
# pylint: disable=no-member,too-many-locals,protected-access
"""
training_utils.py

Implements helper functions for training the model.
"""
from __future__ import absolute_import, annotations, division, print_function

import json
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import tensorflow as tf
import warnings

import utils.file_io as io
import utils.live_plots as plotter
from config import PI
from dynamics.config import NET_WEIGHTS_HMC
from dynamics.gauge_dynamics import GaugeDynamics, build_dynamics
from network.config import LearningRateConfig
from utils.annealing_schedules import get_betas
from utils.attr_dict import AttrDict
from utils.data_containers import DataContainer
from utils.hvd_init import IS_CHIEF, LOCAL_RANK, RANK
from utils.learning_rate import ReduceLROnPlateau
from utils.logger import Logger, in_notebook
from utils.plotting_utils import plot_data
from utils.summary_utils import update_summaries

#  import utils.live_plots as plotter
#  from utils.live_plots import (LivePlotData, init_plots, update_joint_plots,
#  from utils.logger import Logger, in_notebook

if tf.__version__.startswith('1.'):
    TF_VERSION = 1
elif tf.__version__.startswith('2.'):
    TF_VERSION = 2

#SHOULD_TRACK = os.environ.get('TRACK', True)
#  SHOULD_TRACK = not os.environ.get('NOTRACK', False)
SHOULD_TRACK = False
PLOT_STEPS = 10

TO_KEEP = [
    'H', 'Hf', 'plaqs', 'actions', 'charges', 'sin_charges', 'dqint', 'dqsin',
    'accept_prob', 'accept_mask', 'xeps', 'veps', 'sumlogdet', 'beta', 'loss',
    'dt',
]

names = ['month', 'time', 'hour', 'minute', 'second']
formats = [
    '%Y_%m',
    '%Y-%m-%d-%H%M%S',
    '%Y-%m-%d-%H',
    '%Y-%m-%d-%H%M',
    '%Y-%m-%d-%H%M%S'
]
TSTAMPS = {
    k: io.get_timestamp(v) for k, v in dict(zip(names, formats)).items()
}

logger = Logger()

warnings.filterwarnings('once')
warnings.filterwarnings(action='once', category=UserWarning)
warnings.filterwarnings('once', 'keras')

#  try:
#      tf.config.experimental.enable_mlir_bridge()
#      tf.config.experimental.enable_mlir_graph_optimization()
#  except:  # noqa: E722
#      pass

PlotData = plotter.LivePlotData


def update_plots(history: dict, plots: dict, window: int = 1):
    lpdata = PlotData(history['loss'], plots['loss']['plot_obj1'])
    bpdata = PlotData(history['beta'], plots['loss']['plot_obj2'])
    fig_loss = plots['loss']['fig']
    id_loss = plots['loss']['display_id']
    plotter.update_joint_plots(lpdata, bpdata, fig=fig_loss,
                               display_id=id_loss)

    for key, val in history.items():
        if key in plots and key != 'loss':
            plotter.update_plot(y=val, window=window, **plots[key])


def check_if_int(x: tf.Tensor) -> tf.Tensor:
    nearest_int = tf.math.round(x)
    return tf.math.abs(x - nearest_int) < 1e-3


def train_hmc(
        configs: AttrDict,
        make_plots: bool = True,
        num_chains: int = 32,
        #  therm_frac: float = 0.33,
):
    """Main method for training HMC model."""
    hconfigs = AttrDict(dict(configs).copy())
    lr_config = AttrDict(hconfigs.pop('lr_config', None))
    config = AttrDict(hconfigs.pop('dynamics_config', None))
    net_config = AttrDict(hconfigs.pop('network_config', None))
    hconfigs.train_steps = hconfigs.pop('hmc_steps', None)
    hconfigs.beta_init = hconfigs.beta_final

    config.update({
        'hmc': True,
        'use_ncp': False,
        'aux_weight': 0.,
        'zero_init': False,
        'separate_networks': False,
        'use_conv_net': False,
        'directional_updates': False,
        'use_scattered_xnet_update': False,
        'use_tempered_traj': False,
        'gauge_eq_masks': False,
    })

    lr_config = LearningRateConfig(
        warmup_steps=0,
        decay_rate=0.9,
        decay_steps=hconfigs.train_steps // 10,
        lr_init=lr_config.get('lr_init', None),
    )
    dirs = io.setup_directories(hconfigs, 'training_hmc')
    hconfigs.update({
        'profiler': False,
        'make_summaries': True,
        'lr_config': lr_config,
        'dirs': dirs,
    })

    dynamics = GaugeDynamics(hconfigs, config, net_config, lr_config)
    dynamics.save_config(dirs['config_dir'])

    x, train_data = train_dynamics(dynamics, hconfigs, dirs=dirs)
    if IS_CHIEF and make_plots:
        output_dir = os.path.join(dirs['train_dir'], 'outputs')
        io.check_else_make_dir(output_dir)
        train_data.save_data(output_dir)

        params = {
            'eps': dynamics.eps.numpy(),
            'num_steps': dynamics.config.num_steps,
            'beta_init': train_data.data.beta[0],
            'beta_final': train_data.data.beta[-1],
            'x_shape': dynamics.config.x_shape,
            'net_weights': NET_WEIGHTS_HMC,
        }
        _ = plot_data(data_container=train_data, flags=hconfigs,
                      params=params, out_dir=dirs['train_dir'],
                      therm_frac=0.0, num_chains=num_chains)
        #  data_container = output['data_container']

    return x, dynamics, train_data, hconfigs


def random_init_from_configs(configs: dict[str, Any]) -> tf.Tensor:
    xshape = configs.get('dynamics_config', {}).get('x_shape', None)
    assert xshape is not None
    return tf.random.uniform(xshape, -PI, PI)


def load_last_training_point(logdir: Union[str, Path]) -> tf.Tensor:
    """Load previous states from `logdir`."""
    xfpath = os.path.join(logdir, 'training', 'train_data',
                          f'x_rank{RANK}-{LOCAL_RANK}.z')
    return io.loadz(xfpath)


def get_starting_point(configs: dict[str, Any]) -> tf.Tensor:
    logdir = configs.get('log_dir', configs.get('logdir', None))
    if logdir is None:
        return random_init_from_configs(configs)
    try:
        return load_last_training_point(
            configs.get('log_dir', configs.get('logdir', None))
        )
    except FileNotFoundError:
        return random_init_from_configs(configs)


def plot_models(dynamics: GaugeDynamics, logdir: Union[str, Path]):
    if dynamics.config.separate_networks:
        networks = {
            'dynamics_vnet': dynamics.vnet[0],
            'dynamics_xnet0': dynamics.xnet[0][0],
            'dynamics_xnet1': dynamics.xnet[0][1],
        }
    else:
        networks = {
            'dynamics_vnet': dynamics.vnet,
            'dynamics_xnet': dynamics.xnet,
        }

    for key, val in networks.items():
        try:
            fpath = os.path.join(logdir, f'{key}.png')
            tf.keras.utils.plot_model(val, show_shapes=True, to_file=fpath)
        except Exception as exception:
            raise exception


def load_configs_from_logdir(logdir: Union[str, Path]) -> dict[str, Any]:
    fpath = os.path.join(logdir, 'train_configs.json')
    with open(fpath, 'r') as f:
        configs = json.load(f)

    return configs


def check_if_logdir_exists(logdir: Union[str, Path] = None) -> bool:
    if logdir is None:
        return False

    exists = os.path.isdir(logdir)
    if exists:
        contents = os.listdir(logdir)
        if contents is not None and isinstance(contents, list):
            if len(contents) > 0:
                return True


def setup_directories(configs: dict[str, Any]) -> dict[str, Any]:
    """Setup directories for training."""
    logfile = os.path.join(os.getcwd(), 'log_dirs.txt')
    restore_dir = configs.get('restore_from', None)
    restored = None
    if restore_dir is not None:
        restored = load_configs_from_logdir(restore_dir)
        lf_old = restored['dynamics_config']['num_steps']
        lf_new = configs['dynamics_config']['num_steps']

        bi_old = restored['beta_init']
        bf_old = restored['beta_final']

        bi_new = configs['beta_init']
        bf_new = configs['beta_final']
        if bi_old == bi_new and bf_old == bf_new:
            configs['ensure_new'] = False
        else:
            configs['ensure_new'] = True

        configs['restored_configs'] = restored
        assert lf_old == lf_new
        new_dynamics_config = dict(configs['dynamics_config'])
        old_dynamics_config = dict(restored['dynamics_config'])
        for key, new in new_dynamics_config.items():
            old = old_dynamics_config[key]
            if new != old:
                logger.warning(
                    'Mismatch between new and restored dynamics configs: \n'
                    f'key: {key} \n'
                    f'configs["dynamics_config"][{key}] = {new} \n'
                    f'restored["dynamics_config"][{key}] = {old} \n'
                )

                logger.warning(f'Overwriting configs with restored value')
                configs['dynamics_config'][key] = old

        new_network_config = configs['network_config']
        old_network_config = restored['network_config']
        for key, new in new_network_config.items():
            old = old_network_config[key]
            if new != old:
                logger.warning(
                    'Mismatch between new and restored network configs: \n'
                    f'key: {key} \n'
                    f'configs["network_config"][{key}] = {new} \n'
                    f'restored["network_config"][{key}] = {old} \n'
                )

                logger.warning(f'Overwriting configs with restored value')
                configs['network_config'][key] = old


        configs['restored'] = True

    ensure_new = configs.get('ensure_new', False)
    logdir = configs.get('logdir', configs.get('log_dir', None))
    if logdir is not None:
        logdir_exists = os.path.isdir(logdir)
        contents = os.listdir(logdir)
        logdir_nonempty = False
        if contents is not None and isinstance(contents, list):
            if len(contents) > 0:
                logdir_nonempty = True

        if logdir_exists and logdir_nonempty and ensure_new:
            raise ValueError(
                f'Nonempty `logdir`, but `ensure_new={ensure_new}'
            )

    # Create `logdir`, `logdir/training/...`' etc
    dirs = io.setup_directories(configs, timestamps=TSTAMPS)
    configs['dirs'] = dirs
    logdir = dirs.get('logdir', dirs.get('log_dir', None))
    configs['log_dir'] = logdir
    configs['logdir'] = logdir

    if RANK == 0:
        io.save_dict(configs, logdir, name='train_configs')
        io.write(f'{logdir}', logfile, 'a')

        if restored is not None:
            io.save_dict(restored, logdir, name='restored_train_configs')

    return configs


def restore_from(restore_dir: Union[str, Path]) -> GaugeDynamics:
    """Load trained networks and restore model from checkpoint."""
    logger.warning(f'Loading networks from: {restore_dir}')
    try:
        configs_file = os.path.join(restore_dir, 'train_configs.z')
        configs = io.loadz(configs_file)
    except EOFError:
        configs_file = os.path.join(restore_dir, 'train_configs.json')
        with open(configs_file, 'r') as f:
            configs = json.load(f)
    #  configs_file = os.path.join(restore_dir, 'train_configs.z')
    #  configs = io.loadz(configs_file)
    dynamics = build_dynamics(configs)

    networks = dynamics._load_networks(str(restore_dir))
    dynamics.xnet = networks['xnet']
    dynamics.vnet = networks['vnet']

    ckptdir = os.path.join(restore_dir, 'training', 'checkpoints')
    ckpt = tf.train.Checkpoint(dynamics=dynamics, optimizer=dynamics.optimizer)
    manager = tf.train.CheckpointManager(ckpt, ckptdir, max_to_keep=5)
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        logger.warning(f'Restored from {manager.latest_checkpoint}')

    return dynamics


def setup_betas(
        bi: float,
        bf: float,
        train_steps: int,
        current_step: int,
):
    """Setup array of betas for training."""

    if bi == bf:
        betas = bi * tf.ones(train_steps)
    else:
        betas = get_betas(train_steps, bi, bf)

    #  remaining_steps = train_steps - current_step
    #  if len(betas) < remaining_steps:
    #      diff = remaining_steps - len(betas)
    #      betas = list(betas) + diff * [tf.constant(bf)]

    if current_step > 0:
        betas = betas[current_step:]

    return tf.convert_to_tensor(betas)


# TODO: Add type annotations
# pylint:disable=too-many-statements, too-many-branches
def setup(
        configs: dict,
        x: tf.Tensor = None,
        betas: list[tf.Tensor]=None
):
    """Setup training."""
    #  logdir = configs.get('logdir', configs.get('log_dir', None))
    #  ensure_new = configs.get('ensure_new', False)
    #  if logdir is not None:
    #      if os.path.isdir(logdir) and ensure_new:
    #          raise ValueError('logdir exists but `ensure_new` flag is set.')
    train_steps = configs.get('train_steps', None)  # type: int
    save_steps = configs.get('save_steps', None)    # type: int
    print_steps = configs.get('print_steps', None)  # type: int

    beta_init = configs.get('beta_init', None)      # type: float
    beta_final = configs.get('beta_final', None)    # type: float

    dirs = configs.get('dirs', None)  # type: dict[str, Any]
    logdir = dirs.get('logdir', dirs.get('log_dir', None))

    assert dirs is not None
    assert logdir is not None
    assert beta_init is not None and beta_final is not None

    train_data = DataContainer(train_steps, dirs=dirs, print_steps=print_steps)

    # Check if we want to restore from existing directory
    restore_dir = configs.get('restore_from', None)
    if restore_dir is not None:
        dynamics = restore_from(restore_dir)
        datadir = os.path.join(restore_dir, 'training', 'train_data')
        current_step = dynamics.optimizer.iterations.numpy()
        if train_steps <= current_step:
            train_steps = current_step + save_steps
        #  if train_steps > current_step:
        #      train_steps -= current_step
        #  else:
        #      train_steps = current_step + save_steps
        #  train_steps = current_step + train_steps
        x = train_data.restore(datadir, step=current_step,
                               x_shape=dynamics.x_shape,
                               rank=RANK, local_rank=LOCAL_RANK)
        train_data.steps = train_steps
        #  train_data.steps = train_steps + current_step
        #  ckpt = restored['ckpt']
        #  manager = restored['manager']

    # Otherwise, create new dynamics
    else:
        logger.warning('Starting new training run')
        logger.warning('Initializing `x` from `uniform[-pi, pi]`')
        current_step = 0
        dynamics = build_dynamics(configs)
        x = tf.random.uniform(dynamics.x_shape, minval=np.pi, maxval=np.pi)

    # Reshape x from [batch_size, Nt, Nx, Nd] --> [batch_size, Nt * Nx * Nd]
    x = tf.reshape(x, (x.shape[0], -1))

    # Create checkpoint and checkpoint manager for saving during training
    ckptdir = os.path.join(logdir, 'training', 'checkpoints')
    ckpt = tf.train.Checkpoint(dynamics=dynamics, optimizer=dynamics.optimizer)
    manager = tf.train.CheckpointManager(ckpt, ckptdir, max_to_keep=5)
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        logger.warning(f'Restored ckpt from: {manager.latest_checkpoint}')

    # Determine current training step
    #  current_step = dynamics.optimizer.iterations.numpy()
    #  if current_step >= train_steps:
    #      logger.warning(', '.join(['Current step >= train_steps',
    #                                f'current_step={current_step}',
    #                                f'train_steps={train_steps}']))
    #      #  train_steps = current_step + train_steps + save_steps
    #      #  train_data.steps = current_step + save_steps
    #      train_steps = current_step + save_steps
    #      train_data.steps = train_steps
    #      logger.warning(f'Setting train_steps={train_steps}')

    # Setup summary writer for logging metrics through tensorboard
    summdir = dirs['summary_dir']
    make_summaries = configs.get('make_summaries', True)

    steps = tf.range(current_step, train_steps, dtype=tf.int64)
    #  train_data.steps = train_steps

    betas = setup_betas(beta_init, beta_final, train_steps, current_step)

    dynamics.compile(loss=dynamics.calc_losses,
                     optimizer=dynamics.optimizer,
                     experimental_run_tf_function=False)

    _ = dynamics.apply_transition((x, tf.constant(betas[0])), training=True)

    writer = None
    if IS_CHIEF:
        plot_models(dynamics, dirs['log_dir'])
        io.savez(configs, os.path.join(dirs['log_dir'], 'train_configs.z'))
        if make_summaries and TF_VERSION == 2:
            try:
                writer = tf.summary.create_file_writer(summdir)
            except AttributeError:
                writer = None
        else:
            writer = None

    #  prof_range = (0, 0)
    # If profiling, run for 10 steps in the middle of training
    #  if configs.get('profiler', False):
    #      pstart = len(betas) // 2
    #      prof_range = (pstart, pstart + 10)

    return {
        'x': x,
        'betas': betas,
        'dynamics': dynamics,
        'dirs': dirs,
        'steps': steps,
        'writer': writer,
        'manager': manager,
        'configs': configs,
        'checkpoint': ckpt,
        'train_data': train_data,
    }


@dataclass
class TrainOutputs:
    x: tf.Tensor
    logdir: str
    configs: dict[str, Any]
    data: DataContainer
    dynamics: GaugeDynamics


def train(
        configs: dict[str, Any],
        x: tf.Tensor = None,
        num_chains: int = 32,
        make_plots: bool = True,
        therm_frac: float = 0.33,
        #  restore_x: bool = False,
        #  ensure_new: bool = False,
        #  should_track: bool = True,
) -> TrainOutputs:
    """Train model.

    Returns:
        train_outputs: Dataclass with attributes:
          - x: tf.Tensor
          - logdir: str
          - configs: dict[str, Any]
          - data: DataContainer
          - dynamics: GaugeDynamics
    """
    start = time.time()
    configs = setup_directories(configs)
    config = setup(configs, x=x)
    dynamics = config['dynamics']
    dirs = config['dirs']
    configs = config['configs']
    train_data = config['train_data']

    dynamics.save_config(dirs['config_dir'])
    # ------------------------------------
    # Train dynamics
    logger.rule('TRAINING')
    t0 = time.time()
    x, train_data = train_dynamics(dynamics, config, dirs, x=x)
    logger.rule(f'DONE TRAINING. TOOK: {time.time() - t0:.4f}')
    logger.info(f'Training took: {time.time() - t0:.4f}')
    # ------------------------------------
    #  x, train_data = train_dynamics(dynamics, configs, dirs, x=x,
    #                                 should_track=SHOULD_TRACK)

    if IS_CHIEF and make_plots:
        output_dir = os.path.join(dirs['train_dir'], 'outputs')
        train_data.save_data(output_dir, save_dataset=True)

        params = {
            'beta_init': train_data.data.beta[0],
            'beta_final': train_data.data.beta[-1],
            'x_shape': dynamics.config.x_shape,
            'num_steps': dynamics.config.num_steps,
            'net_weights': dynamics.net_weights,
        }
        t0 = time.time()
        output = plot_data(data_container=train_data, flags=configs,
                           params=params, out_dir=dirs['train_dir'],
                           therm_frac=therm_frac, num_chains=num_chains)
        #  data_container = output['data_container']
        #  data_container.plot_dataset(output['out_dir'],
        #                              num_chains=num_chains,
        #                              therm_frac=therm_frac,
        #                              ridgeplots=True)
        dt = time.time() - t0
        logger.debug(
            f'Time spent plotting: {dt}s = {dt // 60}m {(dt % 60):.4f}s'
        )

    logger.info(f'Done training model! took: {time.time() - start:.4f}s')
    io.save_dict(dict(configs), dirs['log_dir'], 'configs')

    #  return x, dynamics, train_data, configs
    return TrainOutputs(x, dirs['log_dir'], configs, train_data, dynamics)


def run_md(
        dynamics: GaugeDynamics,
        inputs: tuple[tf.Tensor, tf.Tensor],
        md_steps: int,
):
    x, beta = inputs
    logger.debug(f'Running {md_steps} MD updates!')
    for _ in range(md_steps):
        mc_states, _ = dynamics.md_update((x, beta), training=True)
        x = mc_states.out.x
    logger.debug(f'Done!')

    return x


def run_profiler(
        dynamics: GaugeDynamics,
        inputs: tuple[tf.Tensor, tf.Tensor],
        logdir: str,
        steps: int = 10
):
    logger.debug(f'Running {steps} profiling steps!')
    x, beta = inputs
    metrics = None
    for step in range(steps):
        with tf.profiler.experimental.Trace('train', step_num=step, _r=1):
            tf.profiler.experimental.start(logdir=logdir)
            x, metrics = dynamics.train_step((x, beta))

    logger.debug(f'Done!')

    return x, metrics

# pylint: disable=broad-except
# pylint: disable=too-many-arguments,too-many-statements, too-many-branches,

def train_dynamics(
        dynamics: GaugeDynamics,
        input: dict[str, Any],
        dirs: Optional[dict[str, str]] = None,
        x: Optional[tf.Tensor] = None,
        betas: Optional[tf.Tensor] = None,
        #  should_track: bool = False,
):
    """Train model."""

    configs = input['configs']
    steps = configs.get('steps', [])
    min_lr = configs.get('min_lr', 1e-5)
    patience = configs.get('patience', 10)
    save_steps = configs.get('save_steps', None)
    factor = configs.get('reduce_lr_factor', 0.5)
    print_steps = configs.get('print_steps', 1000)
    logging_steps = configs.get('logging_steps', None)
    steps_per_epoch = configs.get('steps_per_epoch', 1000)

    # -- Helper functions for training, logging, saving, etc. --------------
    def train_step(x: tf.Tensor, beta: tf.Tensor):
        start = time.time()
        x, metrics = dynamics.train_step((x, tf.constant(beta)))
        metrics.dt = time.time() - start
        return x, metrics

    def should_print(step: int) -> bool:
        return IS_CHIEF and step % print_steps == 0

    def should_log(step: int) -> bool:
        return IS_CHIEF and step % logging_steps == 0

    def should_save(step: int) -> bool:
        return step % save_steps == 0 and ckpt is not None

    betas = input.get('betas')
    if dirs is None:
        dirs = input.get('dirs')

    steps = input['steps']
    writer = input['writer']
    manager = input['manager']
    ckpt = input['checkpoint']
    train_data = input['train_data']

    assert dynamics.lr_config is not None
    warmup_steps = dynamics.lr_config.warmup_steps
    reduce_lr = ReduceLROnPlateau(monitor='loss', mode='min',
                                  warmup_steps=warmup_steps,
                                  factor=factor, min_lr=min_lr,
                                  verbose=1, patience=patience)
    reduce_lr.set_model(dynamics)

    if IS_CHIEF and writer is not None:
        writer.set_as_default()

    #  tf.compat.v1.autograph.experimental.do_not_convert(dynamics.train_step)

    # -- Try running compiled `train_step` fn otherwise run imperatively ----
    assert betas is not None and len(betas) > 0
    b0 = tf.constant(betas[0])
    xshape = dynamics._xshape
    #  xshape = tuple(dynamics.config.x_shape)
    xr = tf.random.uniform(xshape, -PI, PI)
    x = input.get('x', xr) if x is None else x

    assert x is not None
    assert b0 is not None
    assert dirs is not None
    if configs.get('profiler', False):
        #  sdir = dirs.get('summary_dir', )
        sdir = dirs['summary_dir']
        x, metrics = run_profiler(dynamics, (x, b0), logdir=sdir, steps=10)
    else:
        x, metrics = dynamics.train_step((x, b0))

    # -- Run MD update to not get stuck -----------------
    md_steps = configs.get('md_steps', 0)
    if md_steps > 0:
        b0 = tf.constant(b0, )
        x = run_md(dynamics, (x, b0), md_steps)


    # -- Final setup; create timing wrapper for `train_step` function -------
    # -- and get formatted header string to display during training. --------

    warmup_steps = dynamics.lr_config.warmup_steps
    total_steps = steps[-1].numpy()
    if len(steps) != len(betas):
        betas = betas[steps[0]:]
        #  logger.warning(f'len(steps) != len(betas) Restarting step count!')
        #  logger.warning(f'len(steps): {len(steps)}, len(betas): {len(betas)}')
        #  steps = np.arange(len(betas))

    keep = ['dt', 'loss', 'accept_prob', 'beta',
            'Hwb_start', 'Hwf_start',
            'Hwb_mid', 'Hwf_mid',
            'Hwb_end', 'Hwf_end',
            'xeps', 'veps',
            'dq', 'dq_sin',
            'plaqs', 'p4x4',
            'charges', 'sin_charges']

    #  plots = init_plots(configs, figsize=(5, 2), dpi=500)
    #  discrete_betas = np.arange(beta, 8, dtype=int)
    plots = {}
    if in_notebook():
        plots = plotter.init_plots(configs, figsize=(9, 3), dpi=125)

    # -- Training loop ----------------------------------------------------
    data_strs = []
    logdir = dirs['log_dir']
    data_dir = dirs['data_dir']
    logfile = dirs['log_file']
    assert manager is not None
    assert x is not None
    #  for idx, (step, beta) in iterable:
    #  for idx, (step, beta) in enumerate(zip(steps, betas)):
    for step, beta in zip(steps, betas):
        x, metrics = train_step(x, beta)

        # TODO: Run inference when beta hits an integer
        # >>> beta_inf = {i: False, for i in np.arange(beta_final)}
        # >>> if any(np.isclose(beta, np.array(list(beta_inf.keys())))):
        # >>>     run_inference(...)

        if (step + 1) > warmup_steps and (step + 1) % steps_per_epoch == 0:
            reduce_lr.on_epoch_end(step+1, {'loss': metrics.loss})

        # -- Save checkpoints and dump configs `x` from each rank ----------
        if should_save(step + 1):
            train_data.update(step, metrics)
            train_data.dump_configs(x, data_dir, rank=RANK,
                                    local_rank=LOCAL_RANK)
            if IS_CHIEF:
                # -- Save CheckpointManager -------------
                manager.save()
                mstr = f'Checkpoint saved to: {manager.latest_checkpoint}'
                logger.info(mstr)
                # -- Save train_data and free consumed memory --------
                train_data.save_and_flush(data_dir, logfile,
                                          rank=RANK, mode='a')
                if not dynamics.config.hmc:
                    # -- Save network weights -------------------------------
                    dynamics.save_networks(logdir)
                    logger.info(f'Networks saved to: {logdir}')

        # -- Print current training state and metrics ---------------
        if should_print(step):
            train_data.update(step, metrics)
            if step % 5000 == 0:
                pre = [f'step={step}/{total_steps}']
                keep_ = keep + ['xeps_start', 'xeps_mid', 'xeps_end',
                                'veps_start', 'veps_mid', 'veps_end']
                data_str = logger.print_metrics(metrics, pre=pre, keep=keep_)
            else:
                keep_ = ['step', 'dt', 'loss', 'accept_prob', 'beta',
                         'dq_int', 'dq_sin', 'dQint', 'dQsin', 'plaqs', 'p4x4']
                pre = [f'step={step}/{total_steps}']
                data_str = logger.print_metrics(metrics, window=50,
                                                pre=pre, keep=keep_)

            data_strs.append(data_str)

        if in_notebook() and step % PLOT_STEPS == 0 and IS_CHIEF:
            train_data.update(step, metrics)
            if len(train_data.data.keys()) == 0:
                update_plots(metrics, plots)
            else:
                update_plots(train_data.data, plots)

        # -- Update summary objects ---------------------
        if should_log(step):
            #  logger.rule('')
            #  logger.info('Updating summaries')
            train_data.update(step, metrics)
            if writer is not None:
                update_summaries(step, metrics, dynamics)
                writer.flush()

    # -- Dump config objects -------------------------------------------------
    train_data.dump_configs(x, data_dir, rank=RANK, local_rank=LOCAL_RANK)
    if IS_CHIEF:
        manager.save()
        logger.log(f'Checkpoint saved to: {manager.latest_checkpoint}')
        train_data.save_and_flush(data_dir, logfile,
                                  rank=RANK, mode='a')
        if not dynamics.config.hmc:
            try:
                dynamics.save_networks(logdir)
            except (AttributeError, TypeError):
                pass

        if writer is not None:
            writer.flush()
            writer.close()

    return x, train_data
