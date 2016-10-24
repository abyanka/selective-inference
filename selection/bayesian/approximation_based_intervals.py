import numpy as np
import regreg.api as rr
from selection.bayesian.selection_probability_rr import nonnegative_softmax_scaled, cube_subproblem_scaled, \
    cube_barrier_scaled, cube_gradient_scaled, cube_hessian_scaled, cube_objective

#class should return approximate probability of (\beta_E,u_{-E}) in K conditional on s:
class approximate_density(rr.smooth_atom):

    def __init__(self,
                 y,
                 X,
                 feasible_point,
                 active,
                 active_signs,
                 lagrange,
                 mean_parameter,  # in R^n
                 noise_variance,
                 randomizer,
                 epsilon,
                 j, #index of interest amongst active variables
                 s, #point at which density is to computed
                 coef = 1.,
                 offset= None,
                 quadratic= None,
                 nstep = 10):

        n, p = X.shape
        E = active.sum()
        self._X = X

        self.y = y

        self.active = active

        self.noise_variance = noise_variance

        self.randomization = randomizer

        self.inactive_conjugate = self.active_conjugate = randomizer.CGF_conjugate

        if self.active_conjugate is None:
            raise ValueError(
                'randomization must know its CGF_conjugate -- currently only isotropic_gaussian and laplace are implemented and are assumed to be randomization with IID coordinates')

        self.inactive_lagrange = lagrange[~active]

        initial = feasible_point

        #self.feasible_point = feasible_point

        rr.smooth_atom.__init__(self,
                                (E,),
                                offset=offset,
                                quadratic=quadratic,
                                initial=initial,
                                coef=coef)

        self.coefs[:] = initial

        self.nonnegative_barrier = nonnegative_softmax_scaled(E)

        X_E = self.X_E = X[:, active]
        self.X_inactive = X[:, ~active]

        B = X.T.dot(X_E)

        B_E = B[active]
        B_mE = B[~active]

        self.B_active = (B_E + epsilon * np.identity(E)) * active_signs[None, :]
        self.B_inactive = B_mE * active_signs[None, :]\

        subgrad_offset = active_signs * lagrange[active]

        self.offset_active = self.offset(j, s, subgrad_offset)[:E,]

        self.offset_inactive = self.offset(j, s, subgrad_offset)[E:, ]

        self.active_conj_loss = rr.affine_smooth(self.active_conjugate,
                                                 rr.affine_transform(self.B_active, self.offset_active))

        cube_obj = cube_objective(self.inactive_conjugate,
                                  lagrange[~active],
                                  nstep=nstep)

        self.cube_loss = rr.affine_smooth(cube_obj, rr.affine_transform(self.B_inactive, self.offset_inactive))

        self.total_loss = rr.smooth_sum([self.active_conj_loss,
                                         self.cube_loss,
                                         self.nonnegative_barrier])

    def offset(self,j, s, subgrad_offset):

        eta = self.XE_pinv[j, :]
        c = np.true_divide(eta, self.eta_norm_sq[j])
        fixed_part = np.dot(np.identity(self.y.shape) - np.outer(c, eta), self.y)
        _offset_active = subgrad_offset - self.X_E.T.dot(fixed_part) - s*(self.X_E.T.dot(c))
        _offset_inactive = - self.X_inactive.T.dot(fixed_part) - s*(self.X_inactive.T.dot(c))
        return np.vstack[_offset_active,_offset_inactive]

    def smooth_objective(self, param, mode='both', check_feasibility=False):

        param = self.apply_offset(param)

        if mode == 'func':
            f = self.total_loss.smooth_objective(param, 'func')
            return self.scale(f)
        elif mode == 'grad':
            g = self.total_loss.smooth_objective(param, 'grad')
            return self.scale(g)
        elif mode == 'both':
            f, g = self.total_loss.smooth_objective(param, 'both')
            return self.scale(f), self.scale(g)
        else:
            raise ValueError("mode incorrectly specified")


class approximate_CI():
    
































