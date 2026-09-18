"""Research-only, boundary-constrained four-dimensional fill smoother.

The production estimator remains in fill_probability.py. No synthetic anchor
is inserted into the empirical dataset or validation set.
"""

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.interpolate import BSpline, NdBSpline
from scipy.special import softmax
from scipy.sparse import csr_array

from .fill_probability import normalize_family, utc

MODEL_VERSION = 2
DEGREE = 2
CONDITION_KNOTS = np.r_[np.zeros(3), [0.25, 0.5, 0.75], np.ones(3)]
CONDITION_BASES = len(CONDITION_KNOTS) - DEGREE - 1
BID_KNOTS = np.r_[np.zeros(3), [0.1, 0.25, 0.45, 0.65, 0.8, 0.9, 0.97], np.ones(3)]
BID_BASES = len(BID_KNOTS) - DEGREE - 1
_BID_INTEGRAL = BSpline(BID_KNOTS, np.eye(BID_BASES), DEGREE).antiderivative()
_BID_MASS = _BID_INTEGRAL(1) - _BID_INTEGRAL(0)
RAW_BIDS = np.arange(0, 101, 5) / 100


def reference_data(snapshots, family, ticker=None, as_of=None):
    """Preserve original state observations, including zero prices and NaNs."""
    frame = snapshots.loc[
        snapshots.event_ticker.str.startswith(normalize_family(family) + "-")
    ].copy()
    if ticker:
        matches = frame.loc[frame.ticker.eq(ticker), "event_ticker"]
        event = matches.iloc[0] if len(matches) else "-".join(ticker.split("-")[:2])
        frame = frame.loc[frame.event_ticker.ne(event)]
    if as_of:
        cutoff = utc(as_of)
        if pd.isna(cutoff):
            raise ValueError("Enter a valid settlement cutoff timestamp.")
        frame = frame.loc[utc(frame.settlement_time) < cutoff]
    return frame.reset_index(drop=True)


def empirical_bins(frame, outcome="optimistic"):
    """Pool nearby states in 0.1 bins, with equal weight per event per bin.

    Event-averaged labels can be fractional: event_rate_sum is NOT an integer
    execution count. No-future-trade states count as misses, as in the estimator.
    """
    columns = [
        "clv",
        "current_price",
        "normalized_time",
        "bid",
        "probability",
        "event_rate_sum",
        "n_events",
        "n_observations",
        "n_hits",
        "synthetic",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = frame.copy()
    coords = ["clv", "current_price", "normalized_time"]
    keys = ["c_bin", "p_bin", "t_bin"]
    frame[keys] = np.rint(frame[coords].to_numpy() * 10).astype(int)
    rows = []
    for bid in RAW_BIDS[:-1]:
        peers = frame.loc[frame.current_price > bid].copy()
        if peers.empty:
            continue
        peers["hit"] = (
            peers.future_min_cents.le(round(bid * 100, 8))
            if outcome == "optimistic"
            else peers.future_min_cents.lt(round(bid * 100, 8))
        )
        events = peers.groupby(keys + ["event_ticker"], observed=True).agg(
            rate=("hit", "mean"), observations=("hit", "size"), hits=("hit", "sum")
        )
        grouped = events.groupby(keys).agg(
            probability=("rate", "mean"),
            event_rate_sum=("rate", "sum"),
            n_events=("rate", "size"),
            n_observations=("observations", "sum"),
            n_hits=("hits", "sum"),
        )
        locations = peers.groupby(keys)[coords].mean()
        grouped = grouped.join(locations).reset_index(drop=True)
        grouped["bid"], grouped["synthetic"] = float(bid), False
        rows.append(grouped)
    return (
        pd.concat(rows, ignore_index=True)[columns]
        if rows
        else pd.DataFrame(columns=columns)
    )


def basis(points):
    """Sparse local tensor B-spline basis; neighboring slices share coefficients."""
    design = NdBSpline.design_matrix(
        np.ascontiguousarray(np.clip(points, 0, 1), dtype=float),
        (CONDITION_KNOTS,) * 3,
        DEGREE,
    )
    # SciPy infers the number of columns from touched basis functions. Preserve
    # the full coefficient shape even for a single point in an interior cell.
    return csr_array(
        (design.data, design.indices, design.indptr),
        shape=(len(points), CONDITION_BASES**3),
    )


def bid_basis(relative_bids, boundary="empirical"):
    """Integrated nonnegative splines are monotone, with local bend locations.

    Relative bid is only a basis coordinate; the estimator/UI still take an
    absolute bid. It aligns near-current-price behavior across price levels.
    """
    r = np.clip(relative_bids, 0, 1)
    integrated = (_BID_INTEGRAL(r) - _BID_INTEGRAL(0)) / _BID_MASS
    intercept = np.ones(len(r)) if boundary == "empirical" else np.zeros(len(r))
    return np.column_stack([intercept, np.clip(integrated, 0, 1)])


_PENALTY_GRID = np.linspace(0, 1, 81)
_CURVES = bid_basis(_PENALTY_GRID, "empirical")
_CURVATURE = np.diff(_CURVES, n=2, axis=0) / (_PENALTY_GRID[1] - _PENALTY_GRID[0]) ** 2
_BID_PENALTY = _CURVATURE.T @ _CURVATURE / len(_CURVATURE)


def bending(weights):
    """Penalize curve curvature, not similarity of mixture density weights.

    The old equal-weight penalty had an unintended straight-line attractor.
    Here bid derivatives use actual spacing. Other axes regularize neighboring
    control curves with a mean-scaled second-difference penalty.
    """
    active = weights[:, :-1]
    bid_gradient = 2 * (active @ _BID_PENALTY) / len(active)
    loss = float(np.sum(active * (active @ _BID_PENALTY)) / len(active))
    values = (active @ _CURVES.T).reshape(
        (CONDITION_BASES,) * 3 + (len(_PENALTY_GRID),)
    )
    grad = np.zeros_like(values)
    for axis in range(3):
        delta = np.diff(values, n=2, axis=axis)
        scale = (CONDITION_BASES - 1) ** 4 / delta.size
        loss += np.sum(delta**2) * scale
        for offset, multiplier in enumerate((1, -2, 1)):
            slices = [slice(None)] * 4
            slices[axis] = slice(offset, offset + values.shape[axis] - 2)
            grad[tuple(slices)] += 2 * multiplier * delta * scale
    result = np.zeros_like(weights)
    result[:, :-1] = bid_gradient + grad.reshape(len(weights), -1) @ _CURVES
    return loss, result


def fit_weights(raw, strength, boundary="empirical"):
    # Empirical mode learns from bid-zero observations. Only the optional
    # legacy assumption fixes their model value independently of coefficients.
    data = raw.loc[raw.bid > 0] if boundary == "assumed" else raw
    if data.empty:
        raise ValueError("No eligible empirical bid observations available to fit.")
    design = basis(data[["clv", "current_price", "normalized_time"]].to_numpy())
    price = data.current_price.to_numpy()
    bids = bid_basis(data.bid.to_numpy() / price, boundary)
    offset = 1 - price if boundary == "assumed" else np.zeros_like(price)
    amplitude = price if boundary == "assumed" else np.ones_like(price)
    observed = data.probability.to_numpy()
    weight = data.n_events.to_numpy(dtype=float)
    weight /= weight.sum()
    shape = (CONDITION_BASES**3, BID_BASES + 2)

    def objective(flat):
        mixing = softmax(flat.reshape(shape), axis=1)
        predicted = offset + amplitude * np.sum(
            (design @ mixing[:, :-1]) * bids, axis=1
        )
        safe = np.clip(predicted, 1e-9, 1 - 1e-9)
        loss = -np.sum(
            weight * (observed * np.log(safe) + (1 - observed) * np.log1p(-safe))
        )
        derivative = weight * (safe - observed) / (safe * (1 - safe)) * amplitude
        grad = np.zeros(shape)
        grad[:, :-1] = design.T @ (derivative[:, None] * bids)
        penalty, penalty_grad = bending(mixing)
        grad += strength * penalty_grad
        logit_grad = mixing * (grad - np.sum(grad * mixing, axis=1, keepdims=True))
        return loss + strength * penalty, logit_grad.ravel()

    result = minimize(
        objective,
        np.zeros(np.prod(shape)),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 500, "ftol": 1e-9, "gtol": 1e-6, "maxcor": 15},
    )
    return softmax(result.x.reshape(shape), axis=1), bool(result.success)


def predict(weights, points, boundary="empirical"):
    points = np.asarray(points, dtype=float).reshape(-1, 4)
    p = points[:, 1]
    relative = np.divide(points[:, 3], p, out=np.zeros_like(p), where=p > 0)
    learned = np.sum(
        (basis(points[:, :3]) @ weights[:, :-1]) * bid_basis(relative, boundary), axis=1
    )
    return np.clip(1 - p + p * learned if boundary == "assumed" else learned, 0, 1)


@dataclass
class SurfaceModel:
    weights: np.ndarray
    raw: pd.DataFrame
    source: pd.DataFrame
    strength: float
    validation: dict
    outcome: str
    boundary: str = "empirical"

    def evaluate(self, points, tolerances=(0.05, 0.05, 0.025), minimum=20):
        points = np.asarray(points, dtype=float).reshape(-1, 4)
        fitted = predict(self.weights, points, self.boundary)
        counts = np.zeros(len(points), dtype=int)
        observations = np.zeros(len(points), dtype=int)
        source_coords = self.source[
            ["clv", "current_price", "normalized_time"]
        ].to_numpy()
        events = pd.factorize(self.source.event_ticker)[0]
        # Reuse each CLV/price/time neighborhood for every bid on its section.
        coords, inverse = np.unique(points[:, :3], axis=0, return_inverse=True)
        for i, coord in enumerate(coords):
            nearby = np.all(
                np.abs(source_coords - coord) <= np.asarray(tolerances) + 1e-12, axis=1
            )
            indices = np.flatnonzero(nearby)
            for j in np.flatnonzero(inverse == i):
                eligible = indices[source_coords[indices, 1] > points[j, 3]]
                counts[j] = len(np.unique(events[eligible]))
                observations[j] = len(eligible)
        domain = np.all((points >= 0) & (points <= 1), axis=1)
        domain &= (points[:, 3] < points[:, 1]) & (points[:, 2] < 1)
        supported = domain & (counts >= minimum)
        fitted[~supported] = np.nan
        return dict(
            probability=fitted,
            n_events=counts,
            n_observations=observations,
            supported=supported,
            synthetic=(points[:, 3] == 0) & (self.boundary == "assumed"),
        )


def fit_surface(frame, outcome="optimistic", strength="auto", boundary="empirical"):
    if outcome not in ("optimistic", "conservative"):
        raise ValueError("Choose optimistic or conservative.")
    if frame.empty:
        raise ValueError("No reference observations remain after filtering.")
    if boundary not in ("assumed", "empirical"):
        raise ValueError("Choose an assumed or empirical boundary.")
    raw = empirical_bins(frame, outcome)
    validation = {
        "status": "unvalidated",
        "reason": "Fewer than 30 whole games available.",
    }
    chosen = 1e-5 if strength == "auto" else float(strength)
    if not np.isfinite(chosen) or chosen < 0:
        raise ValueError("Smoothing strength must be finite and nonnegative.")
    if frame.event_ticker.nunique() >= 30:
        # Stable whole-event assignment: both outcomes and every timestamp stay together.
        held = frame.event_ticker.map(
            lambda event: int(hashlib.sha256(event.encode()).hexdigest()[:8], 16) % 5
            == 0
        )
        train, test = frame.loc[~held], frame.loc[held]
        if train.event_ticker.nunique() >= 10 and test.event_ticker.nunique() >= 5:
            train_raw, test_raw = (
                empirical_bins(train, outcome),
                empirical_bins(test, outcome),
            )
            scored = (
                test_raw if boundary == "empirical" else test_raw.loc[test_raw.bid > 0]
            )
            scores = {}
            positive_scores = {}
            convergence = {}
            for candidate in [1e-7, 1e-5, 1e-3] if strength == "auto" else [chosen]:
                weights, converged = fit_weights(train_raw, candidate, boundary)
                convergence[candidate] = converged
                estimate = predict(
                    weights,
                    scored[
                        ["clv", "current_price", "normalized_time", "bid"]
                    ].to_numpy(),
                    boundary,
                )
                # Brier loss for Bernoulli labels aggregated to fractional event means.
                loss = (
                    estimate**2
                    - 2 * estimate * scored.probability.to_numpy()
                    + scored.probability.to_numpy()
                )
                scores[candidate] = float(np.average(loss, weights=scored.n_events))
                positive = scored.bid.to_numpy() > 0
                positive_scores[candidate] = (
                    float(
                        np.average(
                            loss[positive], weights=scored.n_events.to_numpy()[positive]
                        )
                    )
                    if positive.any()
                    else None
                )
            chosen = min(scores, key=scores.get)
            validation = dict(
                status="whole-game holdout",
                scores=scores,
                positive_bid_scores=positive_scores,
                candidate_convergence=convergence,
                train_events=int(train.event_ticker.nunique()),
                holdout_events=int(test.event_ticker.nunique()),
                note="Selection holdout Brier loss; not an independent final test or confidence interval.",
            )
    weights, converged = fit_weights(raw, chosen, boundary)
    validation["boundary"] = boundary
    validation["optimizer_converged"] = converged
    validation["boundary_conflict_bins"] = (
        int((raw.probability < 1 - raw.current_price - 1e-12).sum())
        if boundary == "assumed"
        else 0
    )
    return SurfaceModel(weights, raw, frame, chosen, validation, outcome, boundary)


def surface_grid(model, clv, time, tolerances, minimum, resolution=51):
    prices, bids = np.linspace(0.01, 1, resolution), np.linspace(0, 0.99, resolution)
    p, b = np.meshgrid(prices, bids)
    points = np.column_stack(
        [np.full(p.size, clv), p.ravel(), np.full(p.size, time), b.ravel()]
    )
    result = model.evaluate(points, tolerances, minimum)
    return dict(
        prices=prices,
        bids=bids,
        **{key: value.reshape(p.shape) for key, value in result.items()},
    )


def curvature(grid):
    z = grid["probability"]
    result = {}
    for name, axis, coords in [
        ("bid", 0, grid["bids"]),
        ("current_price", 1, grid["prices"]),
    ]:
        spacing = float(coords[1] - coords[0])
        derivative = np.diff(z, n=2, axis=axis) / spacing**2
        finite = np.isfinite(derivative)
        result[name] = float(np.mean(derivative[finite] ** 2)) if finite.any() else None
    return result
