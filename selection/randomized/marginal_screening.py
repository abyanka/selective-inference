from __future__ import print_function
import functools
import numpy as np
from scipy.stats import norm as ndist

import regreg.api as rr

from .base import restricted_estimator
from ..constraints.affine import constraints
from .query import affine_gaussian_sampler
from .randomization import randomization
from ..algorithms.debiased_lasso import debiasing_matrix

def marginal_screening_selection(p_values, level):

    m = p_values.shape[0]
    p_sorted = np.sort(p_values)
    indices = np.arange(m)
    indices_order = np.argsort(p_values)
    active = (p_values <= level)

    return active

class marginal_screening(object):

    def __init__(self,
                 observed_data,
                 covariance, 
                 randomizer_scale,
                 marginal_level,
                 perturb=None):

        self.observed_score_state = -observed_data  # -Z if Z \sim N(\mu,\Sigma), X^Ty in regression setting
        self.nfeature = p = self.observed_score_state.shape[0]
        self.covariance = covariance
        randomized_stdev = np.sqrt(np.diag(covariance) + randomizer_scale**2)
        self.randomizer = randomization.isotropic_gaussian((p,), randomizer_scale)
        self._initial_omega = perturb
        self.marginal_level = marginal_level
        self.threshold = randomized_stdev * ndist.ppf(1. - self.marginal_level / 2.)

    def fit(self, perturb=None):

        p = self.nfeature

        # take a new perturbation if supplied
        if perturb is not None:
            self._initial_omega = perturb
        if self._initial_omega is None:
            self._initial_omega = self.randomizer.sample()

        _randomized_score = self.observed_score_state - self._initial_omega
        Z = -_randomized_score
        soft_thresh = np.sign(Z) * (np.fabs(Z) - self.threshold) * (np.fabs(Z) >= self.threshold)
        active = soft_thresh != 0
        self.initial_soln = soft_thresh

        self._selected = active
        self._not_selected = ~self._selected
        sign = np.sign(Z)
        active_signs = sign[self._selected]
        sign[self._not_selected] = 0
        self.selection_variable = {'sign': sign,
                                   'variables': self._selected.copy()}

        self.observed_opt_state = np.fabs(soft_thresh[self._selected])
        self.num_opt_var = self.observed_opt_state.shape[0]

        opt_linear = np.zeros((p, self.num_opt_var))
        opt_linear[self._selected,:] = np.diag(active_signs)
        opt_offset = np.zeros(p)
        opt_offset[self._selected] = active_signs * self.threshold[self._selected]
        opt_offset[self._not_selected] = _randomized_score[self._not_selected]

        self.opt_transform = (opt_linear, opt_offset)
        self._setup = True

        _, prec = self.randomizer.cov_prec

        if np.asarray(prec).shape in [(), (0,)]:
            cond_precision = opt_linear.T.dot(opt_linear) * prec
            cond_cov = np.linalg.inv(cond_precision)
            logdens_linear = cond_cov.dot(opt_linear.T) * prec  
        else:
            cond_precision = opt_linear.T.dot(prec.dot(opt_linear))
            cond_cov = np.linalg.inv(cond_precision)
            logdens_linear = cond_cov.dot(opt_linear.T).dot(prec)

        cond_mean = logdens_linear.dot(-self.observed_score_state - opt_offset)

        logdens_transform = (logdens_linear, opt_offset)
        A_scaling = -np.identity(len(active_signs))
        b_scaling = np.zeros(self.num_opt_var)

        def log_density(logdens_linear, offset, cond_prec, score, opt):
            if score.ndim == 1:
                mean_term = logdens_linear.dot(score.T + offset).T
            else:
                mean_term = logdens_linear.dot(score.T + offset[:, None]).T
            arg = opt + mean_term
            return - 0.5 * np.sum(arg * cond_prec.dot(arg.T).T, 1)

        log_density = functools.partial(log_density, logdens_linear, opt_offset, cond_precision)

        affine_con = constraints(A_scaling,
                                 b_scaling,
                                 mean=cond_mean,
                                 covariance=cond_cov)

        self.sampler = affine_gaussian_sampler(affine_con,
                                               self.observed_opt_state,
                                               self.observed_score_state,
                                               log_density,
                                               logdens_transform,
                                               selection_info=self.selection_variable)
        return self._selected

    def summary(self,
                observed_target,
                cov_target,
                cov_target_score,
                alternatives,
                parameter=None,
                level=0.9,
                ndraw=10000,
                burnin=2000,
                compute_intervals=False):
        """
        Produce p-values and confidence intervals for targets
        of model including selected features
        Parameters
        ----------
        """

        if parameter is None:
            parameter = np.zeros(observed_target.shape[0])

        opt_sample = self.sampler.sample(ndraw, burnin)

        pivots = self.sampler.coefficient_pvalues(observed_target,
                                                  cov_target,
                                                  cov_target_score,
                                                  parameter=parameter,
                                                  sample=opt_sample,
                                                  alternatives=alternatives)
        if not np.all(parameter == 0):
            pvalues = self.sampler.coefficient_pvalues(observed_target,
                                                       cov_target,
                                                       cov_target_score,
                                                       parameter=np.zeros_like(parameter),
                                                       sample=opt_sample,
                                                       alternatives=alternatives)
        else:
            pvalues = pivots

        intervals = None
        if compute_intervals:
            intervals = self.sampler.confidence_intervals(observed_target,
                                                          cov_target,
                                                          cov_target_score,
                                                          sample=opt_sample,
                                                          level=level)

        return pivots, pvalues, intervals

    def selective_MLE(self,
                      observed_target,
                      cov_target,
                      cov_target_score,
                      level=0.9,
                      solve_args={'tol':1.e-12}):
        """

        Parameters
        ----------

        level : float
            Confidence level.

        """

        return self.sampler.selective_MLE(observed_target,
                                          cov_target,
                                          cov_target_score,
                                          self.observed_opt_state,
                                          solve_args=solve_args)

    def multivariate_targets(self, features, dispersion=1.):
        """
        Entries of the mean of \Sigma[E,E]^{-1}Z_E
        """
        score_linear = self.covariance[:, features] / dispersion
        Q = score_linear[features]
        cov_target = np.linalg.inv(Q)
        observed_target = -np.linalg.inv(Q).dot(self.observed_score_state[features])
        crosscov_target_score = -score_linear.dot(cov_target)
        #alternatives = ([{1: 'greater', -1: 'less'}[int(s)] for s in 
        #                 self.selection_variable['sign'][features]])
        alternatives = ['twosided'] * features.sum()

        return observed_target, cov_target * dispersion, crosscov_target_score.T * dispersion, alternatives

    def marginal_targets(self, features):
        """
        Entries of the mean of Z_E
        """
        score_linear = self.covariance[:, features]
        Q = score_linear[features]
        cov_target = Q
        observed_target = -self.observed_score_state[features]
        crosscov_target_score = -score_linear
        #alternatives = ([{1: 'greater', -1: 'less'}[int(s)] for s in 
        #                 self.selection_variable['sign'][features]])
        alternatives = ['twosided'] * features.sum()

        return observed_target, cov_target, crosscov_target_score.T, alternatives



