"""Scientific and linked-view guarantees for the independent no-CLV model."""

import numpy as np
import pandas as pd
import pytest

from src.analysis.kalshi.no_clv_fill import (
    SHAPE,
    bending,
    empirical_bins,
    fit_model,
    fit_weights,
    heatmap_grid,
    predict,
    time_grid,
    whole_game_split,
)


def observations(games=12):
    return pd.DataFrame(
        [
            dict(
                event_ticker=f"KXTEST-{event}",
                ticker=f"KXTEST-{event}-A",
                current_price=price,
                normalized_time=time,
                clv=clv,
                future_min_cents=future,
                settlement_time=pd.Timestamp("2026-01-02", tz="UTC"),
            )
            for event in range(games)
            for price in (0.25, 0.6, 1.0)
            for time in (0.0, 0.5, 0.95)
            for clv, future in ((0.1, 0.0), (0.9, 50.0), (0.5, np.nan))
        ]
    )


@pytest.fixture(scope="module")
def model():
    return fit_model(observations(), smoothing=(1e-5, 1e-5, 1e-5))


def test_clv_is_pooled_and_never_used(model):
    source = observations()
    raw = empirical_bins(source)
    pd.testing.assert_frame_equal(raw, empirical_bins(source.assign(clv=0.42)))
    pd.testing.assert_frame_equal(raw, empirical_bins(source.drop(columns="clv")))
    assert "clv" not in model.source and "clv" not in model.raw
    row = raw.loc[
        (raw.current_price == 0.6) & (raw.normalized_time == 0.5) & (raw.bid == 0.05)
    ].iloc[0]
    assert row.n_observations == 36
    assert row.n_events == 12
    assert row.n_hits == 12
    assert row.n_missing_future == 12
    assert row.probability == pytest.approx(1 / 3)


def test_weighted_hits_trials_and_valid_zero_rates():
    source = observations().iloc[:1]
    source = pd.concat(
        [source.assign(event_ticker="KXTEST-A", future_min_cents=0.0)] * 10
        + [source.assign(event_ticker="KXTEST-B", future_min_cents=90.0)]
    )
    row = empirical_bins(source).loc[lambda frame: frame.bid.eq(0.05)].iloc[0]
    assert row.probability == 0.5  # Local estimator convention: equal events.
    assert row.state_probability == pytest.approx(10 / 11)  # Binomial model trials.
    assert row.n_hits == 10 and row.n_observations == 11
    raw = empirical_bins(source.assign(future_min_cents=90))
    assert (raw.loc[raw.bid > 0, "probability"] == 0).all()
    assert not raw.synthetic.any()


def test_boundary_bounds_monotonicity_and_continuity(model):
    for price in (0, 0.001, 0.01, 0.25, 0.6, 0.99, 1):
        for time in (0, 0.5, 1):
            assert model.predict([[price, time, 0]])[0] == 1 - price
            bids = np.r_[0, 1e-10, np.linspace(0, price, 50)]
            values = model.predict([[price, time, b] for b in np.sort(bids)])
            assert np.isfinite(values).all()
            assert ((values >= 0) & (values <= 1)).all()
            assert (np.diff(values) >= -1e-12).all()
            assert abs(model.predict([[price, time, 1e-10]])[0] - (1 - price)) < 1e-6
    point = np.array([[0.6, 0.5, 0.2]])
    for axis in range(3):
        moved = point.copy()
        moved[0, axis] += 1e-8
        assert abs(model.predict(point)[0] - model.predict(moved)[0]) < 1e-6


def test_units_order_and_no_data(model):
    for bad in (
        [[50, 0.5, 25]],
        [[0.5, 50, 0.25]],
        [[0.5, 0.5, 25]],
        [[np.nan, 0.5, 0.25]],
        [[0.6, 0.5, 0.25, 0.1]],
    ):
        with pytest.raises(ValueError):
            model.predict(bad)
    with pytest.raises(ValueError, match="No reference"):
        fit_model(observations().iloc[:0])
    with pytest.raises(ValueError, match="positive-bid"):
        fit_model(observations().assign(current_price=0.01))
    with pytest.raises(ValueError, match="smoothing"):
        fit_model(observations(), smoothing=(-1, 1e-5, 1e-5))


def test_support_counts_match_empirical_neighborhood(model):
    points = [
        [0.6, 0.5, 0.05],
        [0.6, 0.5, 0.59],
        [0.1, 0.5, 0.05],
        [0.6, 1, 0.05],
        [0.6, 0.5, 0.6],
    ]
    result = model.evaluate(points, minimum=20)
    assert result["status"].tolist() == [
        "sparse",
        "sparse",
        "unsupported",
        "outside horizon/domain",
        "outside horizon/domain",
    ]
    assert np.isnan(result["probability"]).all()
    raw = model.empirical(points)
    np.testing.assert_equal(result["n_events"][:3], raw.n_events[:3])
    np.testing.assert_equal(result["n_observations"][:3], raw.n_observations[:3])
    supported = model.evaluate(points[:2], minimum=1)
    assert supported["supported"].all()
    assert model.evaluate([[0.1, 1, 0]], minimum=100)["probability"][0] == 0.9
    assert model.evaluate([[0.1, 1, 0]], minimum=100)["status"][0] == "assumed boundary"


def test_penalty_gradient_and_separate_axis_strengths():
    weights = np.random.default_rng(5).dirichlet(np.ones(SHAPE[1]), size=SHAPE[0])
    strengths = (1e-4, 1e-5, 1e-6)
    loss, grad = bending(weights, strengths)
    moved = weights.copy()
    moved[3, 2] += 1e-7
    assert (bending(moved, strengths)[0] - loss) / 1e-7 == pytest.approx(
        grad[3, 2], abs=1e-5
    )
    assert bending(weights, (0, 0, 0))[0] == 0
    assert bending(weights, (1e-4, 0, 0))[0] != bending(weights, (0, 1e-4, 0))[0]


def test_fits_known_curved_function():
    bids = np.arange(1, 60) / 100
    target = 0.4 + 0.5 * (bids / 0.6) ** 4
    raw = pd.DataFrame(
        dict(
            current_price=0.6,
            normalized_time=0.5,
            bid=bids,
            state_probability=target,
            n_observations=1000,
        )
    )
    weights, _ = fit_weights(raw, (1e-7, 1e-7, 1e-7))
    fitted = predict(
        weights,
        np.column_stack([np.full(len(bids), 0.6), np.full(len(bids), 0.5), bids]),
    )
    assert np.sqrt(np.mean((fitted - target) ** 2)) < 0.02


def test_whole_game_validation_excludes_boundary(monkeypatch):
    import src.analysis.kalshi.no_clv_fill as module

    source = observations(60)
    train, test = whole_game_split(source)
    assert set(train.event_ticker).isdisjoint(test.event_ticker)
    assert set(train.event_ticker) | set(test.event_ticker) == set(source.event_ticker)
    scored_bids = []
    original = module.validation_metrics

    def record(raw, predicted):
        scored_bids.extend(raw.bid)
        return original(raw, predicted)

    monkeypatch.setattr(module, "validation_metrics", record)
    fitted = fit_model(source)
    assert fitted.validation["status"] == "whole-game holdout"
    assert scored_bids and min(scored_bids) > 0
    assert len(fitted.validation["candidates"]) == 5
    assert fitted.validation["selected_metrics"]["calibration"]
    assert fitted.validation["selected_metrics"]["price_buckets"]
    assert fitted.validation["selected_metrics"]["time_buckets"]


def test_grid_resolution_and_linked_views(model):
    pytest.importorskip("streamlit")
    from src.analysis.kalshi.no_clv_fill_ui import (
        heatmap_figure,
        time_surface_figure,
        time_series_figure,
        snapshots_figure,
        SNAPSHOTS,
    )

    tolerances = (0.15, 0.15)
    p, t, b = 0.6, 0.5, 0.137
    selected = model.evaluate([[p, t, b]], tolerances, 1)["probability"][0]
    for resolution in (21, 51):
        grid = heatmap_grid(model, t, tolerances, 1, resolution)
        tgrid = time_grid(model, p, tolerances, 1, resolution)
        heat = heatmap_figure(grid, p, t, b, selected)
        surface = time_surface_figure(tgrid, p, t, b, selected, 0)
        series = time_series_figure(model, p, t, b, tolerances, 1)
        assert heat.data[1].customdata[0][1] == selected
        assert surface.data[-1].z[0] == selected
        assert series.data[-1].y[0] == selected
        assert heat.data[0].zmin == 0 and heat.data[0].zmax == 1
        assert surface.data[0].cmin == 0 and surface.data[0].cmax == 1
        grids = [heatmap_grid(model, ts, tolerances, 1, resolution) for ts in SNAPSHOTS]
        snapshots = snapshots_figure(grids, p, t, b, model, tolerances, 1)
        for i, ts in enumerate(SNAPSHOTS):
            np.testing.assert_allclose(
                snapshots[i].data[1].customdata[0][1],
                model.evaluate([[p, ts, b]], tolerances, 1)["probability"][0],
                equal_nan=True,
            )
    assert model.predict([[p, t, b]])[0] == selected


def test_playback_frames_preserve_exact_predictions_and_masks(model):
    pytest.importorskip("streamlit")
    from src.analysis.kalshi.fill_playback import cached_frames, playback_data

    tolerances = (0.15, 0.15)
    frames = cached_frames("test-v1", tolerances, 1, 21, model)
    assert len(frames["frames"]) == 101
    data = playback_data(
        model, frames, 0.6, 0.137, 0.5, tolerances, 1, "token", "KXTEST", None
    )
    for index in (0, 10, 50, 95, 100):
        time = index / 100
        exact = model.evaluate([[0.6, time, 0.137]], tolerances, 1)["probability"][0]
        assert (
            data["selected"][index] == pytest.approx(exact)
            if np.isfinite(exact)
            else data["selected"][index] is None
        )
        grid = heatmap_grid(model, time, tolerances, 1, 21)
        slice_points = np.column_stack(
            [np.full(21, 0.6), np.full(21, time), np.asarray(frames["bids"]) / 100]
        )
        exact_slice = model.evaluate(slice_points, tolerances, 1)["probability"]
        assert (
            data["slices"][index]
            == np.where(np.isfinite(exact_slice), exact_slice, None).tolist()
        )
        expected = np.where(
            np.isfinite(grid["probability"]), grid["probability"], None
        ).tolist()
        assert frames["frames"][index]["z"] == expected


def test_surface_has_room_for_axes_and_legend(model):
    pytest.importorskip("streamlit")
    from src.analysis.kalshi.no_clv_fill_ui import time_surface_figure

    grid = time_grid(model, 0.6, (0.15, 0.15), 1, 21)
    figure = time_surface_figure(grid, 0.6, 0.5, 0.2, 0.4, 0)
    assert figure.layout.margin.b >= 100
    assert figure.layout.legend.y >= 1
    assert figure.layout.scene.camera.projection.type == "orthographic"
    assert figure.layout.scene.zaxis.range == (0, 1)
