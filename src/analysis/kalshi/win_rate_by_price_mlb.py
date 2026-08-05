"""Analyze win rate by price for MLB markets only."""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.analysis.kalshi.util.categories import get_hierarchy
from src.analysis.kalshi.util.trades import create_combined_trades_view
from src.common.analysis import Analysis, AnalysisOutput
from src.common.interfaces.chart import ChartConfig, ChartType, UnitType


class WinRateByPriceMLBAnalysis(Analysis):
    """Analyze win rate vs price calibration for MLB markets on Kalshi."""

    def __init__(
        self,
        trades_dir: Path | str | None = None,
        global_trades_dir: Path | str | None = None,
        markets_dir: Path | str | None = None,
    ):
        super().__init__(
            name="win_rate_by_price_mlb",
            description="Win rate vs price market calibration analysis for MLB markets",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.trades_dir = Path(trades_dir or base_dir / "data" / "kalshi" / "trades")
        self.global_trades_dir = Path(global_trades_dir or base_dir / "data" / "kalshi" / "trades_global_staging")
        self.markets_dir = Path(markets_dir or base_dir / "data" / "kalshi" / "markets")

    def run(self) -> AnalysisOutput:
        """Execute the analysis and return outputs."""
        con = duckdb.connect()
        create_combined_trades_view(con, self.trades_dir, self.global_trades_dir)

        markets_df = con.execute(
            f"""
            SELECT ticker, event_ticker, result
            FROM '{self.markets_dir}/*.parquet'
            WHERE status = 'finalized'
              AND result IN ('yes', 'no')
            """
        ).df()

        if markets_df.empty:
            df = pd.DataFrame(columns=["price", "total_trades", "wins", "win_rate"])
            fig = self._create_figure(df, 0, 0)
            chart = self._create_chart(df)
            return AnalysisOutput(figure=fig, data=df, chart=chart)

        markets_df["event_ticker"] = markets_df["event_ticker"].fillna("").astype(str)
        markets_df["is_mlb"] = markets_df["event_ticker"].apply(self._is_mlb_category)
        mlb_tickers = markets_df.loc[markets_df["is_mlb"], "ticker"].tolist()

        if not mlb_tickers:
            df = pd.DataFrame(columns=["price", "total_trades", "wins", "win_rate"])
            fig = self._create_figure(df, 0, 0)
            chart = self._create_chart(df)
            return AnalysisOutput(figure=fig, data=df, chart=chart)

        ticker_sql = ", ".join(f"'{ticker.replace(chr(39), chr(39) * 2)}'" for ticker in mlb_tickers)

        df = con.execute(
            f"""
            WITH resolved_markets AS (
                SELECT ticker, result
                FROM '{self.markets_dir}/*.parquet'
                WHERE status = 'finalized'
                  AND result IN ('yes', 'no')
                  AND ticker IN ({ticker_sql})
            ),
            all_positions AS (
                SELECT
                    CASE WHEN t.taker_side = 'yes' THEN t.yes_price ELSE t.no_price END AS price,
                    CASE WHEN t.taker_side = m.result THEN 1 ELSE 0 END AS won
                FROM analysis_trades t
                INNER JOIN resolved_markets m ON t.ticker = m.ticker

                UNION ALL

                SELECT
                    CASE WHEN t.taker_side = 'yes' THEN t.no_price ELSE t.yes_price END AS price,
                    CASE WHEN t.taker_side != m.result THEN 1 ELSE 0 END AS won
                FROM analysis_trades t
                INNER JOIN resolved_markets m ON t.ticker = m.ticker
            )
            SELECT
                price,
                COUNT(*) AS total_trades,
                SUM(won) AS wins,
                100.0 * SUM(won) / COUNT(*) AS win_rate
            FROM all_positions
            GROUP BY price
            ORDER BY price
            """
        ).df()

        if df.empty:
            df = pd.DataFrame(columns=["price", "total_trades", "wins", "win_rate"])

        if not df.empty:
            win_rate_prop = df["wins"] / df["total_trades"]
            se = np.sqrt(win_rate_prop * (1 - win_rate_prop) / df["total_trades"])
            df["ci_lower"] = np.clip(win_rate_prop - 1.96 * se, 0.0, 1.0)
            df["ci_upper"] = np.clip(win_rate_prop + 1.96 * se, 0.0, 1.0)
            df["ci_lower_pct"] = df["ci_lower"] * 100.0
            df["ci_upper_pct"] = df["ci_upper"] * 100.0
        else:
            df["ci_lower"] = pd.Series(dtype=float)
            df["ci_upper"] = pd.Series(dtype=float)
            df["ci_lower_pct"] = pd.Series(dtype=float)
            df["ci_upper_pct"] = pd.Series(dtype=float)

        total_markets = int(len(mlb_tickers)) if not df.empty else 0
        total_trades = int(df["total_trades"].sum()) if not df.empty else 0

        fig = self._create_figure(df, total_markets, total_trades)
        chart = self._create_chart(df)

        return AnalysisOutput(figure=fig, data=df, chart=chart)

    def _is_mlb_category(self, event_ticker: str) -> bool:
        """Return True when the market event maps to MLB via the category helper."""
        if not event_ticker:
            return False

        group, category, _ = get_hierarchy(event_ticker)
        return group.lower() == "sports" and category.lower() == "mlb"

    def _create_figure(self, df: pd.DataFrame, total_markets: int, total_trades: int) -> plt.Figure:
        """Create the matplotlib figure."""
        fig, ax = plt.subplots(figsize=(10, 10))
        if not df.empty:
            ax.scatter(
                df["price"],
                df["win_rate"],
                s=30,
                alpha=0.8,
                color="#4C72B0",
                edgecolors="none",
            )

        ax.plot(
            [0, 100],
            [0, 100],
            linestyle="--",
            color="#D65F5F",
            linewidth=1.5,
            label="Perfect calibration",
        )
        ax.set_xlabel("Contract Price (cents)")
        ax.set_ylabel("Win Rate (%)")
        ax.set_title("MLB Win Rate vs Price: Market Calibration")
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.set_xticks(range(0, 101, 10))
        ax.set_xticks(range(0, 101, 1), minor=True)
        ax.set_yticks(range(0, 101, 10))
        ax.set_yticks(range(0, 101, 1), minor=True)
        ax.set_aspect("equal")
        summary = f"Markets: {total_markets:,}   Trades: {total_trades:,}"
        fig.text(0.5, 0.97, summary, ha="center", va="top", fontsize=12, weight="bold")

        if not df.empty:
            ax.fill_between(
                df["price"],
                df["ci_lower_pct"],
                df["ci_upper_pct"],
                color="#4C72B0",
                alpha=0.15,
                label="95% CI",
            )
        ax.legend(loc="upper left")
        plt.tight_layout()
        return fig

    def _create_chart(self, df: pd.DataFrame) -> ChartConfig:
        """Create the chart configuration for web display."""
        chart_data = [
            {
                "price": int(row["price"]),
                "actual": round(row["win_rate"], 2),
                "95% CI Lower": round(row["ci_lower_pct"], 2),
                "95% CI Upper": round(row["ci_upper_pct"], 2),
                "implied": int(row["price"]),
            }
            for _, row in df.iterrows()
            if 1 <= row["price"] <= 99
        ]

        return ChartConfig(
            type=ChartType.LINE,
            data=chart_data,
            xKey="price",
            yKeys=["actual", "95% CI Lower", "95% CI Upper", "implied"],
            title="Actual MLB Win Rate vs Contract Price",
            strokeDasharrays=[None, "5 5", "5 5", "5 5"],
            yUnit=UnitType.PERCENT,
            colors={
                "actual": "#4C72B0",
                "95% CI Lower": "#4C72B0",
                "95% CI Upper": "#4C72B0",
                "implied": "#D65F5F",
            },
            xLabel="Contract Price (cents)",
            yLabel="Actual Win Rate (%)",
        )
