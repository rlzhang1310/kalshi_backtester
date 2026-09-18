"""Numerical guarantees for the research-only surface model."""

import numpy as np
import pandas as pd
import pytest

from src.analysis.kalshi.fill_surface import (
    CONDITION_BASES,
    BID_BASES,
    bending,
    curvature,
    empirical_bins,
    fit_surface,
    fit_weights,
    predict,
    reference_data,
    surface_grid,
)


def observations():
    return pd.DataFrame(
        [
            dict(
                ticker=f"KXTEST-{event}-A",
                event_ticker=f"KXTEST-{event}",
                clv=clv,
                current_price=price,
                normalized_time=time,
                future_min_cents=minimum,
                settlement_time=pd.Timestamp("2026-01-02", tz="UTC"),
            )
            for event in range(12)
            for clv in [0.25, 0.6, 1.0]
            for price in [0.25, 0.6, 1.0]
            for time in [0.0, 0.5, 0.95]
            for minimum in [0.0, 50.0, np.nan]
        ]
    )


@pytest.fixture(scope="module")
def model():
    return fit_surface(observations(), strength=0.01, boundary="assumed")


def test_exact_boundary_monotonicity_and_continuity(model):
    for clv in [0.25, 0.6, 1.0]:
        for price in [0.25, 0.6, 1.0]:
            for time in [0.0, 0.5, 0.95]:
                bids = np.r_[0.0, 1e-9, np.linspace(0.001, price - 0.001, 60)]
                points = [[clv, price, time, b] for b in bids]
                result = model.evaluate(points, minimum=1)
                values = result["probability"]
                assert values[0] == 1 - price
                assert abs(values[1] - values[0]) < 1e-7
                assert (np.diff(values) >= -1e-12).all()
                assert ((values >= 0) & (values <= 1)).all()
    other = fit_surface(observations(), strength=1.0, boundary="assumed")
    assert other.evaluate([[0.6, 0.6, 0.5, 0]], minimum=1)["probability"][0] == 0.4


def test_raw_zeros_missing_and_domain(model):
    source = observations()
    source["future_min_cents"] = 90.0
    source.loc[source.index[:10], "future_min_cents"] = np.nan
    raw = empirical_bins(source)
    assert raw.loc[raw.bid.eq(0.05), "probability"].eq(0).all()
    assert raw.loc[raw.bid.eq(0.05), "n_hits"].eq(0).all()
    assert (raw.n_hits <= raw.n_observations).all()
    assert not raw.synthetic.any()
    assert source.future_min_cents.isna().sum() == 10
    result = model.evaluate(
        [[0.1, 0.6, 0.5, 0.05], [0.6, 0.6, 1.0, 0], [0.6, 0.6, 0.5, 0.6]], minimum=1
    )
    assert np.isnan(result["probability"]).all()
    assert not result["supported"].any()
    assert model.validation["boundary_conflict_bins"] > 0


def test_surface_and_cross_section_use_same_function(model):
    grid = surface_grid(model, 0.6, 0.5, (0.1, 0.1, 0.1), 1, resolution=21)
    row, col = 2, 12
    p, b = grid["prices"][col], grid["bids"][row]
    value = model.evaluate([[0.6, p, 0.5, b]], (0.1, 0.1, 0.1), 1)["probability"][0]
    assert grid["probability"][row, col] == pytest.approx(value)
    # Bernstein mixture continuity in every coordinate, not independent slices.
    point = np.array([[0.6, 0.6, 0.5, 0.2]])
    for axis in range(4):
        neighbor = point.copy()
        neighbor[0, axis] += 1e-7
        assert (
            abs(
                predict(model.weights, point, model.boundary)[0]
                - predict(model.weights, neighbor, model.boundary)[0]
            )
            < 1e-5
        )


def test_curvature_uses_probability_spacing_and_masks():
    for resolution in [21, 51, 101]:
        coords = np.linspace(0, 1, resolution)
        p, b = np.meshgrid(coords, coords)
        z = 3 * p**2 + 2 * b**2
        z[:4, :4] = np.nan
        result = curvature(dict(prices=coords, bids=coords, probability=z))
        assert result["bid"] == pytest.approx(16.0)
        assert result["current_price"] == pytest.approx(36.0)
    assert curvature(dict(prices=coords, bids=coords, probability=z * np.nan)) == dict(
        bid=None, current_price=None
    )


def test_penalty_gradient():
    weights = np.random.default_rng(4).uniform(size=(CONDITION_BASES**3, BID_BASES + 2))
    loss, gradient = bending(weights)
    moved = weights.copy()
    moved[3, 2] += 1e-6
    assert (bending(moved)[0] - loss) / 1e-6 == pytest.approx(gradient[3, 2], abs=1e-3)


@pytest.mark.parametrize("power", [1, 5])
def test_spline_recovers_linear_and_curved_data_without_boundary_floor(power):
    # A known data-generating curve verifies fit quality, rather than merely
    # asserting that the model is capable of producing a curved chart.
    bids = np.arange(0, 60) / 100
    target = 0.12 + 0.8 * (bids / 0.6) ** power
    raw = pd.DataFrame(
        dict(
            clv=0.5,
            current_price=0.6,
            normalized_time=0.5,
            bid=bids,
            probability=target,
            n_events=500,
        )
    )
    weights, _ = fit_weights(raw, 1e-7, "empirical")
    points = np.column_stack(
        [
            np.full(len(bids), 0.5),
            np.full(len(bids), 0.6),
            np.full(len(bids), 0.5),
            bids,
        ]
    )
    fitted = predict(weights, points, "empirical")
    assert np.sqrt(np.mean((fitted - target) ** 2)) < 0.015
    assert abs(fitted[0] - 0.12) < 0.025  # No forced .40 floor.
    assert (np.diff(fitted) >= -1e-12).all()
    if power == 5:
        assert fitted[-1] - fitted[-6] > 5 * (fitted[10] - fitted[5])


def test_penalty_does_not_punish_nonuniform_weights_for_a_straight_curve():
    from src.analysis.kalshi.fill_surface import _BID_MASS

    # Integrated splines weighted by their basis integrals sum to r exactly.
    # Knot spacing is deliberately unequal, so these mixture weights differ.
    weights = np.zeros((CONDITION_BASES**3, BID_BASES + 2))
    weights[:, 1:-1] = _BID_MASS
    loss, _ = bending(weights)
    assert loss < 1e-10


def test_default_boundary_learns_zero_observations_instead_of_imposing_floor():
    fitted = fit_surface(observations().assign(future_min_cents=100.0), strength=1e-5)
    result = fitted.evaluate([[0.6, 0.6, 0.5, 0], [0.6, 0.6, 0.5, 0.4]], minimum=1)
    assert fitted.boundary == "empirical"
    assert not result["synthetic"].any()
    assert (result["probability"] < 0.02).all()


def test_filter_and_no_data():
    data = observations()
    result = reference_data(data, "KXTEST", "KXTEST-0-A")
    assert not result.event_ticker.eq("KXTEST-0").any()
    assert reference_data(data, "KXTEST", as_of="2026-01-01").empty
    with pytest.raises(ValueError, match="No reference"):
        fit_surface(data.iloc[:0])


def test_validation_split_is_by_game(monkeypatch):
    import src.analysis.kalshi.fill_surface as module

    data = pd.concat(
        [
            observations().assign(event_ticker=lambda df: df.event_ticker + f"-{i}")
            for i in range(6)
        ]
    )
    calls = []
    original = module.empirical_bins

    def record(frame, outcome):
        calls.append(set(frame.event_ticker))
        return original(frame, outcome)

    monkeypatch.setattr(module, "empirical_bins", record)
    fitted = fit_surface(data, strength=0.01)
    assert fitted.validation["status"] == "whole-game holdout"
    assert calls[1].isdisjoint(calls[2])
    assert calls[1] | calls[2] == calls[0]


def test_chart_grids_and_selected_readout_match(model):
    pytest.importorskip("streamlit")
    from src.analysis.kalshi.fill_surface_ui import chart_figures

    grid = surface_grid(model, 0.6, 0.5, (0.1, 0.1, 0.1), 1, resolution=21)
    p, b = grid["prices"][12], grid["bids"][2]
    surface, section, point = chart_figures(
        model, grid, 0.6, 0.5, p, b, (0.1, 0.1, 0.1), 1, None, "Surface", 0
    )
    heatmap, _, _ = chart_figures(
        model, grid, 0.6, 0.5, p, b, (0.1, 0.1, 0.1), 1, None, "Heatmap", 0
    )
    np.testing.assert_equal(surface.data[0].z, heatmap.data[0].z)
    assert section.data[-1].y[0] == point["probability"][0]
    assert surface.data[-1].z[0] == point["probability"][0]


@pytest.mark.parametrize("price_cents", [1, *range(5, 101, 5)])
@pytest.mark.parametrize("outcome", ["optimistic", "conservative"])
def test_raw_section_excludes_equal_price_ticks(price_cents, outcome):
    pytest.importorskip("streamlit")
    from types import SimpleNamespace
    from src.analysis.kalshi.fill_surface_ui import raw_section

    price = price_cents / 100
    source = observations().assign(clv=0.5, current_price=price, normalized_time=0.5)
    model = SimpleNamespace(source=source, outcome=outcome)
    raw = raw_section(model, "KXTEST", 0.5, price, 0.5, (0.05, 0.05, 0.025))
    assert not raw.empty
    assert (raw.bid < price).all()
    np.testing.assert_allclose(raw.bid, [c / 100 for c in range(0, price_cents, 5)])
