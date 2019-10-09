
"""Defines Accountant class for keeping track of privacy spending.

A privacy accountant keeps track of privacy spendings. It has methods
accumulate_privacy_spending and get_privacy_spent.
"""
from __future__ import division

import abc
import collections
import math
import sys

import numpy as np
import torch
import torch.nn as nn

from differential_privacy.dp_sgd.dp_optimizer import utils

EpsDelta = collections.namedtuple("EpsDelta", ["spent_eps", "spent_delta"])


class MomentsAccountant(object):
  """Privacy accountant which keeps track of moments of privacy loss.

  MomentsAccountant accumulates the high moments of the privacy loss. It
  requires a method for computing differenital moments of the noise (See
  below for the definition). So every specific accountant should subclass
  this class by implementing _differential_moments method.

  Denote by X_i the random variable of privacy loss at the i-th step.
  Consider two databases D, D' which differ by one item. X_i takes value
  log Pr[M(D')==x]/Pr[M(D)==x] with probability Pr[M(D)==x].
  In MomentsAccountant, we keep track of y_i(L) = log E[exp(L X_i)] for some
  large enough L. To compute the final privacy spending,  we apply Chernoff
  bound (assuming the random noise added at each step is independent) to
  bound the total privacy loss Z = sum X_i as follows:
    Pr[Z > e] = Pr[exp(L Z) > exp(L e)]
              < E[exp(L Z)] / exp(L e)
              = Prod_i E[exp(L X_i)] / exp(L e)
              = exp(sum_i log E[exp(L X_i)]) / exp(L e)
              = exp(sum_i y_i(L) - L e)
  Hence the mechanism is (e, d)-differentially private for
    d =  exp(sum_i y_i(L) - L e).
  We require d < 1, i.e. e > sum_i y_i(L) / L. We maintain y_i(L) for several
  L to compute the best d for any give e (normally should be the lowest L
  such that 2 * sum_i y_i(L) / L < e.

  We further assume that at each step, the mechanism operates on a random
  sample with sampling probability q = batch_size / total_examples. Then
    E[exp(L X)] = E[(Pr[M(D)==x / Pr[M(D')==x])^L]
  By distinguishing two cases of whether D < D' or D' < D, we have
  that
    E[exp(L X)] <= max (I1, I2)
  where
    I1 = (1-q) E ((1-q) + q P(X+1) / P(X))^L + q E ((1-q) + q P(X) / P(X-1))^L
    I2 = E (P(X) / ((1-q) + q P(X+1)))^L

  In order to compute I1 and I2, one can consider to
    1. use an asymptotic bound, which recovers the advance composition theorem;
    2. use the closed formula (like GaussianMomentsAccountant);
    3. use numerical integration or random sample estimation.

  Dependent on the distribution, we can often obtain a tigher estimation on
  the moments and hence a more accurate estimation of the privacy loss than
  obtained using generic composition theorems.

  """

  __metaclass__ = abc.ABCMeta

  def __init__(self, total_examples, moment_orders=32):
    """Initialize a MomentsAccountant.

    Args:
      total_examples: total number of examples.
      moment_orders: the order of moments to keep.
    """

    assert total_examples > 0
    self._total_examples = total_examples
    self._moment_orders = (moment_orders
                           if isinstance(moment_orders, (list, tuple))
                           else range(1, moment_orders + 1))
    self._max_moment_order = max(self._moment_orders)
    assert self._max_moment_order < 100, "The moment order is too large."
    self._log_moments = [np.float64(0.0)
                         for moment_order in self._moment_orders]

  @abc.abstractmethod
  def _compute_log_moment(self, sigma, q, moment_order):
    """Compute high moment of privacy loss.

    Args:
      sigma: the noise sigma, in the multiples of the sensitivity.
      q: the sampling ratio.
      moment_order: the order of moment.
    Returns:
      log E[exp(moment_order * X)]
    """
    pass

  def accumulate_privacy_spending(self, sigma, num_examples):
    """Accumulate privacy spending.

    In particular, accounts for privacy spending when we assume there
    are num_examples, and we are releasing the vector
    (sum_{i=1}^{num_examples} x_i) + Normal(0, stddev=l2norm_bound*sigma)
    where l2norm_bound is the maximum l2_norm of each example x_i, and
    the num_examples have been randomly selected out of a pool of
    self.total_examples.

    Args:
      sigma: the noise sigma, in the multiples of the sensitivity (that is,
        if the l2norm sensitivity is k, then the caller must have added
        Gaussian noise with stddev=k*sigma to the result of the query).
      num_examples: the number of examples involved.
    Returns:
      a TensorFlow operation for updating the privacy spending.
    """
    q = num_examples * 1.0 / self._total_examples

    moments_accum_ops = []
    for i in range(len(self._log_moments)):
      moment = self._compute_log_moment(sigma, q, self._moment_orders[i])
      self._log_moments[i] += moment

  def _compute_delta(self, log_moments, eps):
    """Compute delta for given log_moments and eps.

    Args:
      log_moments: the log moments of privacy loss, in the form of pairs
        of (moment_order, log_moment)
      eps: the target epsilon.
    Returns:
      delta
    """
    min_delta = 1.0
    for moment_order, log_moment in log_moments:
      if math.isinf(log_moment) or math.isnan(log_moment):
        sys.stderr.write("The %d-th order is inf or Nan\n" % moment_order)
        continue
      if log_moment < moment_order * eps:
        min_delta = min(min_delta,
                        math.exp(log_moment - moment_order * eps))
    return min_delta

  def _compute_eps(self, log_moments, delta):
    min_eps = float("inf")
    for moment_order, log_moment in log_moments:
      if math.isinf(log_moment) or math.isnan(log_moment):
        sys.stderr.write("The %d-th order is inf or Nan\n" % moment_order)
        continue
      min_eps = min(min_eps, (log_moment - math.log(delta)) / moment_order)
    return min_eps

  def get_privacy_spent(self, target_eps=None, target_deltas=None):
    """Compute privacy spending in (e, d)-DP form for a single or list of eps.

    Args:
      target_eps: a list of target epsilon's for which we would like to
        compute corresponding delta value.
      target_deltas: a list of target deltas for which we would like to
        compute the corresponding eps value. Caller must specify
        either target_eps or target_delta.
    Returns:
      A list of EpsDelta pairs.
    """
    assert (target_eps is None) ^ (target_deltas is None)
    eps_deltas = []
    log_moments = self._log_moments
    if target_eps is not None:
      for eps in target_eps:
        log_moments_with_order = zip(self._moment_orders, log_moments)
        eps_deltas.append(
            EpsDelta(eps, self._compute_delta(log_moments_with_order, eps)))
    else:
      assert target_deltas
      for delta in target_deltas:
        log_moments_with_order = zip(self._moment_orders, log_moments)
        eps_deltas.append(
            EpsDelta(self._compute_eps(log_moments_with_order, delta), delta))
    return eps_deltas


class GaussianMomentsAccountant(MomentsAccountant):
  """MomentsAccountant which assumes Gaussian noise.

  GaussianMomentsAccountant assumes the noise added is centered Gaussian
  noise N(0, sigma^2 I). In this case, we can compute the differential moments
  accurately using a formula.

  For asymptotic bound, for Gaussian noise with variance sigma^2, we can show
  for L < sigma^2,  q L < sigma,
    log E[exp(L X)] = O(q^2 L^2 / sigma^2).
  Using this we derive that for training T epoches, with batch ratio q,
  the Gaussian mechanism with variance sigma^2 (with q < 1/sigma) is (e, d)
  private for d = exp(T/q q^2 L^2 / sigma^2 - L e). Setting L = sigma^2,
  Tq = e/2, the mechanism is (e, exp(-e sigma^2/2))-DP. Equivalently, the
  mechanism is (e, d)-DP if sigma = sqrt{2 log(1/d)}/e, q < 1/sigma,
  and T < e/(2q). This bound is better than the bound obtained using general
  composition theorems, by an Omega(sqrt{log k}) factor on epsilon, if we run
  k steps. Since we use direct estimate, the obtained privacy bound has tight
  constant.

  For GaussianMomentAccountant, it suffices to compute I1, as I1 >= I2,
  which reduce to computing E(P(x+s)/P(x+s-1) - 1)^i for s = 0 and 1. In the
  companion gaussian_moments.py file, we supply procedure for computing both
  I1 and I2 (the computation of I2 is through multi-precision integration
  package). It can be verified that indeed I1 >= I2 for wide range of parameters
  we have tried, though at the moment we are unable to prove this claim.

  We recommend that when using this accountant, users independently verify
  using gaussian_moments.py that for their parameters, I1 is indeed larger
  than I2. This can be done by following the instructions in
  gaussian_moments.py.
  """

  def __init__(self, total_examples, moment_orders=32):
    """Initialization.

    Args:
      total_examples: total number of examples.
      moment_orders: the order of moments to keep.
    """
    super(self.__class__, self).__init__(total_examples, moment_orders)
    self._binomial_table = utils.GenerateBinomialTable(self._max_moment_order)

  def _differential_moments(self, sigma, s, t):
    """Compute 0 to t-th differential moments for Gaussian variable.

        E[(P(x+s)/P(x+s-1)-1)^t]
      = sum_{i=0}^t (t choose i) (-1)^{t-i} E[(P(x+s)/P(x+s-1))^i]
      = sum_{i=0}^t (t choose i) (-1)^{t-i} E[exp(-i*(2*x+2*s-1)/(2*sigma^2))]
      = sum_{i=0}^t (t choose i) (-1)^{t-i} exp(i(i+1-2*s)/(2 sigma^2))
    Args:
      sigma: the noise sigma, in the multiples of the sensitivity.
      s: the shift.
      t: 0 to t-th moment.
    Returns:
      0 to t-th moment as a tensor of shape [t+1].
    """
    assert t <= self._max_moment_order, ("The order of %d is out "
                                         "of the upper bound %d."
                                         % (t, self._max_moment_order))
    binomial = self._binomial_table[0:t+1, 0:t+1]
    signs = np.zeros((t + 1, t + 1), dtype=np.float64)
    for i in range(t + 1):
      for j in range(t + 1):
        signs[i, j] = 1.0 - 2 * ((i - j) % 2)
    exponents = np.array([j * (j + 1.0 - 2.0 * s) / (2.0 * sigma * sigma)
                             for j in range(t + 1)], dtype=np.float64)
    # x[i, j] = binomial[i, j] * signs[i, j] = (i choose j) * (-1)^{i-j}
    x = binomial * signs
    # y[i, j] = x[i, j] * exp(exponents[j])
    #         = (i choose j) * (-1)^{i-j} * exp(j(j-1)/(2 sigma^2))
    # Note: this computation is done by broadcasting pointwise multiplication
    # between [t+1, t+1] tensor and [t+1] tensor.
    y = x * np.exp(exponents)
    # z[i] = sum_j y[i, j]
    #      = sum_j (i choose j) * (-1)^{i-j} * exp(j(j-1)/(2 sigma^2))
    z = np.sum(y, axis=1)
    return z

  def _compute_log_moment(self, sigma, q, moment_order):
    """Compute high moment of privacy loss.

    Args:
      sigma: the noise sigma, in the multiples of the sensitivity.
      q: the sampling ratio.
      moment_order: the order of moment.
    Returns:
      log E[exp(moment_order * X)]
    """
    assert moment_order <= self._max_moment_order, ("The order of %d is out "
                                                    "of the upper bound %d."
                                                    % (moment_order,
                                                       self._max_moment_order))
    binomial_table = self._binomial_table[moment_order, 0:(moment_order+1)]
    # qs = [1 q q^2 ... q^L] = exp([0 1 2 ... L] * log(q))
    qs = np.exp(np.array([i * 1.0 for i in range(moment_order + 1)],
                            dtype=np.float64) * np.log(q))
    moments0 = self._differential_moments(sigma, 0.0, moment_order)
    term0 = np.sum(binomial_table * qs * moments0)
    moments1 = self._differential_moments(sigma, 1.0, moment_order)
    term1 = np.sum(binomial_table * qs * moments1)
    return np.squeeze(np.log(q * term0 + (1.0 - q) * term1))
