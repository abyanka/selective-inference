from __future__ import print_function
import functools
import numpy as np
from regreg.atoms.slope import slope
from selection.randomized.randomization import randomization
import regreg.api as rr
from selection.randomized.base import restricted_estimator
from selection.constraints.affine import constraints
from selection.randomized.query import (query,
                                        multiple_queries,
                                        langevin_sampler,
                                        affine_gaussian_sampler)

class randomized_slope():

    def __init__(self,
                 loglike,
                 feature_weights,
                 ridge_term,
                 randomizer_scale,
                 perturb=None):
        r"""
        Create a new post-selection object for the SLOPE problem
        Parameters
        ----------
        loglike : `regreg.smooth.glm.glm`
            A (negative) log-likelihood as implemented in `regreg`.
        feature_weights : np.ndarray
            Feature weights for L-1 penalty. If a float,
            it is broadcast to all features.
        ridge_term : float
            How big a ridge term to add?
        randomizer_scale : float
            Scale for IID components of randomization.
        perturb : np.ndarray
            Random perturbation subtracted as a linear
            term in the objective function.
        """

        self.loglike = loglike
        self.nfeature = p = self.loglike.shape[0]

        if np.asarray(feature_weights).shape == ():
            feature_weights = np.ones(loglike.shape) * feature_weights
        self.feature_weights = np.asarray(feature_weights)

        self.randomizer = randomization.isotropic_gaussian((p,), randomizer_scale)
        self.ridge_term = ridge_term
        self.penalty = slope(feature_weights, lagrange=1.)
        self._initial_omega = perturb  # random perturbation

    def fit(self,
            solve_args={'tol': 1.e-12, 'min_its': 50},
            perturb=None):

        p = self.nfeature

        # take a new perturbation if supplied
        if perturb is not None:
            self._initial_omega = perturb
        if self._initial_omega is None:
            self._initial_omega = self.randomizer.sample()

        quad = rr.identity_quadratic(self.ridge_term, 0, -self._initial_omega, 0)
        problem = rr.simple_problem(self.loglike, self.penalty)
        self.initial_soln = problem.solve(quad, **solve_args)

        active_signs = np.sign(self.initial_soln)
        active = self._active = active_signs != 0
        self._unpenalized = np.zeros(p, np.bool)

        self._overall = overall = active> 0
        self._inactive = inactive = ~self._overall

        _active_signs = active_signs.copy()
        self.selection_variable = {'sign': _active_signs,
                                   'variables': self._overall}

        initial_subgrad = -(self.loglike.smooth_objective(self.initial_soln, 'grad') +
                            quad.objective(self.initial_soln, 'grad'))
        self.initial_subgrad = initial_subgrad

        indices = np.argsort(-np.fabs(self.initial_soln))
        sorted_soln = self.initial_soln[indices]
        initial_scalings = np.sort(np.unique(np.fabs(self.initial_soln[active])))[::-1]
        self.observed_opt_state = initial_scalings
        #print("observed opt state", self.observed_opt_state)

        _beta_unpenalized = restricted_estimator(self.loglike, self._overall, solve_args=solve_args)

        beta_bar = np.zeros(p)
        beta_bar[overall] = _beta_unpenalized
        self._beta_full = beta_bar

        self.num_opt_var = self.observed_opt_state.shape[0]

        _opt_linear_term = np.zeros((p, self.num_opt_var))
        _score_linear_term = np.zeros((p, self.num_opt_var))

        X, y = self.loglike.data
        W = self._W = self.loglike.saturated_loss.hessian(X.dot(beta_bar))
        _hessian_active = np.dot(X.T, X[:, active] * W[:, None])
        _score_linear_term = _hessian_active
        self.score_transform = (_score_linear_term, np.zeros(_score_linear_term.shape[0]))

        self.observed_score_state = _score_linear_term.dot(_beta_unpenalized)
        self.observed_score_state[inactive] += self.loglike.smooth_objective(beta_bar, 'grad')[inactive]

        cur_indx_array = []
        cur_indx_array.append(0)
        cur_indx = 0
        pointer = 0
        signs_cluster = []
        for j in range(p - 1):
            if np.abs(sorted_soln[j + 1]) != np.abs(sorted_soln[cur_indx]):
                cur_indx_array.append(j + 1)
                cur_indx = j + 1
                sign_vec = np.zeros(p)
                sign_vec[np.arange(j + 1 - cur_indx_array[pointer]) + cur_indx_array[pointer]] = \
                    np.sign(self.initial_soln[indices[np.arange(j + 1 - cur_indx_array[pointer]) + cur_indx_array[pointer]]])
                signs_cluster.append(sign_vec)
                pointer = pointer + 1
                if sorted_soln[j + 1] == 0:
                    break

        signs_cluster = np.asarray(signs_cluster).T
        X_clustered = X[:, indices].dot(signs_cluster)
        _opt_linear_term = -X.T.dot(X_clustered)
        self.opt_transform = (_opt_linear_term, self.initial_subgrad)

        cov, prec = self.randomizer.cov_prec
        opt_linear, opt_offset = self.opt_transform

        cond_precision = opt_linear.T.dot(opt_linear) * prec
        cond_cov = np.linalg.inv(cond_precision)
        logdens_linear = cond_cov.dot(opt_linear.T) * prec
        cond_mean = -logdens_linear.dot(self.observed_score_state + opt_offset)
        #print("shapes", cond_mean.shape, cond_precision.shape)

        def log_density(logdens_linear, offset, cond_prec, score, opt):
            if score.ndim == 1:
                mean_term = logdens_linear.dot(score.T + offset).T
            else:
                mean_term = logdens_linear.dot(score.T + offset[:, None]).T
            arg = opt + mean_term
            return - 0.5 * np.sum(arg * cond_prec.dot(arg.T).T, 1)

        log_density = functools.partial(log_density, logdens_linear, opt_offset, cond_precision)

        # now make the constraints

        A_scaling = -np.identity(self.num_opt_var)
        b_scaling = np.zeros(self.num_opt_var)

        affine_con = constraints(A_scaling,
                                 b_scaling,
                                 mean=cond_mean,
                                 covariance=cond_cov)

        logdens_transform = (logdens_linear, opt_offset)

        self.sampler = affine_gaussian_sampler(affine_con,
                                               self.observed_opt_state,
                                               self.observed_score_state,
                                               log_density,
                                               logdens_transform,
                                               selection_info=self.selection_variable)
        return active_signs

    def selective_MLE(self,
                      target="selected",
                      features=None,
                      parameter=None,
                      level=0.9,
                      compute_intervals=False,
                      dispersion=None,
                      solve_args={'tol': 1.e-12}):
        """
        Parameters
        ----------
        target : one of ['selected', 'full']
        features : np.bool
            Binary encoding of which features to use in final
            model and targets.
        parameter : np.array
            Hypothesized value for parameter -- defaults to 0.
        level : float
            Confidence level.
        ndraw : int (optional)
            Defaults to 1000.
        burnin : int (optional)
            Defaults to 1000.
        compute_intervals : bool
            Compute confidence intervals?
        dispersion : float (optional)
            Use a known value for dispersion, or Pearson's X^2?
        """

        if parameter is None:
            parameter = np.zeros(self.loglike.shape[0])

        if target == 'selected':
            observed_target, cov_target, cov_target_score, alternatives = self.selected_targets(features=features,
                                                                                                dispersion=dispersion)
        # elif target == 'full':
        #     X, y = self.loglike.data
        #     n, p = X.shape
        #     if n > p:
        #         observed_target, cov_target, cov_target_score, alternatives = self.full_targets(features=features,
        #                                                                                         dispersion=dispersion)
        #     else:
        #         observed_target, cov_target, cov_target_score, alternatives = self.debiased_targets(features=features,
        #                                                                                             dispersion=dispersion)

        # working out conditional law of opt variables given
        # target after decomposing score wrt target

        return self.sampler.selective_MLE(observed_target,
                                          cov_target,
                                          cov_target_score,
                                          self.observed_opt_state,
                                          solve_args=solve_args)

    # Targets of inference
    # and covariance with score representation

    def selected_targets(self, features=None, dispersion=None):

        X, y = self.loglike.data
        n, p = X.shape

        if features is None:
            active = self._active
            unpenalized = self._unpenalized
            noverall = active.sum() + unpenalized.sum()
            overall = active + unpenalized

            score_linear = self.score_transform[0]
            Q = -score_linear[overall]
            cov_target = np.linalg.inv(Q)
            observed_target = self._beta_full[overall]
            crosscov_target_score = score_linear.dot(cov_target)
            Xfeat = X[:, overall]
            alternatives = [{1: 'greater', -1: 'less'}[int(s)] for s in self.selection_variable['sign'][active]] \
                           + ['twosided'] * unpenalized.sum()

        else:

            features_b = np.zeros_like(self._overall)
            features_b[features] = True
            features = features_b

            Xfeat = X[:, features]
            Qfeat = Xfeat.T.dot(self._W[:, None] * Xfeat)
            Gfeat = self.loglike.smooth_objective(self.initial_soln, 'grad')[features]
            Qfeat_inv = np.linalg.inv(Qfeat)
            one_step = self.initial_soln[features] - Qfeat_inv.dot(Gfeat)
            cov_target = Qfeat_inv
            _score_linear = -Xfeat.T.dot(self._W[:, None] * X).T
            crosscov_target_score = _score_linear.dot(cov_target)
            observed_target = one_step
            alternatives = ['twosided'] * features.sum()

        if dispersion is None:  # use Pearson's X^2
            dispersion = ((y - self.loglike.saturated_loss.mean_function(
                Xfeat.dot(observed_target))) ** 2 / self._W).sum() / (n - Xfeat.shape[1])

        print(dispersion, 'dispersion')
        return observed_target, cov_target * dispersion, crosscov_target_score.T * dispersion, alternatives

    @staticmethod
    def gaussian(X,
                 Y,
                 feature_weights,
                 sigma=1.,
                 quadratic=None,
                 ridge_term=0.,
                 randomizer_scale=None):

        loglike = rr.glm.gaussian(X, Y, coef=1. / sigma ** 2, quadratic=quadratic)
        n, p = X.shape

        mean_diag = np.mean((X ** 2).sum(0))
        if ridge_term is None:
            ridge_term = np.std(Y) * np.sqrt(mean_diag) / np.sqrt(n - 1)

        if randomizer_scale is None:
            randomizer_scale = np.sqrt(mean_diag) * 0.5 * np.std(Y) * np.sqrt(n / (n - 1.))

        return randomized_slope(loglike, np.asarray(feature_weights) / sigma ** 2, ridge_term, randomizer_scale)
