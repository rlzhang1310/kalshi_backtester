"""Independent finite-price binomial surface with feature order [distance, time]."""

from dataclasses import dataclass
from functools import cached_property
import hashlib

import numpy as np
import pandas as pd
from scipy.interpolate import BSpline
from scipy.optimize import minimize
from scipy.spatial import cKDTree
from scipy.special import expit, logit

from .no_clv_fill import score_predictions

MODEL_VERSION = "odds-moneyness-1"
FEATURES = ["log_odds_distance", "normalized_time"]
PRICE_RANGE = (0.01, 0.99)
DEFAULT_STRENGTHS = (1e-4, 1e-4)
CANDIDATES = [(1e-6, 1e-6), (1e-4, 1e-4), (1e-2, 1e-2), (1e-4, 1e-6), (1e-6, 1e-4)]
BASES = (12, 9)


def distance(price, bid):
    p, b = np.broadcast_arrays(np.asarray(price, float), np.asarray(bid, float))
    if (
        not np.isfinite(p).all()
        or not np.isfinite(b).all()
        or ((p <= 0) | (p >= 1) | (b <= 0) | (b >= 1)).any()
    ):
        raise ValueError(
            "Odds moneyness requires finite 0 < price, bid < 1; convert cents once."
        )
    return logit(p) - logit(b)


def equivalent_bid(price, d):
    distance(price, price)  # Validate probability units, without clipping endpoints.
    if not np.isfinite(d).all():
        raise ValueError("Distance must be finite.")
    return expit(logit(price) - np.asarray(d))


def points_array(points):
    values = np.asarray(points, dtype=float)
    if values.size == 0:
        return values.reshape(0, 2)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.ndim != 2 or values.shape[1] != 2:
        raise ValueError(
            "Odds-moneyness inputs must have feature order [log_odds_distance, normalized_time]."
        )
    if not np.isfinite(values).all() or ((values[:, 1] < 0) | (values[:, 1] > 1)).any():
        raise ValueError("Inputs must be finite; normalized time must be in [0,1].")
    return values


def dataset(frame, outcome="optimistic"):
    """Actual future-price targets at positive cent bids; never zero-bid anchors.

    Prepared snapshots already use YES probabilities, including complemented
    NO-taker prints. Only strictly below-market bids are eligible, as elsewhere.
    Missing future prints are misses under the existing estimator's definition.
    """
    if outcome not in ("optimistic", "conservative"):
        raise ValueError("Choose optimistic or conservative.")
    required = ["current_price", "normalized_time", "future_min_cents", "event_ticker"]
    if missing := set(required).difference(frame.columns):
        raise ValueError(f"Missing source columns: {sorted(missing)}")
    source = frame[required + (["clv"] if "clv" in frame else [])].copy()
    p, t = source.current_price.to_numpy(float), source.normalized_time.to_numpy(float)
    if (
        (np.isfinite(p) & ((p < 0) | (p > 1))) | (np.isfinite(t) & ((t < 0) | (t > 1)))
    ).any():
        raise ValueError("Source prices and times use [0,1], never cents.")
    eligible = np.isfinite(p) & (p > 0) & (p < 1) & np.isfinite(t) & (t >= 0) & (t < 1)
    source = source.loc[eligible].copy()
    source["market"] = source.event_ticker.str.split("-").str[0]
    source["price_band"] = np.minimum((source.current_price * 5).astype(int), 4)
    source["t_bin"] = np.rint(source.normalized_time / 0.025).astype(int)
    keys = ["event_ticker", "market", "price_band", "d_bin", "t_bin"]
    means = FEATURES + ["current_price", "bid"]
    columns = keys + means + ["n_hits", "n_trials", "n_missing_future"]
    if "clv" in source:
        columns += ["clv", "clv_trials"]
    extent = [np.inf, -np.inf]
    rows = []
    for cents in range(1, 100):
        peers = source.loc[source.current_price > cents / 100].copy()
        if peers.empty:
            continue
        peers["bid"] = cents / 100
        peers[FEATURES[0]] = distance(peers.current_price, peers.bid)
        peers["n_hits"] = (
            peers.future_min_cents.le(cents)
            if outcome == "optimistic"
            else peers.future_min_cents.lt(cents)
        ).astype(int)
        peers["n_trials"] = 1
        peers["n_missing_future"] = peers.future_min_cents.isna().astype(int)
        peers["d_bin"] = np.rint(peers[FEATURES[0]] / 0.1).astype(int)
        extent[0] = min(extent[0], float(peers.log_odds_distance.min()))
        extent[1] = max(extent[1], float(peers.log_odds_distance.max()))
        if "clv" in peers:
            peers["clv_trials"] = peers.clv.notna().astype(int)
        rows.append(peers[columns])
        # Accumulate sufficient statistics in bounded chunks, without retaining
        # every repeated bid-state row for large families.
        if len(rows) >= 10:
            rows = [
                pd.concat(rows, ignore_index=True)
                .groupby(keys, observed=True)
                .sum()
                .reset_index()
            ]
    if not rows:
        raise ValueError("No eligible finite-price, positive below-market bids remain.")
    grouped = (
        pd.concat(rows, ignore_index=True)
        .groupby(keys, observed=True)
        .sum()
        .reset_index()
    )
    for field in means:
        grouped[field] /= grouped.n_trials
    grouped["probability"] = grouped.n_hits / grouped.n_trials
    if "clv" in grouped:
        grouped["clv"] /= grouped.clv_trials.replace(0, np.nan)
    audit = dict(
        source_states=len(frame),
        eligible_states=len(source),
        excluded_states=int((~eligible).sum()),
        endpoint_prices=int(((p == 0) | (p == 1)).sum()),
        invalid_prices=int((~np.isfinite(p)).sum()),
        ended_horizon=int((t == 1).sum()),
        bid_trials=int(grouped.n_trials.sum()),
        side="YES; upstream NO-taker prices complemented once",
        negative_distance="Excluded: production bids must be strictly below current price.",
        source_distance_range=extent,
    )
    return grouped, audit


def pooled_bins(rows):
    grouped = (
        rows.groupby(["d_bin", "t_bin"])
        .agg(
            n_hits=("n_hits", "sum"),
            n_trials=("n_trials", "sum"),
            n_events=("event_ticker", "nunique"),
        )
        .reset_index()
    )
    for field in FEATURES + ["current_price", "bid"]:
        sums = (rows[field] * rows.n_trials).groupby([rows.d_bin, rows.t_bin]).sum()
        grouped[field] = sums.to_numpy() / grouped.n_trials
    grouped["probability"] = grouped.n_hits / grouped.n_trials
    return grouped


def knots(size):
    return np.r_[np.zeros(4), np.linspace(0, 1, size - 2)[1:-1], np.ones(4)]


def design(points, bounds):
    points = points_array(points)
    coords = points.copy()
    coords[:, 0] = np.clip((coords[:, 0] - bounds[0]) / (bounds[1] - bounds[0]), 0, 1)
    d = BSpline.design_matrix(coords[:, 0], knots(BASES[0]), 3).toarray()
    t = BSpline.design_matrix(coords[:, 1], knots(BASES[1]), 3).toarray()
    return (d[:, :, None] * t[:, None, :]).reshape(len(points), -1)


def coefficients(parameters):
    params = parameters.reshape(BASES)
    return params[0][None, :] - np.vstack(
        [np.zeros(BASES[1]), np.cumsum(params[1:], axis=0)]
    )


def predictions(weights, points, bounds):
    points = points_array(points)
    result = np.empty(len(points))
    for start in range(0, len(points), 8192):
        end = min(start + 8192, len(points))
        result[start:end] = expit(design(points[start:end], bounds) @ weights.ravel())
    return result


def coefficient_gradient(gradient):
    result = -np.cumsum(gradient[::-1], axis=0)[::-1]
    result[0] = gradient.sum(axis=0)
    return result.ravel()


def fit_weights(raw, strengths, bounds):
    x = design(raw[FEATURES].to_numpy(), bounds)
    observed = raw.probability.to_numpy()
    weights = raw.n_trials.to_numpy(float)
    weights /= weights.sum()

    def objective(parameters):
        coeff = coefficients(parameters)
        eta = x @ coeff.ravel()
        predicted = expit(eta)
        loss = np.sum(weights * (np.logaddexp(0, eta) - observed * eta))
        gradient = (x.T @ (weights * (predicted - observed))).reshape(BASES)
        for axis, strength in enumerate(strengths):
            delta = np.diff(coeff, n=2, axis=axis)
            scale = strength * (BASES[axis] - 1) ** 4 / delta.size
            loss += scale * np.sum(delta**2)
            for offset, multiplier in enumerate((1, -2, 1)):
                selection = [slice(None), slice(None)]
                selection[axis] = slice(offset, offset + coeff.shape[axis] - 2)
                gradient[tuple(selection)] += 2 * scale * multiplier * delta
        return float(loss), coefficient_gradient(gradient)

    initial = np.zeros(BASES)
    rate = np.clip(np.average(observed, weights=weights), 1e-6, 1 - 1e-6)
    initial[0] = logit(rate)
    result = minimize(
        objective,
        initial.ravel(),
        jac=True,
        method="L-BFGS-B",
        bounds=[(None, None)] * BASES[1] + [(0, None)] * ((BASES[0] - 1) * BASES[1]),
        options={"maxiter": 700, "ftol": 1e-10, "gtol": 1e-6},
    )
    return coefficients(result.x), bool(result.success)


def score(rows, predictions):
    metrics = score_predictions(rows.probability, predictions, rows.n_trials)
    metrics["calibration"] = []
    buckets = np.minimum((predictions * 10).astype(int), 9)
    for bucket in np.unique(buckets):
        mask = buckets == bucket
        weights = rows.n_trials.to_numpy()[mask]
        metrics["calibration"].append(
            dict(
                bucket=int(bucket),
                trials=int(weights.sum()),
                predicted=float(np.average(predictions[mask], weights=weights)),
                observed=float(
                    np.average(rows.probability.to_numpy()[mask], weights=weights)
                ),
            )
        )
    return metrics


def residual_diagnostics(rows, predictions):
    frame = rows.assign(predicted=predictions, residual=rows.probability - predictions)
    bands, markets = [], []
    for field, target in [("price_band", bands), ("market", markets)]:
        for label, peers in frame.groupby(field):
            target.append(
                dict(
                    group=int(label) if field == "price_band" else str(label),
                    events=int(peers.event_ticker.nunique()),
                    residual=float(np.average(peers.residual, weights=peers.n_trials)),
                    **score(peers, peers.predicted.to_numpy()),
                )
            )
    # Within overlapping distance/time bins, remove their weighted means.
    keys = [frame.d_bin, frame.t_bin]
    total = frame.n_trials.groupby(keys).transform("sum")
    pmean = (frame.current_price * frame.n_trials).groupby(keys).transform(
        "sum"
    ) / total
    rmean = (frame.residual * frame.n_trials).groupby(keys).transform("sum") / total
    overlap = (frame.price_band.groupby(keys).transform("nunique") >= 2) & (
        frame.event_ticker.groupby(keys).transform("nunique") >= 5
    )
    x = (frame.current_price - pmean).to_numpy()[overlap]
    y = (frame.residual - rmean).to_numpy()[overlap]
    weights = frame.n_trials.to_numpy()[overlap]
    denominator = np.sum(weights * x**2)
    check = dict(
        status="insufficient price-band overlap",
        overlapping_bins=int(
            frame.loc[overlap, ["d_bin", "t_bin"]].drop_duplicates().shape[0]
        ),
    )
    if denominator > 1e-12:
        slope = float(np.sum(weights * x * y) / denominator)
        cluster = (
            pd.Series(
                weights * x * (y - slope * x),
                index=frame.event_ticker.to_numpy()[overlap],
            )
            .groupby(level=0)
            .sum()
        )
        se = float(
            np.sqrt(np.sum(cluster**2) * len(cluster) / max(len(cluster) - 1, 1))
            / denominator
        )
        check.update(
            status="estimated" if len(cluster) >= 5 else "few events; descriptive only",
            events=len(cluster),
            residual_per_unit_price=slope,
            cluster_standard_error=se,
            interval_95=[slope - 1.96 * se, slope + 1.96 * se],
        )
    return dict(
        price_bands=bands, market_types=markets, within_distance_time_price_effect=check
    )


@dataclass
class OddsMoneynessModel:
    weights: np.ndarray
    bounds: tuple
    rows: pd.DataFrame
    raw: pd.DataFrame
    strengths: tuple
    validation: dict
    audit: dict
    outcome: str
    version: str = MODEL_VERSION

    def predict(self, points):
        return predictions(self.weights, points, self.bounds)

    @cached_property
    def support_bits(self):
        """Compact distinct-game membership per empirical bin."""
        ids = {
            event: index for index, event in enumerate(self.rows.event_ticker.unique())
        }
        return [
            sum(1 << ids[event] for event in events)
            for events in self.rows.groupby(["d_bin", "t_bin"]).event_ticker.unique()
        ]

    def evaluate(self, points, tolerances=(0.2, 0.05), minimum=20):
        points = points_array(points)
        if (
            len(tolerances) != 2
            or not np.isfinite(tolerances).all()
            or min(tolerances) <= 0
            or tolerances[1] > 1
        ):
            raise ValueError(
                "Distance/time tolerances must be positive; time tolerance is at most 1."
            )
        if not isinstance(minimum, int) or minimum < 1:
            raise ValueError("Minimum support must be a positive integer.")
        scale = np.asarray(tolerances)
        tree = cKDTree(self.raw[FEATURES].to_numpy() / scale)
        neighbors = tree.query_ball_point(points / scale, 1 + 1e-12, p=np.inf)
        hits, trials = self.raw.n_hits.to_numpy(), self.raw.n_trials.to_numpy()
        counts = np.zeros(len(points), dtype=int)
        for index, indices in enumerate(neighbors):
            membership = 0
            for bin_index in indices:
                membership |= self.support_bits[bin_index]
            counts[index] = membership.bit_count()
        totals = np.asarray([trials[indices].sum() for indices in neighbors])
        local_hits = np.asarray([hits[indices].sum() for indices in neighbors])
        empirical = np.divide(
            local_hits, totals, out=np.full(len(points), np.nan), where=totals > 0
        )
        domain = (
            (points[:, 0] > self.bounds[0])
            & (points[:, 0] <= self.bounds[1])
            & (points[:, 1] < 1)
        )
        supported = domain & (counts >= minimum)
        probability = self.predict(points)
        probability[~supported] = np.nan
        status = np.where(
            ~domain,
            "extrapolated",
            np.where(
                counts >= minimum,
                "supported",
                np.where(counts > 0, "sparse", "extrapolated"),
            ),
        ).astype(object)
        status[points[:, 1] == 1] = "horizon ended"
        empirical[~domain] = np.nan
        return dict(
            probability=probability,
            empirical=empirical,
            n_events=counts,
            n_trials=totals,
            n_hits=local_hits,
            supported=supported,
            status=status,
        )


def fit_model(frame, outcome="optimistic", smoothing="auto"):
    rows, audit = dataset(frame, outcome)
    raw = pooled_bins(rows)
    bounds = (0.0, audit["source_distance_range"][1])
    automatic = isinstance(smoothing, str) and smoothing == "auto"
    chosen = DEFAULT_STRENGTHS if automatic else tuple(float(v) for v in smoothing)
    if len(chosen) != 2 or not np.isfinite(chosen).all() or min(chosen) < 0:
        raise ValueError(
            "Specify two finite nonnegative smoothing strengths: distance, time."
        )
    buckets = rows.event_ticker.map(
        lambda event: int(hashlib.sha256(event.encode()).hexdigest()[:8], 16) % 5
    )
    train, tune, test = (
        rows.loc[buckets >= 2],
        rows.loc[buckets == 1],
        rows.loc[buckets == 0],
    )
    validation = dict(
        status="unvalidated",
        reason="Need at least 30 games, 10 training, 5 tuning and 5 independent test games.",
        model_version=MODEL_VERSION,
        features=FEATURES,
        monotonic_distance=True,
        monotonicity_reason="A farther below-market bid requires a lower future minimum under the same price-reaching horizon; pooled-price departures are checked in residual diagnostics.",
    )
    centered_d = raw.log_odds_distance - np.average(
        raw.log_odds_distance, weights=raw.n_trials
    )
    denominator = np.sum(raw.n_trials * centered_d**2)
    validation["empirical_distance_slope"] = (
        float(np.sum(raw.n_trials * centered_d * raw.probability) / denominator)
        if denominator > 0
        else None
    )
    if (
        rows.event_ticker.nunique() >= 30
        and train.event_ticker.nunique() >= 10
        and tune.event_ticker.nunique() >= 5
        and test.event_ticker.nunique() >= 5
    ):
        candidates = []
        train_raw = pooled_bins(train)
        for strengths in CANDIDATES if automatic else [chosen]:
            weights, converged = fit_weights(train_raw, strengths, bounds)
            estimates = predictions(weights, tune[FEATURES].to_numpy(), bounds)
            candidates.append(
                dict(strengths=strengths, converged=converged, **score(tune, estimates))
            )
        stable = [entry for entry in candidates if entry["converged"]]
        winner = min(stable or candidates, key=lambda entry: entry["log_loss"])
        chosen = tuple(winner["strengths"])
        weights, converged = fit_weights(
            pooled_bins(pd.concat([train, tune])), chosen, bounds
        )
        estimates = predictions(weights, test[FEATURES].to_numpy(), bounds)
        validation.update(
            status="whole-game tuning + independent test",
            train_events=int(train.event_ticker.nunique()),
            tuning_events=int(tune.event_ticker.nunique()),
            test_events=int(test.event_ticker.nunique()),
            candidates=candidates,
            selected_tuning=winner,
            test_metrics=score(test, estimates),
            test_optimizer_converged=converged,
            diagnostics=residual_diagnostics(test, estimates),
            note="Event groups are disjoint. Repeated states/bids remain dependent; final displayed fit uses all eligible games.",
        )
    weights, converged = fit_weights(raw, chosen, bounds)
    validation.update(
        strengths=chosen,
        optimizer_converged=converged,
        basis_functions=list(BASES),
        training_distance_range=[float(rows.log_odds_distance.min()), bounds[1]],
        weighting="Summed actual hits / state-bid trials; correlated bids are not independent games.",
    )
    return OddsMoneynessModel(
        weights, bounds, rows, raw, chosen, validation, audit, outcome
    )


def surface_grid(model, tolerances=(0.2, 0.05), minimum=20, resolution=101):
    # Concentrate display samples near at-market bids without adding grid cells.
    fractions = np.linspace(0, 1, resolution) ** 4
    distances = model.bounds[0] + (model.bounds[1] - model.bounds[0]) * fractions
    times = np.linspace(0, 1, resolution)
    d, t = np.meshgrid(distances, times)
    result = model.evaluate(
        np.column_stack([d.ravel(), t.ravel()]), tolerances, minimum
    )
    return dict(
        distances=distances,
        times=times,
        **{key: value.reshape(d.shape) for key, value in result.items()},
    )
