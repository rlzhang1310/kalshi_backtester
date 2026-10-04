from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.kalshi.rolling_volatility import rolling_volatility_payload
from src.analysis.kalshi.single_game_odds_time_series import build_game_odds_figure


START = pd.Timestamp("2026-10-01T12:00:00Z")


def trades(prices: list[float], *, seconds: list[int] | None = None, start=START):
    if seconds is None:
        seconds = list(range(0, 5 * len(prices), 5))
    return pd.DataFrame({
        "timestamp": [start + pd.Timedelta(seconds=second) for second in seconds],
        "target_raw_prob": prices,
    })


def at(second: int) -> int:
    return int((START + pd.Timedelta(seconds=second)).value // 1_000_000)


def sample(frame: pd.DataFrame, second: int, *, cadence: int = 1) -> tuple[dict, dict]:
    payload = rolling_volatility_payload(frame, cadence_seconds=cadence, as_of=at(second))
    return payload, payload["samples"][0]


def test_first_price_is_numeric_and_partial():
    payload, current = sample(trades([0.6]), 0)
    assert payload["windows_seconds"] == [60, 300, 600, 1800]
    assert current["values"] == [0.0] * 4
    assert current["per_minute"] == [0.0] * 4
    for key in ("up", "down", "two_way", "up_per_minute", "down_per_minute", "two_way_per_minute"):
        assert current[key] == [0.0] * 4
    assert current["partial"] == [True] * 4
    assert current["coverage_seconds"] == [1] * 4
    assert payload["game"]["values"] == 0.0
    assert payload["game"]["coverage_seconds"] == 1


def test_before_first_price_is_loading_without_future_fill():
    payload = rolling_volatility_payload(trades([0.6]), as_of=at(-1))
    assert payload["samples"] == []
    assert payload["last_trade_ms"] is None
    assert payload["game"] is None


def test_flat_price_is_zero_through_quiet_period():
    payload, current = sample(trades([0.6]), 1800)
    assert current["values"] == [0.0] * 4
    for key in ("up", "down", "two_way", "up_per_minute", "down_per_minute", "two_way_per_minute"):
        assert current[key] == [0.0] * 4
    assert current["partial"] == [False] * 4
    assert payload["last_trade_ms"] == at(0)


def test_upward_only_movement_has_no_downside_or_two_way_movement():
    _, current = sample(trades([0.5, 0.6, 0.7], seconds=[0, 10, 20]), 30)
    assert current["values"][0] == 14.142136
    assert current["up"][0] == 14.142136
    assert current["down"][0] == 0.0
    assert current["two_way"][0] == 0.0


def test_reversals_split_up_and_down_movement():
    _, current = sample(trades([0.5, 0.6, 0.5], seconds=[0, 10, 20]), 30)
    assert current["values"][0] == 14.142136
    assert current["up"][0] == 10.0
    assert current["down"][0] == 10.0
    assert current["two_way"][0] == 14.142136


def test_downward_only_movement_has_no_upside_or_two_way_movement():
    _, current = sample(trades([0.7, 0.6, 0.5], seconds=[0, 10, 20]), 30)
    assert current["up"][0] == 0.0
    assert current["down"][0] == 14.142136
    assert current["two_way"][0] == 0.0


@pytest.mark.parametrize(
    ("prices", "expected"),
    [([0.60, 0.61, 0.60, 0.61, 0.60], 2.0),
     ([0.55, 0.60, 0.55, 0.60, 0.55], 10.0)],
)
def test_two_way_movement_weights_large_swings(prices, expected):
    _, current = sample(trades(prices, seconds=[0, 1, 2, 3, 4]), 4)
    assert current["two_way"][0] == expected
    assert current["up"][0] ** 2 == pytest.approx(expected ** 2 / 2, abs=0.0001)
    assert current["down"][0] ** 2 == pytest.approx(expected ** 2 / 2, abs=0.0001)


def test_large_jump_with_small_correction_has_small_two_way_movement():
    _, current = sample(trades([0.50, 0.70, 0.69], seconds=[0, 1, 2]), 2)
    assert current["up"][0] == 20.0
    assert current["down"][0] == 1.0
    assert current["two_way"][0] == 1.414214
    assert current["values"][0] ** 2 == pytest.approx(401.0, abs=0.0001)


def test_square_rooted_movement_invariants_hold_for_each_window():
    _, current = sample(trades([0.50, 0.70, 0.69, 0.80], seconds=[0, 1, 2, 3]), 3)
    for rv, up, down, score in zip(
        current["values"], current["up"],
        current["down"], current["two_way"],
    ):
        assert rv ** 2 == pytest.approx(up ** 2 + down ** 2, abs=0.0001)
        assert 0 <= score ** 2 <= up ** 2 + down ** 2 + 0.0001


def test_quiet_period_advances_window_and_expires_old_move():
    frame = trades([0.5, 0.6], seconds=[0, 10])
    _, early = sample(frame, 20)
    payload, late = sample(frame, 80)
    assert early["values"][0] == 10.0
    assert late["values"][0] == 0.0
    assert late["up"][0] == 0.0
    assert late["down"][0] == 0.0
    assert late["two_way"][0] == 0.0
    assert not late["partial"][0]
    assert payload["last_trade_ms"] == at(10)


def test_time_normalization_uses_available_history_during_warmup():
    payload, current = sample(trades([0.5, 0.6], seconds=[0, 10]), 19)
    assert current["coverage_seconds"][0] == 20
    assert current["values"][0] == 10.0
    assert current["per_minute"][0] == 17.320508
    assert current["up"][0] == 10.0
    assert current["up_per_minute"][0] == 17.320508
    assert current["down_per_minute"][0] == 0.0
    assert current["two_way_per_minute"][0] == 0.0
    assert payload["game"]["up_per_minute"] == 17.320508
    assert payload["game"]["per_minute"] == current["per_minute"][0]


def test_all_square_rooted_metrics_normalize_like_volatility():
    payload, current = sample(trades([0.5, 0.6, 0.5], seconds=[0, 10, 20]), 29)
    assert current["coverage_seconds"][0] == 30
    assert current["up"][0] == 10.0
    assert current["down"][0] == 10.0
    assert current["two_way"][0] == 14.142136
    assert current["up_per_minute"][0] == 14.142136
    assert current["down_per_minute"][0] == 14.142136
    assert current["two_way_per_minute"][0] == 20.0
    assert payload["game"]["two_way_per_minute"] == 20.0
    assert current["per_minute"][0] ** 2 == pytest.approx(
        current["up_per_minute"][0] ** 2 + current["down_per_minute"][0] ** 2,
        abs=0.0001,
    )
    assert current["two_way_per_minute"][0] ** 2 <= (
        current["up_per_minute"][0] ** 2 + current["down_per_minute"][0] ** 2 + 0.0001
    )


def test_game_total_retains_expired_rolling_moves_and_respects_as_of():
    frame = trades([0.5, 0.6, 0.5], seconds=[0, 10, 20])
    early, _ = sample(frame, 10)
    late, current = sample(frame, 100)
    assert early["game"]["up"] == 10.0
    assert early["game"]["down"] == 0.0
    assert late["game"]["up"] == 10.0
    assert late["game"]["down"] == 10.0
    assert late["game"]["two_way"] == 14.142136
    assert late["game"]["two_way_per_minute"] == pytest.approx(10.900086, abs=0.00001)
    assert current["two_way"][0] == 0.0


def test_game_total_uses_last_trade_in_each_sample_bucket():
    frame = trades([0.5, 0.6, 0.7, 0.8, 0.9], seconds=[0, 1, 2, 3, 4])
    payload, current = sample(frame, 4, cadence=2)
    assert payload["game"]["values"] == current["values"][0]
    assert payload["game"]["up"] == 28.284271


def test_selected_cadence_changes_sampled_prices():
    frame = trades([0.5, 0.6, 0.7, 0.8, 0.9], seconds=[0, 1, 2, 3, 4])
    payload, current = sample(frame, 4, cadence=2)
    assert payload["cadence_seconds"] == 2
    assert current["values"][0] == 28.284271
    assert current["two_way"][0] == 0.0


@pytest.mark.parametrize("cadence", [0, 61, 1.5, True])
def test_invalid_cadence_is_rejected(cadence):
    with pytest.raises(ValueError, match="cadence"):
        rolling_volatility_payload(trades([0.5]), cadence_seconds=cadence)


def test_new_ticker_figure_contains_only_its_own_history():
    def figure(frame, ticker):
        expanded = frame.assign(
            taker_prob=frame.target_raw_prob,
            maker_prob=frame.target_raw_prob,
            count=1,
        )
        return build_game_odds_figure(
            expanded, event_ticker=ticker, target_label="Team", show_fee_lines=False
        )

    first = figure(trades([0.5, 0.6], seconds=[0, 10]), "FIRST")
    second = figure(trades([0.7], start=START + pd.Timedelta(days=1)), "SECOND")
    assert first.layout.meta["rolling_volatility"]["samples"][0]["values"][0] > 0
    assert second.layout.meta["rolling_volatility"]["samples"][0]["values"] == [0.0] * 4
    assert second.layout.meta["rolling_volatility"]["samples"][0]["two_way"] == [0.0] * 4
    assert second.layout.meta["rolling_volatility"]["game"]["two_way"] == 0.0
    assert second.layout.meta["event_ticker"] == "SECOND"


def test_figure_uses_selected_cadence():
    frame = trades([0.5, 0.6, 0.7], seconds=[0, 1, 2]).assign(
        taker_prob=lambda data: data.target_raw_prob,
        maker_prob=lambda data: data.target_raw_prob,
        count=1,
    )
    figure = build_game_odds_figure(
        frame, event_ticker="EVENT", target_label="Team",
        volatility_cadence_seconds=2, show_fee_lines=False,
    )
    assert figure.layout.meta["rolling_volatility"]["cadence_seconds"] == 2
