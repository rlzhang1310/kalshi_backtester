"""Scientific checks for the independent [log-odds distance, time] model."""

import numpy as np
import pandas as pd
import pytest
from scipy.special import expit, logit

from src.analysis.kalshi.odds_moneyness import (
    FEATURES,
    MODEL_VERSION,
    dataset,
    distance,
    equivalent_bid,
    fit_model,
    pooled_bins,
    surface_grid,
)


def observations(games=12):
    return pd.DataFrame(
        [
            dict(
                event_ticker=f"KXTEST-{game}",
                ticker=f"KXTEST-{game}-A",
                current_price=p,
                normalized_time=t,
                clv=0.4,
                future_min_cents=future,
                settlement_time=pd.Timestamp("2026-01-02", tz="UTC"),
            )
            for game in range(games)
            for p in (0.15, 0.35, 0.6, 0.85)
            for t in (0.0, 0.5, 0.95)
            for future in (0.0, 30.0, 90.0, np.nan)
        ]
    )


@pytest.fixture(scope="module")
def model():
    return fit_model(observations(), smoothing=(1e-4, 1e-4))


def test_sign_units_and_inverse():
    assert distance(0.5, 0.25) == pytest.approx(np.log(3))
    assert distance(0.25, 0.5) < 0
    assert distance(0.5, 0.5) == 0
    for p in (0.01, 0.2, 0.5, 0.99):
        for b in (0.01, 0.15, 0.75, 0.99):
            assert equivalent_bid(p, distance(p, b)) == pytest.approx(b)
    for p, b in [
        (50, 0.2),
        (0.5, 20),
        (0, 0.2),
        (1, 0.2),
        (0.5, 0),
        (0.5, 1),
        (0.5, np.nan),
    ]:
        with pytest.raises(ValueError):
            distance(p, b)


def test_actual_trials_weighted_zeros_endpoints_and_target():
    source = observations(1).iloc[:1]
    frame = pd.concat(
        [source.assign(future_min_cents=20.0)] * 10
        + [source.assign(future_min_cents=90.0)]
    )
    frame["current_price"] = 0.6
    rows, audit = dataset(frame)
    pooled = pooled_bins(rows)
    at_bid = pooled.loc[np.isclose(pooled.bid, 0.2)].iloc[0]
    assert at_bid.n_trials == 11 and at_bid.n_hits == 10
    assert at_bid.probability == pytest.approx(10 / 11)
    conservative, _ = dataset(frame, "conservative")
    assert conservative.loc[np.isclose(conservative.bid, 0.2), "n_hits"].sum() == 0
    assert (pooled.loc[pooled.bid < 0.2, "probability"] == 0).all()
    assert rows.bid.min() > 0 and rows.log_odds_distance.min() > 0
    _, audit = dataset(
        pd.concat(
            [
                frame,
                source.assign(current_price=0),
                source.assign(current_price=1),
                source.assign(normalized_time=1),
            ]
        )
    )
    assert audit["endpoint_prices"] == 2 and audit["ended_horizon"] == 1
    with pytest.raises(ValueError, match="cents"):
        dataset(frame.assign(current_price=60))
    with pytest.raises(ValueError, match="No eligible"):
        dataset(frame.assign(current_price=0))


def test_only_distance_time_inputs_and_equivalent_prices(model):
    assert FEATURES == ["log_odds_distance", "normalized_time"]
    assert model.version == MODEL_VERSION
    for p in (0.25, 0.6, 0.9):
        b = expit(logit(p) - 1.2)
        assert model.predict([[distance(p, b), 0.5]])[0] == pytest.approx(
            model.predict([[1.2, 0.5]])[0]
        )
    with pytest.raises(ValueError, match="feature order"):
        model.predict([[0.6, 0.5, 0.4]])
    rows, _ = dataset(observations().assign(clv=0.9))
    pd.testing.assert_frame_equal(
        rows.drop(columns="clv"),
        dataset(observations().assign(clv=0.1))[0].drop(columns="clv"),
    )
    refit = fit_model(observations().assign(clv=0.99), smoothing=model.strengths)
    np.testing.assert_allclose(model.weights, refit.weights)


def test_yes_no_complements_normalized_once_before_distance():
    from src.analysis.kalshi.fill_probability import normalize_trade_prices

    trades = pd.DataFrame(
        [
            dict(
                trade_id="a",
                ticker="KXTEST-A",
                created_time="2026-01-01T00:00:00Z",
                yes_price=40,
                no_price=60,
                taker_side="yes",
            ),
            dict(
                trade_id="b",
                ticker="KXTEST-A",
                created_time="2026-01-01T00:00:01Z",
                yes_price=40,
                no_price=60,
                taker_side="no",
            ),
        ]
    )
    prices = normalize_trade_prices(trades).price_cents.to_numpy() / 100
    np.testing.assert_allclose(prices, [0.4, 0.4])
    np.testing.assert_allclose(distance(prices, 0.2), [distance(0.4, 0.2)] * 2)


def test_monotonic_distance_bounds_support_and_valid_zero(model):
    for t in (0, 0.3, 0.7, 0.99):
        points = np.column_stack([np.linspace(*model.bounds, 151), np.full(151, t)])
        predicted = model.predict(points)
        assert np.all((predicted >= 0) & (predicted <= 1))
        assert np.diff(predicted).max() <= 1e-12
    result = model.evaluate(
        [[1.0, 0.5], [1.0, 1.0], [-1.0, 0.5], [model.bounds[1] + 1, 0.5]], (0.3, 0.1), 1
    )
    assert np.isfinite(result["probability"][0])
    assert result["n_events"][0] == 12  # Nearby bins/bids never multiply game count.
    assert np.isnan(result["probability"][1:]).all()
    assert result["status"][1] == "horizon ended"
    assert np.isnan(model.evaluate([[0.0, 0.5]], (0.3, 0.1), 1)["probability"][0])
    scarce = model.evaluate([[1.0, 0.5]], (0.3, 0.1), 100)
    assert scarce["status"][0] == "sparse" and np.isnan(scarce["probability"][0])


def test_shared_grid_and_exact_markers_independent_of_resolution(model):
    pytest.importorskip("streamlit")
    from src.analysis.kalshi.odds_moneyness_ui import chart_figures

    d, t = 1.234, 0.5
    selected = model.evaluate([[d, t]], (0.2, 0.05), 1)["probability"][0]
    for resolution in (21, 51):
        grid = surface_grid(model, (0.2, 0.05), 1, resolution)
        distances = grid["distances"]
        widths = np.diff(distances)
        assert distances[0] == pytest.approx(model.bounds[0])
        assert distances[-1] == pytest.approx(model.bounds[1])
        assert np.all(widths > 0)
        assert np.all(np.diff(widths) > 0)
        span = model.bounds[1] - model.bounds[0]
        assert distances[resolution // 2] == pytest.approx(model.bounds[0] + span / 16)
        assert widths[0] < span / (resolution - 1) / 10
        assert grid["probability"].shape == (resolution, resolution)
        heat, surface = chart_figures(model, grid, d, t, selected, True, 0)
        np.testing.assert_allclose(heat.data[0].x, grid["times"])
        np.testing.assert_allclose(surface.data[0].x, grid["times"])
        np.testing.assert_allclose(heat.data[0].y, np.log1p(distances / 0.1))
        np.testing.assert_allclose(surface.data[0].y, heat.data[0].y)
        np.testing.assert_allclose(
            heat.data[0].z, grid["probability"].T, equal_nan=True
        )
        assert heat.data[-1].x[0] == surface.data[-1].x[0] == t
        assert heat.data[-1].y[0] == pytest.approx(np.log1p(d / 0.1))
        np.testing.assert_allclose(
            heat.data[0].customdata[:, :, 4].astype(float),
            np.broadcast_to(distances[:, None], (resolution, resolution)),
        )
        np.testing.assert_allclose(heat.data[0].z, surface.data[0].z, equal_nan=True)
        assert heat.data[-1].customdata[0][0] == selected == surface.data[-1].z[0]
        assert heat.data[0].zmin == surface.data[0].cmin == 0
        assert heat.data[0].zmax == surface.data[0].cmax == 1
        assert surface.layout.scene.zaxis.range == (0, 1)
        assert surface.layout.margin.b >= 100


def test_whole_event_tuning_independent_test_and_price_diagnostics():
    model = fit_model(observations(40), smoothing=(1e-4, 1e-4))
    validation = model.validation
    assert validation["status"] == "whole-game tuning + independent test"
    assert (
        validation["train_events"]
        + validation["tuning_events"]
        + validation["test_events"]
        == 40
    )
    assert validation["test_metrics"]["brier"] >= 0
    assert validation["test_metrics"]["log_loss"] > 0
    assert validation["test_metrics"]["calibration"]
    assert len(validation["diagnostics"]["price_bands"]) == 4
    assert (
        validation["diagnostics"]["within_distance_time_price_effect"][
            "overlapping_bins"
        ]
        > 0
    )
