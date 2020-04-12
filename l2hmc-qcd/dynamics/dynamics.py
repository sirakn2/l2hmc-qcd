"""
dynamics.py

Dynamics engine for L2HMC sampler on Lattice Gauge Models.

Reference [Generalizing Hamiltonian Monte Carlo with Neural
Networks](https://arxiv.org/pdf/1711.09268.pdf)

Code adapted from the released TensorFlow graph implementation by original
authors https://github.com/brain-research/l2hmc.

Reference [Robust Parameter Estimation with a Neural Network Enhanced
Hamiltonian Markov Chain Monte Carlo Sampler]
https://infoscience.epfl.ch/record/264887/files/robust_parameter_estimation.pdf

Author: Sam Foreman (github: @saforem2)
Date: 1/14/2019
"""
from __future__ import absolute_import, division, print_function

# pylint:disable=invalid-name,too-many-locals,too-many-arguments
import numpy as np
import tensorflow as tf

import config as cfg

from seed_dict import seeds, vnet_seeds, xnet_seeds
from network.network import FullNet
from network.encoder_net import EncoderNet
from network.cartesian_net import CartesianNet
from network.gauge_network import GaugeNetwork

__all__ = ['Dynamics']

TF_FLOAT = cfg.TF_FLOAT
NP_FLOAT = cfg.NP_FLOAT
TF_INT = cfg.TF_INT
State = cfg.State  # namedtuple object containing `(x, v, beta)`


def exp(x, name=None):
    """Safe exponential using tf.check_numerics."""
    return tf.check_numerics(tf.exp(x), f'{name} is NaN')


def _add_to_collection(collection, ops):
    if len(ops) > 1:
        _ = [tf.add_to_collection(collection, op) for op in ops]
    else:
        tf.add_to_collection(collection, ops)


def cast_float(x, dtype=NP_FLOAT):
    """Cast `x` to `dtype`."""
    if dtype == np.float64:
        return np.float64(x)
    if dtype == np.float32:
        return np.float32(x)
    return np.float(x)


def encode(x):
    return tf.convert_to_tensor([tf.cos(x), tf.sin(x)])


def decode(x):
    return tf.atan2(x[1], x[0])


# pylint:disable=too-many-instance-attributes
class Dynamics(tf.keras.Model):
    """Dynamics engine of naive L2HMC sampler."""
    def __init__(self, potential_fn, **params):
        """Initialization.
        Args:
            potential_fn (callable): Function specifying minus log-likelihood
                objective (that describes the target distribution) to be
                minimized.
        Params:
            x_dim (int): Dimensionality of target distribution.
            num_steps (int): Number of leapfrog steps (trajectory
                length) to use in the molecular dynamics (MD)
                integration.
            eps (float): Initial (trainable) step size to use in the MD
                integrator.
            network_arch (str): Network architecture to use. Must be
                one of `conv2D`, `conv3D`, `generic`.
            hmc (bool, optional): Flag indicating whether generic HMC should be
                performed instead of the L2HMC algorithm.
            eps_trainable (bool, optional): Flag indicating whether the step
                size `eps` should be a trainable parameter. Defaults to True.
        """
        super(Dynamics, self).__init__(name='Dynamics')
        np.random.seed(seeds['global_np'])
        self.potential = potential_fn
        self._params = params

        # create attributes from `params.items()`
        for key, val in params.items():
            if key != 'eps':  # want to use self.eps as tf.Variable
                setattr(self, key, val)

        self._model_type = params.get('model_type', None)
        self.x_dim = params.get('x_dim', None)
        self.batch_size = params.get('batch_size', None)
        self._input_shape = (self.batch_size, self.x_dim)
        self.num_filters = params.get('num_filters', None)  # n conv. filters
        self.hmc = params.get('hmc', False)           # use HMC sampler
        eps_np = params.get('eps', 0.4)
        self._eps_np = cast_float(eps_np, NP_FLOAT)   # initial step size
        self._eps_trainable = params.get('eps_trainable', True)
        self.use_bn = params.get('use_bn', False)     # use batch normalization
        self.num_steps = params.get('num_steps', 5)   # number of lf steps
        self.num_hidden1 = params.get('num_hidden1', 100)   # nodes in h1
        self.num_hidden2 = params.get('num_hidden2', 100)   # nodes in h2
        self.dropout_prob = params.get('dropout_prob', 0.)  # dropout prob
        self.network_arch = params.get('network_arch', 'generic')  # net arch

        self._network_type = params.get('net_type', 'CartesianNet')

        activation = params.get('activation', 'relu')
        if activation == 'tanh':
            self._activation_fn = tf.nn.tanh
        else:
            self._activation_fn = tf.nn.relu

        self.eps = self._build_eps(use_log=False)
        # build binary masks for updating x
        self.zero_masks = params.get('zero_masks', False)   # all 0 binary mask
        if self.zero_masks:
            self.masks = self._build_zero_masks()
        else:
            self.masks = self._build_masks()

        net_params = self._network_setup()
        self.xnet, self.vnet = self.build_network(net_params)

    def __call__(self, *args, **kwargs):
        """Call method."""
        return self.apply_transition(*args, **kwargs)

    def _build_eps(self, use_log=False):
        """Create `self.eps` (i.e. the step size) as a `tf.Variable`.
        Args:
            use_log (bool): If True, initialize `log_eps` as the actual
                `tf.Variable` and set `self.eps = tf.exp(log_eps)`; otherwise,
                set `self.eps` as a `tf.Variable directly.
        Returns:
            eps: The (trainable) step size to be used in the L2HMC algorithm.
        """
        with tf.name_scope('build_eps'):
            if use_log:
                log_eps = tf.Variable(
                    initial_value=tf.log(tf.constant(self._eps_np)),
                    name='log_eps',
                    dtype=TF_FLOAT,
                    trainable=self._eps_trainable
                )

                eps = tf.exp(log_eps, name='eps')

            else:
                eps = tf.Variable(
                    initial_value=tf.constant(self._eps_np),
                    name='eps',
                    dtype=TF_FLOAT,
                    trainable=self._eps_trainable
                )

        return eps

    def _network_setup(self):
        """Collect parameters needed to build neural network."""
        if self.hmc:
            return {}

        net_params = {
            'network_arch': self.network_arch,  # network architecture
            'use_bn': self.use_bn,              # use batch normalization
            'dropout_prob': self.dropout_prob,  # dropout only used if > 0
            'x_dim': self.x_dim,                # dim of target distribution
            'num_hidden1': self.num_hidden1,    # num. nodes in hidden layer 1
            'num_hidden2': self.num_hidden2,    # num. nodes in hidden layer 2
            #  'generic_activation': tf.nn.relu,   # activation fn
            'generic_activation': tf.nn.tanh,   # activation fn
            '_input_shape': self._input_shape,  # input shape (b4 reshaping)
        }

        network_arch = str(self.network_arch).lower()
        if network_arch in ['conv3d', 'conv2d']:
            if network_arch == 'conv2d':
                filter_sizes = [(3, 3), (2, 2)]  # size of conv. filters
            elif network_arch == 'conv3d':
                filter_sizes = [(3, 3, 1), (2, 2, 1)]

            num_filters = int(self.num_filters)  # num. filters in conv layers
            net_params.update({
                'num_filters': [num_filters, int(2 * num_filters)],
                'filter_sizes': filter_sizes
            })

        return net_params

    def build_network(self, net_params):
        """Build neural network used to train model."""
        if self.hmc:
            xnet = lambda inputs, is_training: [  # noqa: E731
                tf.zeros_like(inputs[0]) for _ in range(3)
            ]
            vnet = lambda inputs, is_training: [  # noqa: E731
                tf.zeros_like(inputs[0]) for _ in range(3)
            ]

        else:
            #  if 'gauge' in self._network_type.lower():
            xnet = GaugeNetwork(name='XNet',
                                factor=2.,
                                x_dim=self.x_dim,
                                net_seeds=xnet_seeds,
                                num_hidden1=self.num_hidden1,
                                num_hidden2=self.num_hidden2,
                                activation=self._activation_fn)

            vnet = GaugeNetwork(name='VNet',
                                factor=1.,
                                x_dim=self.x_dim,
                                net_seeds=vnet_seeds,
                                num_hidden1=self.num_hidden1,
                                num_hidden2=self.num_hidden2,
                                activation=self._activation_fn)

            #  if 'cartesian' in self._network_type.lower():
            #      xnet = CartesianNet(name='XNet',
            #                          factor=2.,
            #                          x_dim=self.x_dim,
            #                          net_seeds=xnet_seeds,
            #                          num_hidden1=self.num_hidden1,
            #                          num_hidden2=self.num_hidden2,
            #                          activation=self._activation_fn)
            #
            #      vnet = CartesianNet(name='VNet',
            #                          factor=1.,
            #                          x_dim=self.x_dim,
            #                          net_seeds=vnet_seeds,
            #                          num_hidden1=self.num_hidden1,
            #                          num_hidden2=self.num_hidden2,
            #                          activation=self._activation_fn)
            #  else:
            #      net_params['factor'] = 2.
            #      net_params['net_name'] = 'x'
            #      net_params['net_seeds'] = xnet_seeds
            #      xnet = FullNet(model_name='XNet', **net_params)
            #
            #      net_params['factor'] = 1.
            #      net_params['net_name'] = 'v'
            #      net_params['net_seeds'] = vnet_seeds
            #      vnet = FullNet(model_name='VNet', **net_params)

        return xnet, vnet

    def apply_transition_direction(self,
                                   x_init,
                                   beta,
                                   weights,
                                   is_training,
                                   model_type=None,
                                   hmc=False):
        """Propose a new state and perform the accept/reject step."""
        with tf.name_scope('apply_transition'):
            #  forward = (tf.random_uniform(shape=(), dtype=TF_FLOAT) > 0.5)
            if model_type == 'GaugeModel':
                x_init = tf.mod(x_init, 2 * np.pi)

            v_init = tf.random_normal(tf.shape(x_init),
                                      dtype=TF_FLOAT,
                                      name='v_init')

            state_init = cfg.State(x_init, v_init, beta)
            out = self.transition_kernel(*state_init, weights,
                                         is_training, hmc=hmc)
            x_prop = out['x_proposed']
            v_prop = out['v_proposed']
            px = out['accept_prob']
            px_hmc = out['accept_prob_hmc']
            sumlogdet_prop = out['sumlogdet']

            if model_type == 'GaugeModel':
                x_prop = tf.mod(x_prop, 2 * np.pi)

            # Accept or reject step
            with tf.name_scope('accept_reject_step'):
                # accept_mask, reject_mask
                mask_a, mask_r = self._get_accept_masks(px)

                # State (x, v)  after accept / reject step
                x_out = x_prop * mask_a[:, None] + x_init * mask_r[:, None]
                v_out = v_prop * mask_a[:, None] + v_init * mask_r[:, None]
                sumlogdet_out = sumlogdet_prop * mask_a

        outputs = {
            'x_init': x_init,
            'v_init': v_init,
            'x_proposed': x_prop,
            'v_proposed': v_prop,
            'x_out': x_out,
            'v_out': v_out,
            #  'direction': forward,
            #  'xf': xf,
            #  'xb': xb,
            'accept_prob': px,
            'accept_prob_hmc': px_hmc,
            'sumlogdet_proposed': sumlogdet_prop,
            'sumlogdet_out': sumlogdet_out,
        }

        return outputs

    def apply_transition(self,
                         x_init,
                         beta,
                         weights,
                         is_training,
                         hmc=True):
        """Propose a new state and perform the accept/reject step.
        We simulate the (molecular) dynamics update both forward and backward,
        and use sampled masks to compute the actual solutions.
        Args:
            x_init (tf.placeholder): Batch of (x) samples
                (GaugeLattice.samples).
            beta (tf.placeholder): Inverse coupling constant.
            net_weights: Array of scaling weights to multiply each of the
                output functions (scale, translation, transformation).
            is_training (tf.placeholder): Boolean tf.placeholder used to
                indicate if the model is currently being trained.
        Returns:
            outputs (dict): Containing
             - `outputs_fb`: The outputs from running the dynamics both
               forward and backward and performing the subsequent
               accept/reject step.
             - `energies`: Dictionary of each of the energies computed at the
               beginning and end of the trajectory
        NOTE: In the code below, `proposed` refers to a variable at the end of
        a particular MD trajectory, prior to performing the Metropolis/Hastings
        accept reject step. Consequently, `out` refers to the result after the
        accept/reject.
        """
        if self._model_type == 'GaugeModel':
            x_init = tf.mod(x_init, 2 * np.pi, name='x_in_mod_2_pi')

        # Call `self.transition_kernel` in the forward direction,
        # starting from the initial `State`: `(x_init, v_init_f, beta)`
        # to get the proposed `State`
        with tf.name_scope('apply_transition'):
            with tf.name_scope('forward'):
                vf_init = tf.random_normal(tf.shape(x_init),
                                           dtype=TF_FLOAT,
                                           seed=seeds['vf_init'],
                                           name='vf_init')


                state_init_f = cfg.State(x_init, vf_init, beta)
                outf = self.transition_kernel(*state_init_f,
                                              weights, is_training,
                                              forward=True, hmc=hmc)
                xf = outf['x_proposed']
                vf = outf['v_proposed']
                pxf = outf['accept_prob']
                pxf_hmc = outf['accept_prob_hmc']
                sumlogdetf = outf['sumlogdet']

            with tf.name_scope('backward'):
                vb_init = tf.random_normal(tf.shape(x_init),
                                           dtype=TF_FLOAT,
                                           seed=seeds['vb_init'],
                                           name='vb_init')

                state_init_b = cfg.State(x_init, vb_init, beta)
                outb = self.transition_kernel(*state_init_b,
                                              weights, is_training,
                                              forward=False, hmc=hmc)
                xb = outb['x_proposed']
                vb = outb['v_proposed']
                pxb = outb['accept_prob']
                pxb_hmc = outb['accept_prob_hmc']
                sumlogdetb = outb['sumlogdet']

            # Decide direction uniformly
            with tf.name_scope('combined'):
                mask_f, mask_b = self._get_direction_masks()

                # Use forward/backward mask to reconstruct `v_init`
                v_init = (vf_init * mask_f[:, None]
                          + vb_init * mask_b[:, None])

                # Obtain proposed states
                x_proposed = xf * mask_f[:, None] + xb * mask_b[:, None]
                v_proposed = vf * mask_f[:, None] + vb * mask_b[:, None]
                sumlogdet_proposed = sumlogdetf * mask_f + sumlogdetb * mask_b

                # Probability of accepting proposed states
                accept_prob = pxf * mask_f + pxb * mask_b

                # -------------------------------------------------------------
                # NOTE: `accept_prob_hmc` is the probability of accepting the
                # proposed states if `sumlogdet = 0`, and can be ignored unless
                # using the NNEHMC loss function from the 'Robust Parameter
                # Estimation...' paper (see line 10 for a link)
                # -------------------------------------------------------------
                accept_prob_hmc = pxf_hmc * mask_f + pxb_hmc * mask_b  # (!)

            # Accept or reject step
            with tf.name_scope('accept_reject_step'):
                # accept_mask, reject_mask
                mask_a, mask_r = self._get_accept_masks(accept_prob)

                # State (x, v)  after accept / reject step
                x_out = x_proposed * mask_a[:, None] + x_init * mask_r[:, None]
                v_out = v_proposed * mask_a[:, None] + v_init * mask_r[:, None]
                sumlogdet_out = sumlogdet_proposed * mask_a

        outputs = {
            'x_init': x_init,
            'v_init': v_init,
            'x_proposed': x_proposed,
            'v_proposed': v_proposed,
            'x_out': x_out,
            'v_out': v_out,
            'xf': xf,
            'xb': xb,
            'accept_prob': accept_prob,
            'accept_prob_hmc': accept_prob_hmc,
            'sumlogdet_proposed': sumlogdet_proposed,
            'sumlogdet_out': sumlogdet_out,
        }

        return outputs

    def transition_kernel(self,
                          x_in,
                          v_in,
                          beta,
                          weights,
                          is_training,
                          forward=True,
                          hmc=True):
        """Transition kernel of augmented leapfrog integrator."""
        #  forward = tf.cast(forward, tf.bool)
        lf_fn = self._forward_lf if forward else self._backward_lf
        #  rn = tf.random_uniform(shape=(), dtype=TF_FLOAT)
        #  lf_fn = tf.cond(rn < 0.5, self._forward_lf, self._backward_lf)
        with tf.name_scope('transition_kernel'):
            x_proposed, v_proposed = x_in, v_in
            #  if x_proposed.shape[0] != 2:
            #      x_proposed = encode(x_proposed)
            #  if v_proposed.shape[0] != 2:
            #      v_proposed = encode(v_proposed)
            #
            step = tf.constant(0., name='md_step', dtype=TF_FLOAT)
            #  batch_size = tf.shape(x_in)[0]
            logdet = tf.zeros((self.batch_size,), dtype=TF_FLOAT)

            def body(step, x, v, logdet):
                new_x, new_v, j, = lf_fn(x, v, beta, step,
                                         weights, is_training)
                return (step+1, new_x, new_v, logdet+j)

            # pylint: disable=unused-argument
            def cond(step, *args):
                return tf.less(step, self.num_steps)

            outputs = tf.while_loop(
                cond=cond,
                body=body,
                loop_vars=[step, x_proposed, v_proposed, logdet]
            )

            step = outputs[0]
            x_proposed = outputs[1]
            v_proposed = outputs[2]
            sumlogdet = outputs[3]

            accept_prob, accept_prob_hmc = self._compute_accept_prob(
                x_in, v_in, x_proposed, v_proposed, sumlogdet, beta, hmc=hmc
            )

        outputs = {
            'x_init': x_in,
            'v_init': v_in,
            'x_proposed': x_proposed,
            'v_proposed': v_proposed,
            'sumlogdet': sumlogdet,
            'accept_prob': accept_prob,
            'accept_prob_hmc': accept_prob_hmc,
        }

        return outputs

    def _forward_lf(self, x, v, beta, step, net_weights, is_training):
        """One forward augmented leapfrog step."""
        with tf.name_scope('forward_lf'):
            t = self._get_time(step, tile=tf.shape(x)[0])
            #  t = self._get_time(step, tile=tf.shape(x)[1])
            mask, mask_inv = self._get_mask(step)

            sumlogdet = 0.
            v, logdet = self._update_v_forward(x, v, beta, t,
                                               net_weights,
                                               is_training)
            sumlogdet += logdet

            x, logdet = self._update_x_forward(x, v, t,
                                               net_weights,
                                               is_training,
                                               (mask, mask_inv))
            sumlogdet += logdet

            x, logdet = self._update_x_forward(x, v, t,
                                               net_weights,
                                               is_training,
                                               (mask_inv, mask))
            sumlogdet += logdet

            v, logdet = self._update_v_forward(x, v, beta, t,
                                               net_weights,
                                               is_training)
            sumlogdet += logdet

        return x, v, sumlogdet

    def _backward_lf(self, x, v, beta, step, net_weights, is_training):
        """One backward augmented leapfrog step."""
        with tf.name_scope('backward_lf'):
            step_r = self.num_steps - step - 1
            #  t = self._get_time(step_r, tile=tf.shape(x)[0])
            t = self._get_time(step_r, tile=tf.shape(x)[0])
            mask, mask_inv = self._get_mask(step_r)

            sumlogdet = 0.

            v, logdet = self._update_v_backward(x, v, beta, t,
                                                net_weights,
                                                is_training)
            sumlogdet += logdet

            x, logdet = self._update_x_backward(x, v, t,
                                                net_weights,
                                                is_training,
                                                (mask_inv, mask))
            sumlogdet += logdet

            x, logdet = self._update_x_backward(x, v, t,
                                                net_weights,
                                                is_training,
                                                (mask, mask_inv))
            sumlogdet += logdet

            v, logdet = self._update_v_backward(x, v, beta, t,
                                                net_weights,
                                                is_training)
            sumlogdet += logdet

        return x, v, sumlogdet

    def _update_v_forward(self, x, v, beta, t, net_weights, is_training):
        """Update v in the forward leapfrog step.
        Args:
            x: input position tensor
            v: input momentum tensor
            beta: inverse coupling constant
            t: current leapfrog step
            net_weights: Placeholders for the multiplicative weights by which
                to multiply the S, Q, and T functions (scale, transformation,
                translation resp.)
        Returns:
            v: Updated (output) momentum
            logdet: Jacobian factor
        NOTE: The momentum update in the forwared direction takes v to v' via
                v' = v * exp(0.5*eps*Sv) - 0.5*eps*[grad*exp(eps*Qv) + Tv]
        """
        with tf.name_scope('update_vf'):
            if self._model_type == 'GaugeModel':
                x = tf.mod(x, 2 * np.pi)

            grad = self.grad_potential(x, beta)

            Sv, Tv, Qv = self.vnet([x, grad, t], is_training)

            transl = net_weights.v_translation * Tv
            scale = net_weights.v_scale * (0.5 * self.eps * Sv)
            transf = net_weights.v_transformation * (self.eps * Qv)

            exp_scale = tf.exp(scale, name='exp_scale_vf')
            exp_transf = tf.exp(transf, name='exp_transf_vf')

            vf = v * exp_scale - 0.5 * self.eps * (grad * exp_transf + transl)
            logdet_vf = tf.reduce_sum(scale, axis=-1, name='logdet_vf')

        return vf, logdet_vf

    def _update_x_forward(self, x, v, t, net_weights, is_training, masks):
        """Update x in the forward leapfrog step."""
        with tf.name_scope('update_xf'):
            mask, mask_inv = masks
            if self._model_type == 'GaugeModel':
                x = tf.mod(x, 2 * np.pi)

            Sx, Tx, Qx = self.xnet([v, mask * x, t], is_training)

            transl = net_weights.x_translation * Tx
            scale = net_weights.x_scale * (self.eps * Sx)
            transf = net_weights.x_transformation * (self.eps * Qx)

            y = x * tf.exp(scale) + self.eps * (v * tf.exp(transf) + transl)
            xf = mask * x + mask_inv * y

            logdet_xf = tf.reduce_sum(mask_inv * scale,
                                      axis=-1, name='logdet_xf')

        return xf, logdet_xf

    def _update_v_backward(self, x, v, beta, t, net_weights, is_training):
        """Update v in the backward leapfrog step. Invert the forward update"""
        with tf.name_scope('update_vb'):
            if self._model_type == 'GaugeModel':
                x = tf.mod(x, 2 * np.pi)

            grad = self.grad_potential(x, beta)

            Sv, Tv, Qv = self.vnet([x, grad, t], is_training)

            transl = net_weights.v_translation * Tv
            scale = net_weights.v_scale * (-0.5 * self.eps * Sv)
            transf = net_weights.v_transformation * (self.eps * Qv)

            exp_scale = tf.exp(scale, name='exp_scale_vb')
            exp_transf = tf.exp(transf, name='exp_transf_vb')

            half_eps = 0.5 * self.eps
            vb = exp_scale * (v + half_eps * (grad * exp_transf + transl))

            logdet = tf.reduce_sum(scale, axis=-1, name='logdet_vb')

        return vb, logdet

    def _update_x_backward(self, x, v, t, net_weights, is_training, masks):
        """Update x in the backward lf step. Inverting the forward update."""
        mask, mask_inv = masks
        with tf.name_scope('update_xb'):
            if self._model_type == 'GaugeModel':
                x = tf.mod(x, 2 * np.pi)

            Sx, Tx, Qx = self.xnet([v, mask * x, t], is_training)

            scale = net_weights.x_scale * (-self.eps * Sx)
            transl = net_weights.x_translation * Tx
            transf = net_weights.x_transformation * (self.eps * Qx)

            exp_scale = tf.exp(scale, name='exp_scale_xb')
            exp_transf = tf.exp(transf, name='exp_transf_xb')
            y = exp_scale * (x - self.eps * (v * exp_transf + transl))
            xb = mask * x + mask_inv * y

            logdet = tf.reduce_sum(mask_inv * scale, axis=-1, name='logdet_xb')

        return xb, logdet

    def _compute_accept_prob(self, xi, vi, xf, vf, sumlogdet, beta, hmc=True):
        """Compute the prob of accepting the proposed state given old state.
        Args:
            xi: Initial state.
            vi: Initial v.
            xf: Proposed state.
            vf: Proposed v.
            sumlogdet: Sum of the terms of the log of the determinant.
                (Eq. 14 of original paper).
            beta: Inverse coupling constant of gauge model.
        """
        with tf.name_scope('accept_prob'):
            with tf.name_scope('old_hamiltonian'):
                h_init = self.hamiltonian(xi, vi, beta)  # initial H
            with tf.name_scope('new_hamiltonian'):
                h_proposed = self.hamiltonian(xf, vf, beta)

            with tf.name_scope('calc_prob'):
                dh = h_init - h_proposed + sumlogdet
                prob = tf.exp(tf.minimum(dh, 0.))

                # Ensure numerical stability as well as correct gradients
                accept_prob = tf.where(tf.is_finite(prob),
                                       prob, tf.zeros_like(prob))
            if hmc:
                prob_hmc = self._compute_accept_prob_hmc(h_init,
                                                         h_proposed, beta)
            else:
                prob_hmc = tf.zeros_like(accept_prob)

        return accept_prob, prob_hmc

    @staticmethod
    def _compute_accept_prob_hmc(h_init, h_proposed, beta):
        """Compute the prob. of accepting the proposed state given old state.
        NOTE: This computes the accept prob. for generic HMC.
        """
        with tf.name_scope('accept_prob_nnehmc'):
            prob = tf.exp(tf.minimum(beta * (h_init - h_proposed), 0.))
            accept_prob = tf.where(tf.is_finite(prob),
                                   prob, tf.zeros_like(prob))

        return accept_prob

    def _get_time(self, i, tile=1):
        """Format time as [cos(..), sin(...)]."""
        with tf.name_scope('get_time'):
            trig_t = tf.squeeze([
                tf.cos(2 * np.pi * i / self.num_steps),
                tf.sin(2 * np.pi * i / self.num_steps),
            ])

            t = tf.tile(tf.expand_dims(trig_t, 0), (tile, 1))

        return t

    def _get_direction_masks(self):
        with tf.name_scope('direction_masks'):
            with tf.name_scope('forward_mask'):
                forward_mask = tf.cast(
                    tf.random_uniform((self.batch_size,),
                                      dtype=TF_FLOAT,
                                      seed=seeds['mask_f']) > 0.5,
                    TF_FLOAT,
                    name='forward_mask'
                )
            with tf.name_scope('backward_mask'):
                backward_mask = 1. - forward_mask

        return forward_mask, backward_mask

    @staticmethod
    def _get_accept_masks(accept_prob):
        with tf.name_scope('accept_masks'):
            accept_mask = tf.cast(
                accept_prob > tf.random_uniform(tf.shape(accept_prob),
                                                dtype=TF_FLOAT,
                                                seed=seeds['mask_a']),
                TF_FLOAT,
                name='acccept_mask'
            )
            reject_mask = 1. - accept_mask

        return accept_mask, reject_mask

    def _build_zero_masks(self):
        masks = []
        for _ in range(self.num_steps):
            mask = tf.constant(np.zeros((self.x_dim,)), dtype=TF_FLOAT)
            masks.append(mask[None, :])

        return masks

    def _build_masks(self):
        """Construct different binary masks for different time steps.
        Args:
            all_zeros (bool): If set to True, create a mask with all entries
                equal to zero instead of half zeros, half ones.
        """
        with tf.name_scope('x_masks'):
            #  if self.zero_masks:
            #      masks = self._build_zero_masks()
            #  else:
            masks = []
            for _ in range(self.num_steps):
                # Need to use npr here because tf would generate different
                # random values across different `sess.run`
                _idx = np.arange(self.x_dim)
                idx = np.random.permutation(_idx)[:self.x_dim // 2]
                mask = np.zeros((self.x_dim,))
                mask[idx] = 1.
                mask = tf.constant(mask, dtype=TF_FLOAT)
                masks.append(mask[None, :])

        return masks

    def _get_mask(self, step):
        """Retrieve the binary mask associated with the time step `step`."""
        with tf.name_scope('get_mask'):
            if tf.executing_eagerly():
                m = self.masks[step]
            else:
                m = tf.gather(self.masks, tf.cast(step, dtype=TF_INT))

        return m, 1. - m

    def grad_potential(self, x, beta):
        """Get gradient of potential function at current location."""
        with tf.name_scope('grad_potential'):
            if tf.executing_eagerly():
                tfe = tf.contrib.eager
                grad = tfe.gradients_function(self.potential_energy,
                                              params=[0])(x, beta)[0]
            else:
                grad = tf.gradients(self.potential_energy(x, beta), x)[0]

        return grad

    def potential_energy(self, x, beta):
        """Compute potential energy using `self.potential` and beta."""
        with tf.name_scope('potential_energy'):
            potential_energy = beta * self.potential(x)

        return potential_energy

    @staticmethod
    def kinetic_energy(v):
        """Compute the kinetic energy."""
        with tf.name_scope('kinetic_energy'):
            kinetic_energy = 0.5 * tf.reduce_sum(v**2, axis=-1)

        return kinetic_energy

    def hamiltonian(self, x, v, beta):
        """Compute the overall Hamiltonian."""
        with tf.name_scope('hamiltonian'):
            kinetic = self.kinetic_energy(v)
            potential = self.potential_energy(x, beta)

            hamiltonian = potential + kinetic

        return hamiltonian
