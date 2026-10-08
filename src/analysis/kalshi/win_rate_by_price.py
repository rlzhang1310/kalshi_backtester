"""Analyze win rate by price with market-clustered confidence intervals."""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from src.analysis.kalshi.util.sports_markets import (
    MARKET_CATEGORY_LABELS,
    SPORT_LABELS,
    market_category_case_sql,
    normalize_market_category,
    normalize_sport,
    sport_case_sql,
)
from src.analysis.kalshi.util.trades import create_combined_trades_view
from src.common.analysis import Analysis, AnalysisOutput
from src.common.interfaces.chart import ChartConfig, ChartType, UnitType


class WinRateByPriceAnalysis(Analysis):
    """Analyze win rate by price to assess market calibration on Kalshi."""

    def __init__(
        self,
        trades_dir: Path | str | None = None,
        markets_dir: Path | str | None = None,
        sport: str | None = None,
        market_category: str | None = None,
    ):
        super().__init__(
            name="win_rate_by_price",
            description="Win rate vs price by selectable sport and market category",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.trades_dir = Path(trades_dir or base_dir / "data" / "kalshi" / "trades_by_series")
        self.markets_dir = Path(markets_dir or base_dir / "data" / "kalshi" / "markets")
        self.sport: str | None = None
        self.market_category: str | None = None

        if (sport is None) != (market_category is None):
            raise ValueError("sport and market_category must be provided together")
        if sport is not None and market_category is not None:
            self.configure_filters(sport, market_category)

    def configure_filters(self, sport: str, market_category: str) -> None:
        """Set validated sport/category filters and a selection-specific name."""
        sport_slug = normalize_sport(sport)
        category_slug = normalize_market_category(market_category)
        self.sport = sport_slug
        self.market_category = category_slug
        self.name = f"win_rate_by_price_{sport_slug}_{category_slug}"
        if category_slug == "all":
            self.description = (
                "Win rate vs price market calibration analysis for all "
                f"{SPORT_LABELS[sport_slug]} markets"
            )
        else:
            self.description = (
                "Win rate vs price market calibration analysis for "
                f"{SPORT_LABELS[sport_slug]} {MARKET_CATEGORY_LABELS[category_slug]} markets"
            )

    def run(self) -> AnalysisOutput:
        """Execute the analysis and return outputs."""
        con = duckdb.connect()

        markets_glob = str(self.markets_dir / "*.parquet").replace("'", "''")
        filter_sql = ""
        if self.sport is not None and self.market_category is not None:
            sport_expression = sport_case_sql("event_ticker")
            sport_slug = self.sport.replace("'", "''")
            filter_sql = f"""
                  AND ({sport_expression}) = '{sport_slug}'
            """
            if self.market_category != "all":
                category_expression = market_category_case_sql(
                    "event_ticker",
                    "title",
                    "yes_sub_title",
                    "no_sub_title",
                )
                category_slug = self.market_category.replace("'", "''")
                filter_sql += f"""
                  AND ({category_expression}) = '{category_slug}'
                """

        # Materialize a filtered selection once so the large market archive is
        # not copied into Python or re-scanned for both the count and trade join.
        con.execute(
            f"""
            CREATE TEMP TABLE selected_markets AS
            SELECT ticker, event_ticker, result, volume
            FROM read_parquet('{markets_glob}', union_by_name = true)
            WHERE status = 'finalized'
              AND result IN ('yes', 'no')
              {filter_sql}
            """
        )

        families = None if self.sport is None else [
            row[0] for row in con.execute(
                "SELECT DISTINCT split_part(event_ticker, '-', 1) FROM selected_markets"
            ).fetchall() if row[0]
        ]
        create_combined_trades_view(con, self.trades_dir, families=families)

        market_summary = con.execute(
            "SELECT COUNT(*) AS total_markets FROM selected_markets"
        ).fetchone()

        df = con.execute(
            """
            WITH all_positions AS (
                -- Taker side
                SELECT
                    m.ticker AS market_ticker,
                    CASE WHEN t.taker_side = 'yes' THEN t.yes_price ELSE t.no_price END AS price,
                    CASE WHEN t.taker_side = m.result THEN 1 ELSE 0 END AS won,
                    t.count AS contracts,
                    t.count * (CASE WHEN t.taker_side = 'yes' THEN t.yes_price ELSE t.no_price END) / 100.0 AS trade_volume_usd,
                    m.volume AS market_volume_usd
                FROM analysis_trades t
                INNER JOIN selected_markets m ON t.ticker = m.ticker

                UNION ALL

                -- Maker side (counterparty)
                SELECT
                    m.ticker AS market_ticker,
                    CASE WHEN t.taker_side = 'yes' THEN t.no_price ELSE t.yes_price END AS price,
                    CASE WHEN t.taker_side != m.result THEN 1 ELSE 0 END AS won,
                    t.count AS contracts,
                    t.count * (CASE WHEN t.taker_side = 'yes' THEN t.no_price ELSE t.yes_price END) / 100.0 AS trade_volume_usd,
                    m.volume AS market_volume_usd
                FROM analysis_trades t
                INNER JOIN selected_markets m ON t.ticker = m.ticker
            ),
            market_price_positions AS (
                SELECT
                    price,
                    market_ticker,
                    COUNT(*) AS market_positions,
                    SUM(won) AS market_wins,
                    SUM(trade_volume_usd) AS trade_volume_usd,
                    SUM(market_volume_usd) AS market_volume_usd
                FROM all_positions
                GROUP BY price, market_ticker
            ),
            price_summary AS (
                SELECT
                    price,
                    SUM(market_positions) AS total_trades,
                    SUM(market_wins) AS wins,
                    100.0 * SUM(market_wins) / SUM(market_positions) AS win_rate,
                    COUNT(*) AS unique_markets,
                    SUM(trade_volume_usd) AS trade_volume_usd,
                    SUM(market_volume_usd) AS market_volume_usd
                FROM market_price_positions
                GROUP BY price
            )
            SELECT
                summary.price,
                summary.total_trades,
                summary.wins,
                summary.win_rate,
                summary.unique_markets,
                summary.trade_volume_usd,
                summary.market_volume_usd,
                SUM(
                    POWER(
                        market_cluster.market_wins
                        - market_cluster.market_positions
                            * summary.wins / summary.total_trades,
                        2
                    )
                ) AS cluster_residual_sum_squares
            FROM price_summary summary
            INNER JOIN market_price_positions market_cluster USING (price)
            GROUP BY
                summary.price,
                summary.total_trades,
                summary.wins,
                summary.win_rate,
                summary.unique_markets,
                summary.trade_volume_usd,
                summary.market_volume_usd
            ORDER BY summary.price
            """
        ).df()

        self._add_market_clustered_confidence(df)

        total_markets = int(market_summary[0] or 0)
        total_trades = int(df["total_trades"].sum()) if not df.empty else 0

        fig = self._create_figure(df, total_markets, total_trades)
        chart = self._create_chart(df)

        return AnalysisOutput(
            figure=fig,
            data=df,
            chart=chart,
            metadata={
                "sport": self.sport,
                "market_category": self.market_category,
                "total_markets": total_markets,
                "total_positions": total_trades,
            },
        )

    @staticmethod
    def _add_market_clustered_confidence(df: pd.DataFrame) -> None:
        """Add a 95% market-clustered t interval to each price row.

        The point estimate remains position-weighted. Trades from the same
        ticker form one cluster, and the t distribution uses M - 1 degrees of
        freedom where M is the number of distinct markets at that price.
        """
        confidence_columns = (
            "clustered_standard_error_pct",
            "t_critical_95",
            "confidence_margin_pct",
            "ci_lower",
            "ci_upper",
            "ci_lower_pct",
            "ci_upper_pct",
        )

        if df.empty:
            df["degrees_of_freedom"] = pd.Series(dtype="int64")
            df["confidence_status"] = pd.Series(dtype="object")
            for column in confidence_columns:
                df[column] = pd.Series(dtype=float)
            df.drop(
                columns=["cluster_residual_sum_squares"], errors="ignore", inplace=True
            )
            return

        market_count = df["unique_markets"].astype(int)
        degrees_of_freedom = market_count - 1
        win_rate_prop = df["win_rate"] / 100.0
        enough_markets = degrees_of_freedom > 0
        interior_estimate = (win_rate_prop > 0.0) & (win_rate_prop < 1.0)
        nondegenerate_variance = df["cluster_residual_sum_squares"] > 1e-12
        valid = enough_markets & interior_estimate & nondegenerate_variance

        df["degrees_of_freedom"] = degrees_of_freedom
        df["confidence_status"] = "available"
        df.loc[~enough_markets, "confidence_status"] = "insufficient_markets"
        df.loc[enough_markets & ~interior_estimate, "confidence_status"] = (
            "boundary_estimate"
        )
        df.loc[
            enough_markets & interior_estimate & ~nondegenerate_variance,
            "confidence_status",
        ] = "degenerate_cluster_variance"
        for column in confidence_columns:
            df[column] = np.nan
        df.loc[enough_markets, "t_critical_95"] = student_t.ppf(
            0.975,
            degrees_of_freedom[enough_markets],
        )

        cluster_variance = (
            market_count[valid]
            / degrees_of_freedom[valid]
            * df.loc[valid, "cluster_residual_sum_squares"]
            / np.square(df.loc[valid, "total_trades"])
        )
        standard_error = np.sqrt(cluster_variance)
        t_critical = df.loc[valid, "t_critical_95"]
        margin = t_critical * standard_error

        df.loc[valid, "clustered_standard_error_pct"] = standard_error * 100.0
        df.loc[valid, "confidence_margin_pct"] = margin * 100.0
        df.loc[valid, "ci_lower"] = np.clip(
            win_rate_prop[valid] - margin,
            0.0,
            1.0,
        )
        df.loc[valid, "ci_upper"] = np.clip(
            win_rate_prop[valid] + margin,
            0.0,
            1.0,
        )
        df.loc[valid, "ci_lower_pct"] = df.loc[valid, "ci_lower"] * 100.0
        df.loc[valid, "ci_upper_pct"] = df.loc[valid, "ci_upper"] * 100.0
        df.drop(columns=["cluster_residual_sum_squares"], inplace=True)

    def _selection_label(self) -> str | None:
        if self.sport is None or self.market_category is None:
            return None
        if self.market_category == "all":
            return f"{SPORT_LABELS[self.sport]} All Markets"
        return (
            f"{SPORT_LABELS[self.sport]} {MARKET_CATEGORY_LABELS[self.market_category]}"
        )

    def _create_figure(
        self, df: pd.DataFrame, total_markets: int, total_trades: int
    ) -> plt.Figure:
        """Create the matplotlib figure."""
        fig, ax = plt.subplots(figsize=(10, 10))
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
        selection_label = self._selection_label()
        title_prefix = f"{selection_label} " if selection_label else ""
        ax.set_title(f"{title_prefix}Win Rate vs Price: Market Calibration")
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
                label="95% CI (market-clustered)",
            )

        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)
        plt.tight_layout(rect=(0, 0, 1, 0.95))
        return fig

    def _create_chart(self, df: pd.DataFrame) -> ChartConfig:
        """Create the chart configuration for web display."""

        def rounded_or_none(value: float) -> float | None:
            return None if pd.isna(value) else round(value, 2)

        chart_data = [
            {
                "price": int(row["price"]),
                "actual": round(row["win_rate"], 2),
                "95% CI Lower": rounded_or_none(row["ci_lower_pct"]),
                "95% CI Upper": rounded_or_none(row["ci_upper_pct"]),
                "Unique Markets": int(row["unique_markets"]),
                "Degrees of Freedom": int(row["degrees_of_freedom"]),
                "95% Confidence Margin": rounded_or_none(row["confidence_margin_pct"]),
                "Confidence Status": row["confidence_status"],
                "implied": int(row["price"]),
            }
            for _, row in df.iterrows()
            if 1 <= row["price"] <= 99
        ]

        selection_label = self._selection_label()
        title_prefix = f"{selection_label} " if selection_label else ""

        return ChartConfig(
            type=ChartType.LINE,
            data=chart_data,
            xKey="price",
            yKeys=["actual", "95% CI Lower", "95% CI Upper", "implied"],
            title=f"Actual {title_prefix}Win Rate vs Contract Price",
            caption=(
                "Pointwise 95% confidence uses market-clustered standard errors "
                "and a Student-t critical value with degrees of freedom equal "
                "to distinct traded markets minus one. Intervals are unavailable "
                "for insufficient or degenerate market variation."
            ),
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
