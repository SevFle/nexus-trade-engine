"""Portfolio optimization primitives.

Pure-numpy implementations of four classic portfolio construction
techniques:

- :func:`mean_variance_optimization` — Markowitz min-variance / max-Sharpe
  closed-form solution.
- :func:`risk_parity` — equal-risk-contribution weights via fixed-point
  iteration.
- :func:`hierarchical_risk_parity` — Lopez de Prado's HRP (correlation
  -> distance -> linkage tree -> recursive bisection inverse-variance).
- :func:`black_litterman` — posterior return + covariance update from a
  prior plus investor views.

All functions operate on dense ``numpy.ndarray`` inputs. No bounds /
turnover / sector constraints — those layer on top in a follow-up
issue. Outputs are weight vectors that sum to 1; no leverage.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

_NDIM_MATRIX = 2
_PD_EIGVAL_FLOOR = 1e-12


class OptimizerError(Exception):
    """Raised when input matrices are malformed or numerically degenerate."""


def _validate_cov(cov: FloatArray) -> FloatArray:
    cov = np.asarray(cov, dtype=np.float64)
    if cov.ndim != _NDIM_MATRIX or cov.shape[0] != cov.shape[1]:
        msg = f"cov must be a square 2-D array; got shape {cov.shape}"
        raise OptimizerError(msg)
    if cov.shape[0] == 0:
        msg = "cov must have at least one asset"
        raise OptimizerError(msg)
    if not np.isfinite(cov).all():
        msg = "cov contains non-finite entries (NaN / Inf)"
        raise OptimizerError(msg)
    return cov


def _safe_inv(cov: FloatArray) -> FloatArray:
    try:
        return np.linalg.inv(cov).astype(np.float64, copy=False)
    except np.linalg.LinAlgError as exc:
        msg = f"cov is singular and cannot be inverted: {exc}"
        raise OptimizerError(msg) from exc


def mean_variance_optimization(
    *,
    cov: FloatArray,
    expected_returns: FloatArray | None = None,
) -> FloatArray:
    """Markowitz mean-variance optimization (closed-form).

    Without ``expected_returns``: solves ``min w' Σ w`` subject to
    ``sum(w) = 1``. Closed form: ``w = Σ⁻¹ 1 / (1' Σ⁻¹ 1)``.

    With ``expected_returns``: solves ``max w' μ - λ w' Σ w`` for the
    tangency / max-Sharpe portfolio: ``w ∝ Σ⁻¹ μ`` rescaled to sum to 1.

    No long-only constraint, no leverage cap. Both layer separately
    in the constrained-optimization follow-up.
    """
    cov = _validate_cov(cov)
    inv = _safe_inv(cov)
    n = cov.shape[0]
    if expected_returns is None:
        ones = np.ones(n)
        unscaled = inv @ ones
    else:
        mu = np.asarray(expected_returns, dtype=np.float64)
        if mu.shape != (n,):
            msg = f"expected_returns shape {mu.shape} does not match cov shape ({n},{n})"
            raise OptimizerError(msg)
        if not np.isfinite(mu).all():
            msg = "expected_returns contains non-finite entries (NaN / Inf)"
            raise OptimizerError(msg)
        unscaled = inv @ mu
    total = unscaled.sum()
    if not np.isfinite(total) or total == 0.0:
        msg = "MVO produced a degenerate weight vector (sum=0 or non-finite)"
        raise OptimizerError(msg)
    return unscaled / total


def risk_parity(
    *,
    cov: FloatArray,
    max_iter: int = 1000,
    tol: float = 1e-8,
) -> FloatArray:
    """Equal-risk-contribution weights via Spinu fixed-point.

    Each asset contributes the same share of total portfolio variance:
    ``w_i * (Σw)_i`` is equal across all i. Multiplicative-update
    fixed-point converges to the unique positive solution when Σ is
    positive-definite.
    """
    cov = _validate_cov(cov)
    n = cov.shape[0]
    eigvals = np.linalg.eigvalsh(cov)
    if eigvals.min() <= _PD_EIGVAL_FLOOR:
        msg = "cov is not positive-definite; risk parity requires PD"
        raise OptimizerError(msg)
    w = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        sigma_w = cov @ w
        # Guard against pathological covariances where sigma_w * w can
        # turn non-positive (PD cov with negative off-diagonals).
        if (sigma_w <= 0).any():
            msg = (
                "risk parity iteration produced non-positive marginal "
                "contributions; covariance is not diagonally dominant "
                "enough for the Spinu fixed-point"
            )
            raise OptimizerError(msg)
        # Spinu fixed-point: w_{k+1,i} = sqrt(w_{k,i} / (Σ w_k)_i),
        # then renormalize. Converges to equal-risk-contribution for
        # diagonally-dominant Σ.
        new_w = np.sqrt(w / sigma_w)
        new_w = new_w / new_w.sum()
        if np.linalg.norm(new_w - w, ord=np.inf) < tol:
            return new_w
        w = new_w
    msg = f"risk parity did not converge in {max_iter} iterations (tol={tol})"
    raise OptimizerError(msg)


def _correlation_from_cov(cov: FloatArray) -> FloatArray:
    std = np.sqrt(np.diag(cov))
    outer = np.outer(std, std)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(outer > 0, cov / outer, 0.0)
    return np.clip(corr, -1.0, 1.0)


def _correlation_distance(corr: FloatArray) -> FloatArray:
    return np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, 1.0))


def _quasi_diag_order(cov: FloatArray) -> list[int]:
    """Single-linkage hierarchical-clustering quasi-diagonal leaf order.

    Simple agglomerative algorithm; no scipy dependency. Repeatedly
    merges the two clusters whose minimum pairwise correlation-distance
    is smallest, returning the resulting leaf ordering.
    """
    n = cov.shape[0]
    if n <= 1:
        return list(range(n))
    corr = _correlation_from_cov(cov)
    dist = _correlation_distance(corr)
    clusters: list[list[int]] = [[i] for i in range(n)]
    while len(clusters) > 1:
        best = (np.inf, -1, -1)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d_ij = min(dist[a, b] for a in clusters[i] for b in clusters[j])
                if d_ij < best[0]:
                    best = (d_ij, i, j)
        _, i, j = best
        merged = clusters[i] + clusters[j]
        clusters.pop(j)
        clusters.pop(i)
        clusters.append(merged)
    return clusters[0]


def _ivp_weights(cov_block: FloatArray) -> FloatArray:
    ivp = 1.0 / np.diag(cov_block)
    return ivp / ivp.sum()


def _cluster_var(cov: FloatArray, indices: list[int]) -> float:
    block = cov[np.ix_(indices, indices)]
    w = _ivp_weights(block)
    return float(w @ block @ w)


def _recursive_bisection(cov: FloatArray, order: list[int]) -> FloatArray:
    n = cov.shape[0]
    weights = np.ones(n)
    stack: list[list[int]] = [order]
    while stack:
        cluster = stack.pop()
        if len(cluster) <= 1:
            continue
        mid = len(cluster) // 2
        left, right = cluster[:mid], cluster[mid:]
        var_left = _cluster_var(cov, left)
        var_right = _cluster_var(cov, right)
        alpha = 1.0 - var_left / (var_left + var_right)
        for idx in left:
            weights[idx] *= alpha
        for idx in right:
            weights[idx] *= 1.0 - alpha
        stack.extend([left, right])
    return weights


def hierarchical_risk_parity(*, cov: FloatArray) -> FloatArray:
    """Lopez de Prado HRP weights.

    Pipeline: (1) correlation-distance hierarchical clustering for a
    quasi-diagonal asset ordering, (2) recursive bisection along that
    ordering, (3) inverse-variance allocation within each split.

    Robust to ill-conditioned Σ — never inverts the full matrix; only
    diagonals of sub-blocks are touched.
    """
    cov = _validate_cov(cov)
    order = _quasi_diag_order(cov)
    weights = _recursive_bisection(cov, order)
    total = weights.sum()
    if not np.isfinite(total) or total <= 0:
        msg = "HRP produced a degenerate weight vector"
        raise OptimizerError(msg)
    return weights / total


def black_litterman(
    *,
    prior_returns: FloatArray,
    prior_cov: FloatArray,
    views_p: FloatArray,
    views_q: FloatArray,
    view_uncertainty: FloatArray,
    tau: float = 0.05,
) -> tuple[FloatArray, FloatArray]:
    """Black-Litterman posterior returns + covariance.

    Parameters
    ----------
    prior_returns
        Equilibrium / market-implied excess returns ``π``, shape ``(n,)``.
    prior_cov
        Asset covariance ``Σ``, shape ``(n, n)``.
    views_p
        View pick matrix ``P``, shape ``(k, n)``.
    views_q
        View target returns ``Q``, shape ``(k,)``.
    view_uncertainty
        View-uncertainty matrix ``Ω``, shape ``(k, k)``.
    tau
        Scalar uncertainty in the prior. Standard practice: 0.025-0.10.

    Returns
    -------
    (posterior_mu, posterior_cov)
        Blended posterior expected returns and covariance.
    """
    if tau <= 0:
        msg = f"tau must be positive; got {tau}"
        raise OptimizerError(msg)
    cov = _validate_cov(prior_cov)
    pi = np.asarray(prior_returns, dtype=np.float64)
    if pi.shape != (cov.shape[0],):
        msg = f"prior_returns shape {pi.shape} does not match prior_cov ({cov.shape[0]},)"
        raise OptimizerError(msg)
    p = np.asarray(views_p, dtype=np.float64).reshape(-1, cov.shape[0])
    q = np.asarray(views_q, dtype=np.float64).reshape(-1)
    omega = np.asarray(view_uncertainty, dtype=np.float64)

    if p.shape[0] == 0:
        return pi.copy(), cov.copy()

    omega = omega.reshape(p.shape[0], p.shape[0])
    tau_cov = tau * cov
    inv_tau_cov = _safe_inv(tau_cov)
    inv_omega = _safe_inv(omega)
    posterior_precision = inv_tau_cov + p.T @ inv_omega @ p
    posterior_cov_overlay = _safe_inv(posterior_precision)
    posterior_mu = posterior_cov_overlay @ (inv_tau_cov @ pi + p.T @ inv_omega @ q)
    posterior_cov_full = cov + posterior_cov_overlay
    return posterior_mu, posterior_cov_full


__all__ = [
    "OptimizerError",
    "black_litterman",
    "hierarchical_risk_parity",
    "mean_variance_optimization",
    "risk_parity",
]
