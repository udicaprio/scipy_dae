import numpy as np
from numpy.polynomial import Polynomial as Poly
from scipy.linalg import eig, cdf2rdf
from scipy.integrate._ivp.common import norm, EPS, warn_extraneous
from .base import DAEDenseOutput
from .dae import DaeSolver

"""
References
----------
.. [1] van der Houwen, P. J., de Swart, J. J. B. Parallel linear system solvers for 
       Runge-Kutta methods. Advances in Computational Mathematics 7, 157-181 (1997). 
.. [2] P. J. van der Houwen and J. J. B. de Swart, "Triangularly Implicit Iteration Methods 
       for ODE-IVP Solvers", SIAM Journal on Scientific Computing 18, 41-55 (1997).
.. [3] Hoffmann, W., De Swart, J. J. B. Approximating Runge-Kutta matrices by triangular 
       matrices. Bit Numer Math 37, 346-354 (1997).
"""

DAMPING_RATIO_ERROR_ESTIMATE = 0.01 # Hairer (8.19) is obtained by the choice 1.0. 
                                    # de Swart proposes 0.067 for s=3.
MIN_FACTOR = 0.2  # Minimum allowed decrease in a step size.
MAX_FACTOR = 10  # Maximum allowed increase in a step size.
KAPPA = 1 # Factor of the smooth limiter

# UNKNOWN_VELOCITIES = False
UNKNOWN_VELOCITIES = True


def radau_butcher_tableau(s):
    # nodes are given by the zeros of the Radau polynomial, see Hairer1999 (7)
    poly = Poly([0, 1]) ** (s - 1) * Poly([-1, 1]) ** s
    poly_der = poly.deriv(s - 1)
    c = poly_der.roots()

    # compute coefficients a_ij, see Hairer1999 (11)
    A = np.zeros((s, s))
    for i in range(s):
        Mi = np.zeros((s, s))
        ri = np.zeros(s)
        for q in range(s):
            Mi[q] = c**q
            ri[q] = c[i] ** (q + 1) / (q + 1)
        A[i] = np.linalg.solve(Mi, ri)

    b = A[-1, :]
    p = 2 * s - 1
    return A, b, c, p


def LUdecompCrout(A):
    """Perform Crout's LU decomposition on matrix A."""
    n = A.shape[0]
    L = np.zeros_like(A)
    U = np.zeros_like(A)

    for j in range(n):
        for i in range(j, n):
            L[i, j] = A[i, j] - np.dot(L[i, :j], U[:j, j])

        U[j, j] = 1
        for i in range(j + 1, n):
            U[j, i] = (A[j, i] - np.dot(L[j, :j], U[:j, i])) / L[j, j]

    return L, U


def radau_constants(s):
    # Butcher tableau
    A, b, c, p = radau_butcher_tableau(s)
    A_inv = np.linalg.inv(A)

    # Section 3.2.2 in [2]
    T_L, T_U = LUdecompCrout(A)
    B = T_L
    # Z_0 = A - B
    # Z_oo = np.eye(*(B.shape)) - T_U

    # compute Butcher transformation that eliminates rhs of B = L + D splitting 
    lambdas, V = eig(B)
    idx = np.nonzero(lambdas.real[:, None] == np.diag(B))[1]
    lambdas = lambdas[idx]
    V = V[:, idx]
    Gammas, T = cdf2rdf(lambdas, V)
    gammas = np.diag(Gammas)
    TI = np.linalg.inv(T)

    # check if everything worked
    assert np.allclose(V @ np.diag(lambdas) @ np.linalg.inv(V), B)
    assert np.allclose(np.linalg.inv(V) @ B @ V, np.diag(lambdas))
    assert np.allclose(T @ Gammas @ TI, B)
    assert np.allclose(TI @ B @ T, Gammas)
    
    # compute embedded method for error estimate
    c_hat = np.array([0, *c])
    vander = np.vander(c_hat, increasing=True).T

    rhs = 1 / np.arange(1, s + 1)
    b_hats2 = gammas[-1]
    b_hat1 = DAMPING_RATIO_ERROR_ESTIMATE * b_hats2
    rhs[0] -= b_hat1
    rhs -= b_hats2

    b_hat = np.linalg.solve(vander[:-1, 1:], rhs)
    v = b - b_hat

    rhs2 = 1 / np.arange(1, s + 1)
    rhs2[0] -= gammas[-1]

    b_hat2 = np.linalg.solve(vander[:-1, 1:], rhs2)
    v2 = b_hat2 - b

    # Compute the inverse of the Vandermonde matrix to get the interpolation matrix P.
    P = np.linalg.inv(vander)[1:, 1:]

    # Compute coefficients using Vandermonde matrix.
    vander2 = np.vander(c, increasing=True)
    P2 = np.linalg.inv(vander2)

    return A, A_inv, gammas, c, T, TI, P, P2, b_hat1, v, v2, b_hat, b_hat2, p


def solve_collocation_system(fun, t, y, h, Z0, scale, tol,
                             LUs, solve_lu, C, T, TI, A, A_inv, 
                             newton_max_iter):
    """Solve the collocation system.

    Parameters
    ----------
    fun : callable
        Right-hand side of the system.
    t : float
        Current time.
    y : ndarray, shape (n,)
        Current state.
    h : float
        Step to try.
    Z0 : ndarray, shape (s, n)
        Initial guess for the solution. It determines new values of `y` at
        ``t + h * C`` as ``y + Z0``, where ``C`` is the Radau method constants.
    scale : ndarray, shape (n)
        Problem tolerance scale, i.e. ``rtol * abs(y) + atol``.
    tol : float
        Tolerance to which solve the system. This value is compared with
        the normalized by `scale` error.
    LU_real, LU_complex
        LU decompositions of the system Jacobians.
    solve_lu : callable
        Callable which solves a linear system given a LU decomposition. The
        signature is ``solve_lu(LU, b)``.
    C : ndarray, shape (s,)
        Array containing the Radau IIA nodes.
    T, TI : ndarray, shape (s, s)
        Transformation matrix and inverse of the methods coefficient matrix A.
    A_inv : ndarray, shape (s, s)
        Inverse the methods coefficient matrix A.

    Returns
    -------
    converged : bool
        Whether iterations converged.
    n_iter : int
        Number of completed iterations.
    Z : ndarray, shape (3, n)
        Found solution.
    rate : float
        The rate of convergence.
    """
    s, n = Z0.shape
    tau = t + h * C

    Z = Z0
    W = TI.dot(Z0)
    Yp = (A_inv / h) @ Z
    Y = y + Z

    F = np.empty((s, n))

    dW_norm_old = None
    dW = np.empty_like(W)
    converged = False
    rate = None
    for k in range(newton_max_iter):
        for i in range(s):
            F[i] = fun(tau[i], Y[i], Yp[i])

        if not np.all(np.isfinite(F)):
            break

        U = TI @ F
        for i in range(s):
            dW[i] = solve_lu(LUs[i], -U[i])

        dW_norm = norm(dW / scale)
        if dW_norm_old is not None:
            rate = dW_norm / dW_norm_old

        if (rate is not None and (rate >= 1 or rate ** (newton_max_iter - k) / (1 - rate) * dW_norm > tol)):
            break

        # if (rate is not None and rate >= 1.0):
        #     # print(f"rate >= 1")
        #     break
        #     # if n_bad_iter > 5:
        #     #     break
        #     # else:
        #     #     n_bad_iter += 1

        # # TODO: Why this is a bad indicator for divergence of the iteration?
        # if (rate is not None and rate ** (newton_max_iter - k) / (1 - rate) * dW_norm > tol):
        # #     print(f"rate ** (newton_max_iter - k) / (1 - rate) * dW_norm > tol")
        #     break

        W += dW
        Z = T.dot(W)
        Yp = (A_inv / h) @ Z
        Y = y + Z

        if (dW_norm == 0 or rate is not None and rate / (1 - rate) * dW_norm < tol):
            converged = True
            break

        dW_norm_old = dW_norm

    return converged, k + 1, Y, Yp, Z, rate


def solve_collocation_system2(fun, t, y, h, Yp0, scale, tol,
                              LUs, solve_lu, C, T, TI, A, 
                              newton_max_iter):
    s, n = Yp0.shape
    tau = t + h * C

    Yp = Yp0
    Y = y + h * A.dot(Yp)
    V_dot = TI.dot(Yp)

    F = np.empty((s, n))

    dY_norm_old = None
    dV_dot = np.empty_like(V_dot)
    converged = False
    rate = None
    for k in range(newton_max_iter):
        for i in range(s):
            F[i] = fun(tau[i], Y[i], Yp[i])

        if not np.all(np.isfinite(F)):
            break

        U = TI @ F
        for i in range(s):
            dV_dot[i] = solve_lu(LUs[i], -U[i])

        dYp = T.dot(dV_dot)
        dY = h * A.dot(dYp)

        Yp += dYp
        Y += dY

        dY_norm = norm(dY / scale)
        if dY_norm_old is not None:
            rate = dY_norm / dY_norm_old

        if (rate is not None and (rate >= 1 or rate ** (newton_max_iter - k) / (1 - rate) * dY_norm > tol)):
            break

        # if (rate is not None and rate >= 1.0):
        #     break

        # # TODO: Why this is a bad indicator for divergence of the iteration?
        # if (rate is not None and rate ** (newton_max_iter - k) / (1 - rate) * dY_norm > tol):
        #     break

        if (dY_norm == 0 or rate is not None and rate / (1 - rate) * dY_norm < tol):
            converged = True
            break

        dY_norm_old = dY_norm

    return converged, k + 1, Y, Yp, Y - y, rate


def predict_factor(h_abs, h_abs_old, error_norm, error_norm_old, s):
    """Predict by which factor to increase/decrease the step size.

    The algorithm is described in [1]_.

    Parameters
    ----------
    h_abs, h_abs_old : float
        Current and previous values of the step size, `h_abs_old` can be None
        (see Notes).
    error_norm, error_norm_old : float
        Current and previous values of the error norm, `error_norm_old` can
        be None (see Notes).
    s : int
        Number of stages of the Radau IIA method.

    Returns
    -------
    factor : float
        Predicted factor.

    Notes
    -----
    If `h_abs_old` and `error_norm_old` are both not None then a two-step
    algorithm is used, otherwise a one-step algorithm is used.

    References
    ----------
    .. [1] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential
           Equations II: Stiff and Differential-Algebraic Problems", Sec. IV.8.
    """
    if error_norm_old is None or h_abs_old is None or error_norm == 0:
        multiplier = 1
    else:
        multiplier = h_abs / h_abs_old * (error_norm_old / error_norm) ** (1 / (s + 1))

    with np.errstate(divide='ignore'):
        factor = min(1, multiplier) * error_norm ** (-1 / (s + 1))

    # # nonsmooth limiter
    # factor = max(MIN_FACTOR, min(factor, MAX_FACTOR))

    # smooth limiter
    factor = 1 + KAPPA * np.arctan((factor - 1) / KAPPA)

    return factor


class PTIRKDAE(DaeSolver):
    """Implicit Runge-Kutta method of Radau IIA family of order 2s - 1.

    The implementation follows [4]_, where most of the ideas come from [2]_. 
    The embedded formula of [3]_ is applied to implicit differential equations.
    The error is controlled with a (s)th-order accurate embedded formula as 
    introduced in [2]_ and refined in [3]_. The procedure is slightly adapted 
    by [4]_ to cope with implicit differential equations. The embedded error 
    estimate can be mixed with the contunous error of the lower order 
    collocation polynomial as porposed in [5]_ and [6]_.
    
    A cubic polynomial 
    which satisfies the collocation conditions is used for the dense output of 
    both state and derivatives.

    Parameters
    ----------
    fun : callable
        Function defining the DAE system: ``f(t, y, yp) = 0``. The calling 
        signature is ``fun(t, y, yp)``, where ``t`` is a scalar and 
        ``y, yp`` are ndarrays with 
        ``len(y) = len(yp) = len(y0) = len(yp0)``. ``fun`` must return 
        an array of the same shape as ``y, yp``. See `vectorized` for more
        information.
    t0 : float
        Initial time.
    y0 : array_like, shape (n,)
        Initial state.
    yp0 : array_like, shape (n,)
        Initial derivative.
    t_bound : float
        Boundary time - the integration won't continue beyond it. It also
        determines the direction of the integration.
    stages : int, optional
        Number of used stages. Default is 3, which corresponds to the 
        ``solve_ivp`` method. Only odd number of stages are allowed.
    first_step : float or None, optional
        Initial step size. Default is ``None`` which means that the algorithm
        should choose.
    newton_max_iter : int or None, optional
        Number of allowed (simplified) Newton iterations. Default is ``None`` 
        which uses ``newton_max_iter = 7 + (stages - 3) * 2`` as done in
        Hairer's radaup.f code.
    max_step : float, optional
        Maximum allowed step size. Default is np.inf, i.e., the step size is not
        bounded and determined solely by the solver.
    rtol, atol : float and array_like, optional
        Relative and absolute tolerances. The solver keeps the local error
        estimates less than ``atol + rtol * abs(y)``. HHere `rtol` controls a
        relative accuracy (number of correct digits), while `atol` controls
        absolute accuracy (number of correct decimal places). To achieve the
        desired `rtol`, set `atol` to be smaller than the smallest value that
        can be expected from ``rtol * abs(y)`` so that `rtol` dominates the
        allowable error. If `atol` is larger than ``rtol * abs(y)`` the
        number of correct digits is not guaranteed. Conversely, to achieve the
        desired `atol` set `rtol` such that ``rtol * abs(y)`` is always smaller
        than `atol`. If components of y have different scales, it might be
        beneficial to set different `atol` values for different components by
        passing array_like with shape (n,) for `atol`. Default values are
        1e-3 for `rtol` and 1e-6 for `atol`.
    continuous_error_weight : float, optional
        Weighting of continuous error of the dense output as introduced in 
        [5]_ and [6]_. The embedded error is weighted by (1 - continuous_error_weight). 
        Has to satisfy 0 <= continuous_error_weight <= 1. Default is 0.0, i.e., only 
        the embedded error is considered.
    jac : {None, array_like, sparse_matrix, callable}, optional
        Jacobian matrix of the right-hand side of the system with respect to
        y, required by this method. The Jacobian matrix has shape (n, n) and
        its element (i, j) is equal to ``d f_i / d y_j``.
        There are three ways to define the Jacobian:

            * If array_like or sparse_matrix, the Jacobian is assumed to
              be constant.
            * If callable, the Jacobian is assumed to depend on both
              t and y; it will be called as ``jac(t, y)`` as necessary.
              For the 'Radau' and 'BDF' methods, the return value might be a
              sparse matrix.
            * If None (default), the Jacobian will be approximated by
              finite differences.

        It is generally recommended to provide the Jacobian rather than
        relying on a finite-difference approximation.
    # TODO: Adapt and test this.
    jac_sparsity : {None, array_like, sparse matrix}, optional
        Defines a sparsity structure of the Jacobian matrix for a
        finite-difference approximation. Its shape must be (n, n). This argument
        is ignored if `jac` is not `None`. If the Jacobian has only few non-zero
        elements in *each* row, providing the sparsity structure will greatly
        speed up the computations [2]_. A zero entry means that a corresponding
        element in the Jacobian is always zero. If None (default), the Jacobian
        is assumed to be dense.
    vectorized : bool, optional
        Whether `fun` can be called in a vectorized fashion. Default is False.

        If ``vectorized`` is False, `fun` will always be called with ``y`` 
        and ``yp`` of shape ``(n,)``, where ``n = len(y0) = len(yp0)``.

        If ``vectorized`` is True, `fun` may be called with ``y`` and ``yp`` of 
        shape ``(n, k)``, where ``k`` is an integer. In this case, `fun` must 
        behave such that ``fun(t, y, yp)[:, i] == fun(t, y[:, i], yp[:, i])``.

        Setting ``vectorized=True`` allows for faster finite difference
        approximation of the Jacobian by this method, but may result in slower
        execution overall in some circumstances (e.g. small ``len(y0)``).
        Default is False.

    Attributes
    ----------
    n : int
        Number of equations.
    status : string
        Current status of the solver: 'running', 'finished' or 'failed'.
    t_bound : float
        Boundary time.
    direction : float
        Integration direction: +1 or -1.
    t : float
        Current time.
    y : ndarray
        Current state.
    yp : ndarray
        Current derivative.
    t_old : float
        Previous time. None if no steps were made yet.
    step_size : float
        Size of the last successful step. None if no steps were made yet.
    nfev : int
        Number of evaluations of the right-hand side.
    njev : int
        Number of evaluations of the Jacobian.
    nlu : int
        Number of LU decompositions.

    References
    ----------
    .. [1] E. Hairer, G. Wanner, "Solving Ordinary Differential Equations II:
           Stiff and Differential-Algebraic Problems", Sec. IV.8.
    .. [2] A. Curtis, M. J. D. Powell, and J. Reid, "On the estimation of
           sparse Jacobian matrices", Journal of the Institute of Mathematics
           and its Applications, 13, pp. 117-120, 1974.
    .. [3] J. de Swart, G. Söderlind, "On the construction of error estimators for 
           implicit Runge-Kutta methods", Journal of Computational and Applied 
           Mathematics, 86, pp. 347-358, 1997.
    .. [4] B. Fabien, "Analytical System Dynamics: Modeling and Simulation", 
           Sec. 5.3.5.
    .. [5] N. Guglielmi, E. Hairer, "Implementing Radau IIA Methods for Stiff 
           Delay Differential Equations", Computing 67, 1-12, 2001.
    .. [6] N. Guglielmi, "Open issues in devising software for the numerical 
           solution of implicit delay differential equations", Journal of 
           Computational and Applied Mathematics 185, 261-277, 2006.
    """
    def __init__(self, fun, t0, y0, yp0, t_bound, stages=4,
                 max_step=np.inf, rtol=1e-3, atol=1e-6, 
                 continuous_error_weight=0.0, jac=None, 
                 jac_sparsity=None, vectorized=False, 
                 first_step=None, newton_max_iter=None,
                 jac_recompute_rate=1e-3, newton_iter_embedded=1,
                 controller_deadband=(1.0, 1.2),
                 **extraneous):
        warn_extraneous(extraneous)
        super().__init__(fun, t0, y0, yp0, t_bound, rtol, atol, first_step, max_step, vectorized, jac, jac_sparsity)

        assert stages % 2 == 0
        self.stages = stages
        (
            self.A, self.A_inv, self.gammas, self.c, self.T, self.TI, self.P, self.P2, 
            self.b0, self.v, self.v2, self.b_hat, self.b_hat2, self.order,
        ) = radau_constants(stages)
        
        self.h_abs_old = None
        self.error_norm_old = None

        # modify tolerances as in radau.f line 824ff and 920ff
        # TODO: This rescaling leads to a saturation of the convergence
        EXPMNS = (stages + 1) / (2 * stages)
        # print(f"atol: {atol}")
        # print(f"rtol: {rtol}")
        # rtol = 0.1 * rtol ** EXPMNS
        # quott = atol / rtol
        # atol = rtol * quott
        # print(f"atol: {atol}")
        # print(f"rtol: {rtol}")

        # newton tolerance as in radau.f line 1008ff
        EXPMI = 1 / EXPMNS
        self.newton_tol = max(10 * EPS / rtol, min(0.03, rtol ** (EXPMI - 1)))
        # print(f"newton_tol: {self.newton_tol}")
        # print(f"10 * EPS / rtol: {10 * EPS / rtol}")
        # print(f"0.03")
        # print(f"rtol ** (EXPMI - 1): {rtol ** (EXPMI - 1)}")

        # maximum number of newton terations as in radaup.f line 234
        if newton_max_iter is None:
            newton_max_iter = 7 + int((stages - 3) * 2.5)
            newton_max_iter = 15 + int((stages - 4) * 2.5)

            # # newton_max_iter = 15 + int((stages - 4) * 2.5)
            # newton_max_iter = 15 + int((stages - 3) * 2.5)
        
        assert isinstance(newton_max_iter, int)
        assert newton_max_iter >= 1
        self.newton_max_iter = newton_max_iter

        assert 0 <= continuous_error_weight <= 1
        self.continuous_error_weight = continuous_error_weight

        assert 0 < jac_recompute_rate < 1
        self.jac_recompute_rate = jac_recompute_rate

        assert 0 < controller_deadband[0] <= controller_deadband[1]
        self.controller_deadband = controller_deadband

        assert 0 <= newton_iter_embedded
        self.newton_iter_embedded = newton_iter_embedded

        self.sol = None
        self.current_jac = True
        self.LUs = None
        self.Z = None
        self.Y = None
        self.Yp = None

    def _step_impl(self):
        t = self.t
        y = self.y
        yp = self.yp

        s = self.stages
        gammas = self.gammas
        c = self.c
        T = self.T
        TI = self.TI
        A = self.A
        A_inv = self.A_inv
        v = self.v
        v2 = self.v2
        b0 = self.b0
        P2 = self.P2

        max_step = self.max_step
        atol = self.atol
        rtol = self.rtol
        newton_tol = self.newton_tol
        newton_max_iter = self.newton_max_iter

        min_step = 10 * np.abs(np.nextafter(t, self.direction * np.inf) - t)
        if self.h_abs > max_step:
            h_abs = max_step
            h_abs_old = None
            error_norm_old = None
        elif self.h_abs < min_step:
            h_abs = min_step
            h_abs_old = None
            error_norm_old = None
        else:
            h_abs = self.h_abs
            h_abs_old = self.h_abs_old
            error_norm_old = self.error_norm_old

        Jy = self.Jy
        Jyp = self.Jyp
        LUs = self.LUs

        current_jac = self.current_jac
        jac = self.jac

        factor = None
        step_accepted = False
        message = None
        while not step_accepted:
            if h_abs < min_step:
                return False, self.TOO_SMALL_STEP

            h = h_abs * self.direction
            t_new = t + h

            if self.direction * (t_new - self.t_bound) > 0:
                t_new = self.t_bound

            h = t_new - t
            h_abs = np.abs(h)

            if self.sol is None:
                if UNKNOWN_VELOCITIES:
                    Yp0 = np.zeros((s, y.shape[0]))
                else:
                    Z0 = np.zeros((s, y.shape[0]))
            else:
                if UNKNOWN_VELOCITIES:
                    Yp0 = self.sol(t + h * c)[1].T
                else:
                    Z0 = self.sol(t + h * c)[0].T - y
            scale = atol + np.abs(y) * rtol

            converged = False
            while not converged:
                if LUs is None:
                    if UNKNOWN_VELOCITIES:
                        LUs = [self.lu(Jyp + h * ga * Jy) for ga in gammas]
                    else:
                        # LUs = [self.lu(ga / h * Jyp + Jy) for ga in gammas]
                        LUs = [self.lu(1 / (h * ga) * Jyp + Jy) for ga in gammas]

                if UNKNOWN_VELOCITIES:
                    converged, n_iter, Y, Yp, Z, rate = solve_collocation_system2(
                        self.fun, t, y, h, Yp0, scale, newton_tol, LUs, 
                        self.solve_lu, c, T, TI, A, newton_max_iter)
                else:
                    converged, n_iter, Y, Yp, Z, rate = solve_collocation_system(
                        self.fun, t, y, h, Z0, scale, newton_tol, LUs, 
                        self.solve_lu, c, T, TI, A, A_inv, newton_max_iter)

                if not converged:
                    if current_jac:
                        break

                    Jy, Jyp = self.jac(t, y, yp)
                    current_jac = True
                    LUs = None

            if not converged:
                h_abs *= 0.5
                LUs = None
                continue

            y_new = Y[-1]
            yp_new = Yp[-1]

            scale = atol + np.maximum(np.abs(y), np.abs(y_new)) * rtol

            # error of collocation polynomial of order s
            error_collocation = y - P2[0] @ Y

            # embedded error measures
            if self.newton_iter_embedded == 0:
                # explicit embedded method with R(z) = +oo for z -> oo
                if UNKNOWN_VELOCITIES:
                    error_embedded = h * (yp * gammas[-1] + v2 @ Yp)
                else:
                    error_embedded = h * (yp * gammas[-1] + v2 @ Yp)
            elif self.newton_iter_embedded == 1:
                # compute implicit embedded method with a single Newton iteration;
                # R(z) = b_hat1 / b_hats2 = DAMPING_RATIO_ERROR_ESTIMATE for z -> oo
                yp_hat_new = (v @ Yp - b0 * yp) / gammas[-1]
                F = self.fun(t_new, y_new, yp_hat_new)
                if UNKNOWN_VELOCITIES:
                    error_embedded = -h * gammas[-1] * self.solve_lu(LUs[-1], F)
                else:
                    error_embedded = -self.solve_lu(LUs[-1], F)
            else:
                # compute implicit embedded method with `newton_iter_embedded`` iterations
                if UNKNOWN_VELOCITIES:
                    raise NotImplementedError
                else:
                    y_hat_new = y_new.copy() # initial guess
                    for _ in range(self.newton_iter_embedded):
                        yp_hat_new = (
                            (y_hat_new - y) / h
                            - b0 * yp
                            - self.b_hat @ Yp
                        ) / gammas[-1]
                        F = self.fun(t_new, y_hat_new, yp_hat_new)
                        y_hat_new -= self.solve_lu(LUs[-1], F)

                    error_embedded = y_hat_new - y_new 

            # mix embedded error with collocation error as proposed in Guglielmi2001/ Guglielmi2003
            error = (
                self.continuous_error_weight * np.abs(error_collocation)**((s + 1) / s) 
                + (1 - self.continuous_error_weight) * np.abs(error_embedded)
            )
            error_norm = norm(error / scale)

            safety = 0.9 * (2 * newton_max_iter + 1) / (2 * newton_max_iter + n_iter)

            if error_norm > 1:
                factor = predict_factor(h_abs, h_abs_old, error_norm, error_norm_old, s)
                h_abs *= safety * factor
                LUs = None
            else:
                step_accepted = True

        # Step is converged and accepted
        recompute_jac = (
            jac is not None 
            and n_iter > 2 
            and rate > self.jac_recompute_rate
        )

        if factor is None:
            factor = predict_factor(h_abs, h_abs_old, error_norm, error_norm_old, s)
            factor = safety * factor

        # do not alter step-size in deadband
        if (
            not recompute_jac 
            and self.controller_deadband[0] <= factor <= self.controller_deadband[1]
        ):
            factor = 1
        else:
            LUs = None

        if recompute_jac:
            Jy, Jyp = self.jac(t_new, y_new, yp_new)
            current_jac = True
        elif jac is not None:
            current_jac = False

        self.h_abs_old = self.h_abs
        self.error_norm_old = error_norm

        self.h_abs = h_abs * factor

        self.y_old = y
        self.yp_old = yp

        self.t = t_new
        self.y = y_new
        self.yp = yp_new

        self.Z = Z
        self.Y = Y
        self.Yp = Yp

        self.LUs = LUs
        self.current_jac = current_jac
        self.Jy = Jy
        self.Jyp = Jyp

        self.t_old = t
        self.sol = self._compute_dense_output()

        return step_accepted, message

    def _compute_dense_output(self):
        # Q = np.dot(self.Z.T, self.P)
        # h = self.t - self.t_old
        # Yp = (self.A_inv / h) @ self.Z
        Z = self.Y - self.y_old
        Q = np.dot(Z.T, self.P)
        Zp = self.Yp - self.yp_old
        Qp = np.dot(Zp.T, self.P)
        return RadauDenseOutput(self.t_old, self.t, self.y_old, Q, self.yp_old, Qp)

    def _dense_output_impl(self):
        return self.sol


class RadauDenseOutput(DAEDenseOutput):
    def __init__(self, t_old, t, y_old, Q, yp_old, Qp):
        super().__init__(t_old, t)
        self.h = t - t_old
        self.Q = Q
        self.Qp = Qp
        self.order = Q.shape[1] - 1
        self.y_old = y_old
        self.yp_old = yp_old

    def _call_impl(self, t):
        x = (t - self.t_old) / self.h
        x = np.atleast_1d(x)

        # factors for interpolation polynomial and its derivative
        c = np.arange(1, self.order + 2)[:, None]
        p = x**c
        dp = (c / self.h) * (x**(c - 1))

        # TODO: This seems to be a better initial guess
        # 1. compute derivative of interpolation polynomial for y
        y = np.dot(self.Q, p)
        y += self.y_old[:, None]
        yp = np.dot(self.Q, dp)

        # # 2. compute collocation polynomial for y and yp
        # y = np.dot(self.Q, p)
        # yp = np.dot(self.Qp, p)
        # y += self.y_old[:, None]
        # yp += self.yp_old[:, None]

        # # 3. compute both values by Horner's rule
        # y = np.zeros_like(y)
        # yp = np.zeros_like(y)
        # for i in range(self.order, -1, -1):
        #     y = self.Q[:, i][:, None] + y * x[None, :]
        #     yp = y + yp * x[None, :]
        # y = self.y_old[:, None] + y * x[None, :]
        # yp /= self.h

        if t.ndim == 0:
            y = np.squeeze(y)
            yp = np.squeeze(yp)

        return y, yp
