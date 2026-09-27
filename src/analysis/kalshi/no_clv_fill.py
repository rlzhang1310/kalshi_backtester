"""Independent three-input price-reaching model; inference order is [p, t, b].

The zero-bid boundary is an assumption, never a fabricated training label.
Historical preparation and the production CLV estimator are unchanged.
"""

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd
from scipy.interpolate import NdBSpline
from scipy.optimize import minimize
from scipy.special import softmax
from scipy.sparse import csr_array

from .fill_surface import (
    BID_BASES,
    CONDITION_BASES,
    CONDITION_KNOTS,
    DEGREE,
    RAW_BIDS,
    _BID_PENALTY,
    bid_basis,
    bid_coordinate,
    log_odds,
)

MODEL_VERSION = "no-clv-1"
FEATURES = ["current_price", "normalized_time", "bid"]
SHAPE = (CONDITION_BASES**2, BID_BASES + 2)
DEFAULT_STRENGTHS = (1e-5, 1e-5, 1e-5)  # price, time, bid
CANDIDATES = [(s, s, s) for s in (1e-7, 1e-5, 1e-3)] + [
    (1e-5, 1e-7, 1e-7),
    (1e-7, 1e-5, 1e-7),
]
_CURVES = bid_basis(np.linspace(0, 1, 81), "assumed")


def points_array(points):
    values = np.asarray(points, dtype=float)
    if values.size == 0:
        return values.reshape(0, 3)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError(
            "No-CLV inputs must have feature order [current_price, normalized_time, bid]."
        )
    if not np.isfinite(values).all() or ((values < 0) | (values > 1)).any():
        raise ValueError(
            "No-CLV inputs must be finite probabilities in [0, 1]; convert cents once."
        )
    return values


def empirical_bins(frame, outcome="optimistic"):
    """Pool all CLVs at observation level, retaining hits and state trials.

    probability retains the production estimator's equal weight per event;
    state_probability supplies actual hits/trials for the fitted binomial model.
    Missing future prints remain misses, exactly as in the production target.
    """
    if outcome not in ("optimistic", "conservative"):
        raise ValueError("Choose optimistic or conservative.")
    columns = FEATURES + [
        "probability",
        "state_probability",
        "event_rate_sum",
        "n_events",
        "n_observations",
        "n_hits",
        "n_missing_future",
        "synthetic",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = frame.copy()
    coords, keys = FEATURES[:2], ["p_bin", "t_bin"]
    frame[keys] = np.rint(frame[coords].to_numpy() * 10).astype(int)
    rows = []
    for bid in RAW_BIDS[:-1]:
        peers = frame.loc[frame.current_price > bid].copy()
        if peers.empty:
            continue
        threshold = round(bid * 100, 8)
        peers["hit"] = (
            peers.future_min_cents.le(threshold)
            if outcome == "optimistic"
            else peers.future_min_cents.lt(threshold)
        )
        peers["missing"] = peers.future_min_cents.isna()
        events = peers.groupby(keys + ["event_ticker"]).agg(
            rate=("hit", "mean"),
            observations=("hit", "size"),
            hits=("hit", "sum"),
            missing=("missing", "sum"),
        )
        pooled = (
            events.groupby(keys)
            .agg(
                probability=("rate", "mean"),
                event_rate_sum=("rate", "sum"),
                n_events=("rate", "size"),
                n_observations=("observations", "sum"),
                n_hits=("hits", "sum"),
                n_missing_future=("missing", "sum"),
            )
            .join(peers.groupby(keys)[coords].mean())
            .reset_index(drop=True)
        )
        pooled["state_probability"] = pooled.n_hits / pooled.n_observations
        pooled["bid"], pooled["synthetic"] = float(bid), False
        rows.append(pooled)
    return (
        pd.concat(rows, ignore_index=True)[columns]
        if rows
        else pd.DataFrame(columns=columns)
    )


def basis(points):
    coords = np.asarray(points, dtype=float).copy()
    limit = float(log_odds(1))
    coords[:, 0] = (log_odds(coords[:, 0]) + limit) / (2 * limit)
    design = NdBSpline.design_matrix(
        np.ascontiguousarray(coords), (CONDITION_KNOTS,) * 2, DEGREE
    )
    return csr_array(
        (design.data, design.indices, design.indptr), shape=(len(coords), SHAPE[0])
    )


def predict(weights, points):
    """Exact fitted G, even at unsupported points; evaluate() adds coverage masks."""
    points = points_array(points)
    p, b = points[:, 0], points[:, 2]
    increments = bid_basis(bid_coordinate(b, p), "assumed")
    learned = np.sum((basis(points[:, :2]) @ weights[:, :-1]) * increments, axis=1)
    return np.clip(1 - p + p * learned, 0, 1)


def bending(weights, strengths):
    """Separate price, time and bid roughness, in spline coordinates."""
    active = weights[:, :-1]
    loss = strengths[2] * np.sum(active * (active @ _BID_PENALTY)) / len(active)
    gradient = np.zeros_like(weights)
    gradient[:, :-1] = strengths[2] * 2 * (active @ _BID_PENALTY) / len(active)
    curves = (active @ _CURVES.T).reshape((CONDITION_BASES,) * 2 + (len(_CURVES),))
    grad = np.zeros_like(curves)
    for axis in range(2):
        delta = np.diff(curves, n=2, axis=axis)
        scale = strengths[axis] * (CONDITION_BASES - 1) ** 4 / delta.size
        loss += np.sum(delta**2) * scale
        for offset, multiplier in enumerate((1, -2, 1)):
            indices = [slice(None)] * 3
            indices[axis] = slice(offset, offset + curves.shape[axis] - 2)
            grad[tuple(indices)] += 2 * multiplier * delta * scale
    gradient[:, :-1] += grad.reshape(len(weights), -1) @ _CURVES
    return float(loss), gradient


def fit_weights(raw, strengths):
    data = raw.loc[raw.bid > 0]
    if data.empty:
        raise ValueError("No positive-bid training observations are available.")
    design = basis(data[FEATURES[:2]].to_numpy())
    price = data.current_price.to_numpy()
    increments = bid_basis(bid_coordinate(data.bid.to_numpy(), price), "assumed")
    observed = data.state_probability.to_numpy()
    weight = data.n_observations.to_numpy(dtype=float)
    weight /= weight.sum()

    def objective(flat):
        mixing = softmax(flat.reshape(SHAPE), axis=1)
        predicted = (
            1 - price + price * np.sum((design @ mixing[:, :-1]) * increments, axis=1)
        )
        safe = np.clip(predicted, 1e-9, 1 - 1e-9)
        loss = -np.sum(
            weight * (observed * np.log(safe) + (1 - observed) * np.log1p(-safe))
        )
        derivative = weight * (safe - observed) / (safe * (1 - safe)) * price
        gradient = np.zeros(SHAPE)
        gradient[:, :-1] = design.T @ (derivative[:, None] * increments)
        penalty, penalty_grad = bending(mixing, strengths)
        gradient += penalty_grad
        logit_grad = mixing * (
            gradient - np.sum(gradient * mixing, axis=1, keepdims=True)
        )
        return loss + penalty, logit_grad.ravel()

    result = minimize(
        objective,
        np.zeros(np.prod(SHAPE)),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 500, "ftol": 1e-9, "gtol": 1e-6, "maxcor": 15},
    )
    return softmax(result.x.reshape(SHAPE), axis=1), bool(result.success)


def whole_game_split(frame):
    held = frame.event_ticker.map(
        lambda event: int(hashlib.sha256(event.encode()).hexdigest()[:8], 16) % 5 == 0
    )
    return frame.loc[~held], frame.loc[held]


def score_predictions(observed, predicted, counts):
    observed, predicted, counts = map(np.asarray, (observed, predicted, counts))
    if not len(observed):
        return None
    safe = np.clip(predicted, 1e-9, 1 - 1e-9)
    return dict(
        brier=float(
            np.average(
                predicted**2 - 2 * predicted * observed + observed, weights=counts
            )
        ),
        log_loss=float(
            np.average(
                -observed * np.log(safe) - (1 - observed) * np.log1p(-safe),
                weights=counts,
            )
        ),
        trial_weight=int(counts.sum()),
    )


def validation_metrics(raw, predicted):
    observed, counts = raw.state_probability.to_numpy(), raw.n_observations.to_numpy()
    metrics = score_predictions(observed, predicted, counts)
    for field, values in [
        ("price_buckets", raw.current_price.to_numpy()),
        ("time_buckets", raw.normalized_time.to_numpy()),
        ("calibration", predicted),
    ]:
        buckets = np.minimum((values * 10).astype(int), 9)
        metrics[field] = []
        for bucket in range(10):
            mask = buckets == bucket
            if mask.any():
                metrics[field].append(
                    dict(
                        lower=bucket / 10,
                        upper=(bucket + 1) / 10,
                        predicted=float(
                            np.average(predicted[mask], weights=counts[mask])
                        ),
                        observed=float(
                            np.average(observed[mask], weights=counts[mask])
                        ),
                        **score_predictions(
                            observed[mask], predicted[mask], counts[mask]
                        ),
                    )
                )
    return metrics


@dataclass
class NoClvModel:
    weights: np.ndarray
    raw: pd.DataFrame
    source: pd.DataFrame
    strengths: tuple
    validation: dict
    outcome: str
    version: str = MODEL_VERSION

    def predict(self, points):
        return predict(self.weights, points)

    def evaluate(self, points, tolerances=(0.05, 0.025), minimum=20):
        points = points_array(points)
        if len(tolerances) != 2 or any(
            not np.isfinite(v) or not 0 <= v <= 1 for v in tolerances
        ):
            raise ValueError("Specify price and time tolerances in [0,1].")
        if not isinstance(minimum, int) or minimum < 1:
            raise ValueError("Minimum support must be a positive integer.")
        fitted = self.predict(points)
        counts = np.zeros(len(points), dtype=int)
        observations = np.zeros(len(points), dtype=int)
        source = self.source[FEATURES[:2]].to_numpy()
        events = pd.factorize(self.source.event_ticker)[0]
        coords, inverse = np.unique(points[:, :2], axis=0, return_inverse=True)
        for i, coord in enumerate(coords):
            nearby = np.all(
                np.abs(source - coord) <= np.asarray(tolerances) + 1e-12, axis=1
            )
            prices = source[nearby, 0]
            ids = events[nearby]
            if not len(prices):
                continue
            maxima = np.full(events.max() + 1, -np.inf)
            np.maximum.at(maxima, ids, prices)
            sorted_prices, sorted_maxima = (
                np.sort(prices),
                np.sort(maxima[np.isfinite(maxima)]),
            )
            indices = np.flatnonzero(inverse == i)
            bids = points[indices, 2]
            observations[indices] = len(prices) - np.searchsorted(
                sorted_prices, bids, side="right"
            )
            counts[indices] = len(sorted_maxima) - np.searchsorted(
                sorted_maxima, bids, side="right"
            )
        domain = (points[:, 2] < points[:, 0]) & (points[:, 1] < 1)
        supported = domain & (counts >= minimum)
        synthetic = points[:, 2] == 0
        status = np.where(
            ~domain,
            "outside horizon/domain",
            np.where(
                counts == 0, "unsupported", np.where(supported, "supported", "sparse")
            ),
        )
        status[synthetic] = "assumed boundary"
        fitted[~supported & ~synthetic] = np.nan
        return dict(
            probability=fitted,
            n_events=counts,
            n_observations=observations,
            supported=supported,
            synthetic=synthetic,
            status=status,
        )

    def empirical(self, points, tolerances=(0.05, 0.025)):
        """Equal-event local historical rate, pooled over every pregame price."""
        points = points_array(points)
        rows = []
        for price, time, bid in points:
            peers = self.source.loc[
                self.source.current_price.sub(price).abs().le(tolerances[0] + 1e-12)
                & self.source.normalized_time.sub(time).abs().le(tolerances[1] + 1e-12)
                & self.source.current_price.gt(bid)
            ]
            if time == 1 or bid >= price:
                peers = peers.iloc[:0]
            threshold = round(bid * 100, 8)
            hits = (
                peers.future_min_cents.le(threshold)
                if self.outcome == "optimistic"
                else peers.future_min_cents.lt(threshold)
            )
            rates = hits.groupby(peers.event_ticker).mean()
            rows.append(
                dict(
                    current_price=price,
                    normalized_time=time,
                    bid=bid,
                    probability=float(rates.mean()) if len(rates) else np.nan,
                    n_events=len(rates),
                    n_observations=len(peers),
                    n_hits=int(hits.sum()),
                    n_missing_future=int(peers.future_min_cents.isna().sum()),
                    synthetic=False,
                )
            )
        return pd.DataFrame(rows)


def fit_model(frame, outcome="optimistic", smoothing="auto"):
    if frame.empty:
        raise ValueError("No reference observations remain after filtering.")
    # CLV is discarded, not replaced by a constant or used for membership.
    source = frame.drop(columns=["clv"], errors="ignore").copy()
    points_array(
        np.column_stack([source[FEATURES[:2]].to_numpy(), np.zeros(len(source))])
    )
    raw = empirical_bins(source, outcome)
    if not (raw.bid > 0).any():
        raise ValueError("No positive-bid training observations are available.")
    automatic = isinstance(smoothing, str) and smoothing == "auto"
    chosen = DEFAULT_STRENGTHS if automatic else tuple(float(v) for v in smoothing)
    if len(chosen) != 3 or any(not np.isfinite(v) or v < 0 for v in chosen):
        raise ValueError(
            "Specify finite nonnegative smoothing for price, time and bid."
        )
    validation = dict(
        status="unvalidated",
        reason="At least 30 games, 10 training games and 5 held-out games are required.",
    )
    train, test = whole_game_split(source)
    if (
        source.event_ticker.nunique() >= 30
        and train.event_ticker.nunique() >= 10
        and test.event_ticker.nunique() >= 5
    ):
        train_raw, test_raw = (
            empirical_bins(train, outcome),
            empirical_bins(test, outcome),
        )
        scored = test_raw.loc[test_raw.bid > 0]
        if (train_raw.bid > 0).any() and not scored.empty:
            candidates = []
            for strengths in CANDIDATES if automatic else [chosen]:
                weights, converged = fit_weights(train_raw, strengths)
                estimates = predict(weights, scored[FEATURES].to_numpy())
                candidates.append(
                    dict(
                        strengths=strengths,
                        converged=converged,
                        **validation_metrics(scored, estimates),
                    )
                )
            stable = [c for c in candidates if c["converged"]]
            winner = min(stable or candidates, key=lambda c: c["log_loss"])
            chosen = tuple(winner["strengths"])
            validation = dict(
                status="whole-game holdout",
                train_events=int(train.event_ticker.nunique()),
                holdout_events=int(test.event_ticker.nunique()),
                candidates=candidates,
                selected_metrics=winner,
                note="Selection holdout; repeated states/bids are dependent. Boundary rows excluded. Not an independent final test.",
            )
    weights, converged = fit_weights(raw, chosen)
    validation.update(
        optimizer_converged=converged,
        strengths=chosen,
        model_version=MODEL_VERSION,
        feature_order=FEATURES,
        weighting="Binomial state hits/trials; local empirical readout weights each game equally.",
        boundary_conflict_bins=int(
            ((raw.bid > 0) & (raw.state_probability < 1 - raw.current_price)).sum()
        ),
    )
    return NoClvModel(weights, raw, source, chosen, validation, outcome)


def heatmap_grid(model, time, tolerances, minimum, resolution=51):
    prices, bids = np.linspace(0.01, 1, resolution), np.linspace(0, 0.99, resolution)
    p, b = np.meshgrid(prices, bids)
    result = model.evaluate(
        np.column_stack([p.ravel(), np.full(p.size, time), b.ravel()]),
        tolerances,
        minimum,
    )
    return dict(
        prices=prices, bids=bids, **{k: v.reshape(p.shape) for k, v in result.items()}
    )


def time_grid(model, price, tolerances, minimum, resolution=51):
    times, bids = np.linspace(0, 1, resolution), np.linspace(0, 0.99, resolution)
    t, b = np.meshgrid(times, bids)
    result = model.evaluate(
        np.column_stack([np.full(t.size, price), t.ravel(), b.ravel()]),
        tolerances,
        minimum,
    )
    return dict(
        times=times, bids=bids, **{k: v.reshape(t.shape) for k, v in result.items()}
    )
