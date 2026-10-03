from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.kalshi.rolling_volatility import rolling_volatility_payload
from src.analysis.kalshi.single_game_odds_time_series import build_game_odds_figure


def trades(prices: list[float], *, start: str = "2026-10-01T12:00:00Z", step: int = 5):
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=len(prices), freq=f"{step}s"),
            "target_raw_prob": prices,
        }
    )


def test_known_sequence_and_warmup():
    frame = trades([0.5, 0.52, 0.5] + [0.5] * 10)
    payload = rolling_volatility_payload(frame)
    assert payload["windows_seconds"] == [60, 300, 600, 1800]
    assert payload["samples"][0]["values"] == [None] * 4
    assert payload["samples"][0]["per_minute"] == [None] * 4
    assert payload["samples"][-1]["values"][0] == 2.828427
    assert payload["samples"][-1]["per_minute"][0] == 2.828427
    assert payload["samples"][-1]["values"][1] is None


def test_flat_prices_are_zero_not_na():
    samples = rolling_volatility_payload(trades([0.6] * 361))["samples"]
    assert samples[-1]["values"] == [0.0] * 4
    assert samples[-1]["per_minute"] == [0.0] * 4


def test_time_normalization_compares_windows_on_one_minute_basis():
    prices = [0.5, 0.52] + [0.52] * 359
    sample = rolling_volatility_payload(trades(prices))["samples"][-1]
    # The early movement is outside the short windows but inside 30 minutes.
    assert sample["values"] == [0.0, 0.0, 0.0, 2.0]
    assert sample["per_minute"] == [0.0, 0.0, 0.0, 0.365148]


def test_sparse_data_is_unavailable():
    samples = rolling_volatility_payload(trades([0.5] * 20, step=40))["samples"]
    assert samples[-1]["values"][0] is None
    assert samples[-1]["reasons"][0] in (
        "insufficient window coverage", "data gap / unsupported resolution"
    )


def test_material_gap_inside_covered_window_is_unavailable():
    frame = trades([0.5] * 13)
    frame = frame.drop(index=[3, 4, 5, 6, 7, 8])
    sample = rolling_volatility_payload(frame)["samples"][-1]
    assert sample["values"][0] is None
    assert sample["reasons"][0] == "data gap / unsupported resolution"


def test_minutely_observations_support_long_windows_without_fake_short_values():
    sample = rolling_volatility_payload(trades([0.5] * 31, step=60))["samples"][-1]
    assert sample["values"] == [None, 0.0, 0.0, 0.0]


def test_last_observed_trade_per_bucket_and_as_of_timestamp():
    frame = trades([0.5, 0.6], step=1)
    payload = rolling_volatility_payload(frame)
    assert len(payload["samples"]) == 1
    assert payload["samples"][0]["t"] == int(frame["timestamp"].iloc[1].value // 1_000_000)


def test_three_second_bucket_boundary():
    payload = rolling_volatility_payload(trades([0.5, 0.6, 0.7, 0.8], step=1))
    assert payload["cadence_seconds"] == 3
    assert len(payload["samples"]) == 2
    assert payload["samples"][0]["t"] == int(
        pd.Timestamp("2026-10-01T12:00:02Z").value // 1_000_000
    )


def test_custom_cadence_changes_bucket_boundaries():
    frame = trades([0.5, 0.6, 0.7, 0.8, 0.9], step=1)
    payload = rolling_volatility_payload(frame, cadence_seconds=2)
    assert payload["cadence_seconds"] == 2
    assert [sample["t"] for sample in payload["samples"]] == [
        int(frame["timestamp"].iloc[index].value // 1_000_000)
        for index in (1, 3, 4)
    ]


@pytest.mark.parametrize("cadence", [0, 61, 1.5, True])
def test_invalid_cadence_is_rejected(cadence):
    with pytest.raises(ValueError, match="cadence"):
        rolling_volatility_payload(trades([0.5]), cadence_seconds=cadence)


def test_new_ticker_figure_contains_only_its_own_history():
    def figure(frame, ticker):
        expanded = frame.assign(taker_prob=frame.target_raw_prob, maker_prob=frame.target_raw_prob, count=1)
        return build_game_odds_figure(expanded, event_ticker=ticker, target_label="Team", show_fee_lines=False)

    first = figure(trades([0.5] * 361), "FIRST")
    second = figure(trades([0.7] * 2, start="2026-10-02T12:00:00Z"), "SECOND")
    assert first.layout.meta["rolling_volatility"]["samples"][-1]["values"] == [0.0] * 4
    assert second.layout.meta["rolling_volatility"]["samples"][-1]["values"] == [None] * 4
    assert second.layout.meta["event_ticker"] == "SECOND"


def test_figure_uses_selected_volatility_cadence():
    frame = trades([0.5, 0.6, 0.7, 0.8], step=1).assign(
        taker_prob=lambda data: data.target_raw_prob,
        maker_prob=lambda data: data.target_raw_prob,
        count=1,
    )
    figure = build_game_odds_figure(
        frame, event_ticker="EVENT", target_label="Team",
        volatility_cadence_seconds=2, show_fee_lines=False,
    )
    assert figure.layout.meta["rolling_volatility"]["cadence_seconds"] == 2
