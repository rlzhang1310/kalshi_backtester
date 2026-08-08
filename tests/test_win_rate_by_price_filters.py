from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pytest

from src.analysis.kalshi.win_rate_by_price import WinRateByPriceAnalysis


def _analysis_kwargs(tmp_path: Path) -> dict[str, Path]:
    """Write a tiny archive containing every important filtering edge case."""
    markets_dir = tmp_path / "markets"
    trades_dir = tmp_path / "legacy_trades"
    global_trades_dir = tmp_path / "global_trades"
    markets_dir.mkdir()
    trades_dir.mkdir()
    global_trades_dir.mkdir()

    pd.DataFrame(
        [
            # The first selected market has repeated trades at the same bins.
            {
                "ticker": "NFL-SPREAD-TRADED",
                "event_ticker": "KXNFLSPREAD-26JAN01BUFNYJ",
                "title": "Buffalo spread",
                "yes_sub_title": "Buffalo -3.5",
                "no_sub_title": "New York +3.5",
                "status": "finalized",
                "result": "yes",
                "volume": 100.0,
            },
            # A finalized matching market must count even without a trade.
            {
                "ticker": "NFL-SPREAD-NO-TRADE",
                "event_ticker": "KXNFLSPREAD-26JAN02MIABOS",
                "title": "Miami spread",
                "yes_sub_title": "Miami -1.5",
                "no_sub_title": "Boston +1.5",
                "status": "finalized",
                "result": "no",
                "volume": 25.0,
            },
            # A second traded ticker at 40/60 gives those bins two independent
            # market clusters even though the first ticker traded twice.
            {
                "ticker": "NFL-SPREAD-SECOND-TRADED",
                "event_ticker": "KXNFLSPREAD-26JAN02LAXNYG",
                "title": "Los Angeles spread",
                "yes_sub_title": "Los Angeles -2.5",
                "no_sub_title": "New York +2.5",
                "status": "finalized",
                "result": "yes",
                "volume": 75.0,
            },
            # Same category, wrong sport.
            {
                "ticker": "NBA-SPREAD",
                "event_ticker": "KXNBASPREAD-26JAN01NYKBOS",
                "title": "New York spread",
                "yes_sub_title": "New York +2.5",
                "no_sub_title": "Boston -2.5",
                "status": "finalized",
                "result": "yes",
                "volume": 300.0,
            },
            # Same sport, wrong category.
            {
                "ticker": "NFL-MONEYLINE",
                "event_ticker": "KXNFLGAME-26JAN03DALPHI",
                "title": "Who will win?",
                "yes_sub_title": "Dallas",
                "no_sub_title": "Philadelphia",
                "status": "finalized",
                "result": "yes",
                "volume": 400.0,
            },
            # Matching classification is not enough: it must be finalized.
            {
                "ticker": "NFL-SPREAD-ACTIVE",
                "event_ticker": "KXNFLSPREAD-26JAN04LACHI",
                "title": "Los Angeles spread",
                "yes_sub_title": "Los Angeles -4.5",
                "no_sub_title": "Chicago +4.5",
                "status": "active",
                "result": "yes",
                "volume": 500.0,
            },
            # Nor may a finalized market have an unresolved/non-binary result.
            {
                "ticker": "NFL-SPREAD-VOID",
                "event_ticker": "KXNFLSPREAD-26JAN05SEASFO",
                "title": "Seattle spread",
                "yes_sub_title": "Seattle +1.5",
                "no_sub_title": "San Francisco -1.5",
                "status": "finalized",
                "result": "void",
                "volume": 600.0,
            },
            # Mixed families are excluded from narrow categories but included
            # when the user explicitly selects All for this sport.
            {
                "ticker": "NFL-MIXED-TRADED",
                "event_ticker": "KXMVENFLSINGLEGAME-26JAN06",
                "title": "Mixed NFL legs",
                "yes_sub_title": "Buffalo wins and total over 40.5",
                "no_sub_title": "Other",
                "status": "finalized",
                "result": "yes",
                "volume": 50.0,
            },
        ]
    ).to_parquet(markets_dir / "markets.parquet", index=False)

    pd.DataFrame(
        [
            # Stored counter-prices are intentionally bad. The taker price is
            # authoritative, so these become 60/40 YES/NO positions.
            {
                "trade_id": "selected-yes-taker",
                "ticker": "NFL-SPREAD-TRADED",
                "count": 2,
                "yes_price": 60,
                "no_price": 7,
                "taker_side": "YES",
                "created_time": "2026-01-01T00:00:00Z",
            },
            {
                "trade_id": "selected-no-taker",
                "ticker": "NFL-SPREAD-TRADED",
                "count": 3,
                "yes_price": 12,
                "no_price": 60,
                "taker_side": "no",
                "created_time": "2026-01-01T00:00:01Z",
            },
            {
                "trade_id": "selected-second-market",
                "ticker": "NFL-SPREAD-SECOND-TRADED",
                "count": 1,
                "yes_price": 60,
                "no_price": 40,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:01Z",
            },
            {
                "trade_id": "wrong-sport",
                "ticker": "NBA-SPREAD",
                "count": 10,
                "yes_price": 70,
                "no_price": 30,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:02Z",
            },
            {
                "trade_id": "wrong-category",
                "ticker": "NFL-MONEYLINE",
                "count": 10,
                "yes_price": 80,
                "no_price": 20,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:03Z",
            },
            {
                "trade_id": "not-finalized",
                "ticker": "NFL-SPREAD-ACTIVE",
                "count": 10,
                "yes_price": 90,
                "no_price": 10,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:04Z",
            },
            {
                "trade_id": "not-resolved",
                "ticker": "NFL-SPREAD-VOID",
                "count": 10,
                "yes_price": 95,
                "no_price": 5,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:05Z",
            },
            {
                "trade_id": "mixed-family",
                "ticker": "NFL-MIXED-TRADED",
                "count": 1,
                "yes_price": 55,
                "no_price": 45,
                "taker_side": "yes",
                "created_time": "2026-01-01T00:00:06Z",
            },
        ]
    ).to_parquet(global_trades_dir / "trades.parquet", index=False)

    return {
        "trades_dir": trades_dir,
        "global_trades_dir": global_trades_dir,
        "markets_dir": markets_dir,
    }


def test_run_filters_by_sport_and_category_and_builds_both_positions(
    tmp_path: Path,
) -> None:
    analysis = WinRateByPriceAnalysis(
        **_analysis_kwargs(tmp_path),
        sport="National Football League",
        market_category="spreads",
    )

    output = analysis.run()
    try:
        assert output.metadata == {
            "sport": "nfl",
            "market_category": "spread",
            "total_markets": 3,
            "total_positions": 6,
        }

        data = output.data.set_index("price")
        assert "cluster_residual_sum_squares" not in data.columns
        assert list(data.index) == [40, 60]
        assert data["total_trades"].to_dict() == {40: 3, 60: 3}
        assert data["wins"].to_dict() == {40: pytest.approx(1), 60: pytest.approx(2)}
        assert data["win_rate"].to_dict() == {
            40: pytest.approx(100.0 / 3.0),
            60: pytest.approx(200.0 / 3.0),
        }
        # Counts affect notional volume, while each trade contributes one maker
        # and one taker position to the calibration sample.
        assert data["trade_volume_usd"].to_dict() == {
            40: pytest.approx(2.4),
            60: pytest.approx(3.6),
        }
        assert data["unique_markets"].to_dict() == {40: 2, 60: 2}
        assert data["degrees_of_freedom"].to_dict() == {40: 1, 60: 1}
        assert data["confidence_status"].to_dict() == {
            40: "available",
            60: "available",
        }
        assert data["clustered_standard_error_pct"].to_dict() == {
            40: pytest.approx(200.0 / 9.0),
            60: pytest.approx(200.0 / 9.0),
        }
        assert data["t_critical_95"].to_dict() == {
            40: pytest.approx(12.7062047364),
            60: pytest.approx(12.7062047364),
        }
        assert data["confidence_margin_pct"].to_dict() == {
            40: pytest.approx(282.36010525),
            60: pytest.approx(282.36010525),
        }
        assert data["ci_lower_pct"].to_dict() == {
            40: pytest.approx(0.0),
            60: pytest.approx(0.0),
        }
        assert data["ci_upper_pct"].to_dict() == {
            40: pytest.approx(100.0),
            60: pytest.approx(100.0),
        }

        assert analysis.name == "win_rate_by_price_nfl_spread"
        assert output.figure.axes[0].get_title() == (
            "NFL Spread Win Rate vs Price: Market Calibration"
        )
        assert output.chart.title == "Actual NFL Spread Win Rate vs Contract Price"
        chart_row = next(row for row in output.chart.data if row["price"] == 40)
        assert chart_row["Unique Markets"] == 2
        assert chart_row["Degrees of Freedom"] == 1
        assert chart_row["95% Confidence Margin"] == pytest.approx(282.36)
        assert chart_row["Confidence Status"] == "available"
        assert "market-clustered" in output.chart.caption
    finally:
        plt.close(output.figure)


def test_run_with_empty_sport_category_intersection_is_safe(tmp_path: Path) -> None:
    # NBA and moneyline each occur in the archive, but no market is both.
    analysis = WinRateByPriceAnalysis(
        **_analysis_kwargs(tmp_path),
        sport="NBA",
        market_category="game winner",
    )

    output = analysis.run()
    try:
        assert output.data.empty
        assert output.metadata == {
            "sport": "nba",
            "market_category": "moneyline",
            "total_markets": 0,
            "total_positions": 0,
        }
        assert output.chart.data == []
        assert {
            "unique_markets",
            "degrees_of_freedom",
            "confidence_status",
            "clustered_standard_error_pct",
            "t_critical_95",
            "confidence_margin_pct",
            "ci_lower_pct",
            "ci_upper_pct",
        }.issubset(output.data.columns)
        assert analysis.name == "win_rate_by_price_nba_moneyline"
        assert output.figure.axes[0].get_title() == (
            "NBA Moneyline Win Rate vs Price: Market Calibration"
        )
        assert output.chart.title == ("Actual NBA Moneyline Win Rate vs Contract Price")
    finally:
        plt.close(output.figure)


def test_boundary_win_rate_marks_clustered_confidence_unavailable() -> None:
    data = pd.DataFrame(
        [
            {
                "price": 10,
                "total_trades": 2,
                "wins": 0,
                "win_rate": 0.0,
                "unique_markets": 2,
                "cluster_residual_sum_squares": 0.0,
            }
        ]
    )

    WinRateByPriceAnalysis._add_market_clustered_confidence(data)

    row = data.iloc[0]
    assert row["degrees_of_freedom"] == 1
    assert row["confidence_status"] == "boundary_estimate"
    assert row["t_critical_95"] == pytest.approx(12.7062047364)
    assert pd.isna(row["clustered_standard_error_pct"])
    assert pd.isna(row["confidence_margin_pct"])
    assert pd.isna(row["ci_lower_pct"])
    assert pd.isna(row["ci_upper_pct"])


def test_zero_cluster_variance_marks_confidence_unavailable() -> None:
    data = pd.DataFrame(
        [
            {
                "price": 50,
                "total_trades": 4,
                "wins": 2,
                "win_rate": 50.0,
                "unique_markets": 2,
                "cluster_residual_sum_squares": 0.0,
            }
        ]
    )

    WinRateByPriceAnalysis._add_market_clustered_confidence(data)

    row = data.iloc[0]
    assert row["degrees_of_freedom"] == 1
    assert row["confidence_status"] == "degenerate_cluster_variance"
    assert pd.isna(row["clustered_standard_error_pct"])
    assert pd.isna(row["confidence_margin_pct"])
    assert pd.isna(row["ci_lower_pct"])
    assert pd.isna(row["ci_upper_pct"])


def test_all_category_applies_only_the_sport_filter(tmp_path: Path) -> None:
    analysis = WinRateByPriceAnalysis(
        **_analysis_kwargs(tmp_path),
        sport="NFL",
        market_category="all",
    )

    output = analysis.run()
    try:
        # Three spreads, one moneyline, and one unclassified mixed NFL market
        # are finalized. Active, void, and NBA markets remain excluded.
        assert output.metadata == {
            "sport": "nfl",
            "market_category": "all",
            "total_markets": 5,
            "total_positions": 10,
        }
        assert {45, 55}.issubset(set(output.data["price"]))
        single_market_row = output.data.set_index("price").loc[45]
        assert single_market_row["unique_markets"] == 1
        assert single_market_row["degrees_of_freedom"] == 0
        assert single_market_row["confidence_status"] == "insufficient_markets"
        assert pd.isna(single_market_row["confidence_margin_pct"])
        assert pd.isna(single_market_row["ci_lower_pct"])
        chart_row = next(row for row in output.chart.data if row["price"] == 45)
        assert chart_row["95% CI Lower"] is None
        assert chart_row["95% CI Upper"] is None
        assert chart_row["95% Confidence Margin"] is None
        assert chart_row["Confidence Status"] == "insufficient_markets"
        assert "NaN" not in output.chart.to_json()
        assert analysis.name == "win_rate_by_price_nfl_all"
        assert output.figure.axes[0].get_title() == (
            "NFL All Markets Win Rate vs Price: Market Calibration"
        )
        assert output.chart.title == (
            "Actual NFL All Markets Win Rate vs Contract Price"
        )
    finally:
        plt.close(output.figure)
