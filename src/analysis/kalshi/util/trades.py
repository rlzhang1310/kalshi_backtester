"""DuckDB helpers for reading the published Kalshi family trade dataset."""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
import re

import duckdb


DEFAULT_TRADE_DATASET = (
    Path(__file__).resolve().parents[4] / "data" / "kalshi" / "trades_by_series"
)
FAMILY_RE = re.compile(r"^[A-Z0-9]{2,64}$")
TRADE_COLUMNS = (
    "trade_id,ticker,count,yes_price,no_price,"
    "lower(taker_side) AS taker_side,created_time"
)


def create_combined_trades_view(
    con: duckdb.DuckDBPyConnection,
    dataset_dir: Path | str,
    *,
    families: Iterable[str] | None = None,
) -> None:
    """Calibration view with authoritative taker-side price repair."""
    create_organized_trades_view(con, dataset_dir, families=families, repair_prices=True)


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _partition_key(dataset_dir: Path) -> str:
    metadata = dataset_dir / "_metadata" / "dataset.json"
    if not metadata.is_file():
        raise FileNotFoundError(
            f"Published Kalshi family trade dataset is missing at {dataset_dir}. "
            "Finish and verify the trade reorganization before local analysis."
        )
    info = json.loads(metadata.read_text(encoding="utf8"))
    key = info.get("partition_key")
    if info.get("validation_status") != "passed" or not info.get("run_id") or key not in {
        "series_ticker", "ticker_family"
    }:
        raise ValueError("Kalshi family dataset is not a verified published snapshot")
    if not (dataset_dir / "_metadata" / "family_catalog.parquet").is_file():
        raise FileNotFoundError("Published Kalshi family catalog is missing")
    return key


def _family(value: str) -> str:
    family = value.strip().upper()
    if not FAMILY_RE.fullmatch(family):
        raise ValueError(f"Invalid Kalshi ticker family: {value!r}")
    return family


def family_trade_files(dataset_dir: Path | str, family: str) -> list[Path]:
    """Return only committed base and scoped Parquet files in one partition."""
    root = Path(dataset_dir)
    folder = root / f"{_partition_key(root)}={_family(family)}"
    return sorted(
        path for path in folder.glob("*.parquet")
        if path.name.startswith(("part-", "scoped_"))
    )


def _scan_sql(
    dataset_dir: Path | str, families: Iterable[str] | None = None
) -> str | None:
    root = Path(dataset_dir)
    key = _partition_key(root)
    if families is None:
        # All-family analyses necessarily read the whole published dataset.
        globs = [
            root / f"{key}=*" / pattern
            for pattern in ("part-*.parquet", "scoped_*.parquet")
            if any(root.glob(f"{key}=*/{pattern}"))
        ]
        if not globs:
            raise FileNotFoundError(f"Published Kalshi dataset has no trade files at {root}")
    else:
        globs = []
        for family in sorted({_family(value) for value in families}):
            folder = root / f"{key}={family}"
            for pattern in ("part-*.parquet", "scoped_*.parquet"):
                if any(folder.glob(pattern)):
                    globs.append(folder / pattern)
        if not globs:
            return None
    paths = ", ".join(f"'{_sql_literal(str(path))}'" for path in globs)
    return f"read_parquet([{paths}], union_by_name=true, hive_partitioning=false)"


def create_organized_trades_view(
    con: duckdb.DuckDBPyConnection,
    dataset_dir: Path | str = DEFAULT_TRADE_DATASET,
    *,
    families: Iterable[str] | None = None,
    repair_prices: bool = False,
) -> None:
    """Create analysis_trades from the published base and scoped files.

    Selected families open only their own directories. An absent selected
    partition creates an empty view for analyses whose markets have no prints.
    Existing duplicate rows in the migration baseline are preserved.
    """
    scan = _scan_sql(dataset_dir, families)
    if scan is None:
        con.execute(
            "CREATE OR REPLACE TEMP VIEW analysis_trades AS "
            "SELECT CAST(NULL AS VARCHAR) trade_id, CAST(NULL AS VARCHAR) ticker, "
            "CAST(NULL AS BIGINT) count, CAST(NULL AS BIGINT) yes_price, "
            "CAST(NULL AS BIGINT) no_price, CAST(NULL AS VARCHAR) taker_side, "
            "CAST(NULL AS TIMESTAMPTZ) created_time WHERE false"
        )
        return
    if repair_prices:
        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW analysis_trades AS
            WITH stored AS (
                SELECT trade_id,ticker,count,yes_price,no_price,
                       lower(taker_side) AS taker_side,created_time
                FROM {scan}
            ), priced AS (
                SELECT *, CASE WHEN taker_side = 'yes' THEN yes_price
                               WHEN taker_side = 'no' THEN no_price END AS taker_price
                FROM stored
            )
            SELECT trade_id,ticker,count,
                   CASE WHEN taker_side = 'yes' THEN taker_price ELSE 100-taker_price END AS yes_price,
                   CASE WHEN taker_side = 'no' THEN taker_price ELSE 100-taker_price END AS no_price,
                   taker_side,created_time
            FROM priced WHERE taker_side IN ('yes','no') AND taker_price BETWEEN 0 AND 100
            """
        )
    else:
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW analysis_trades AS SELECT {TRADE_COLUMNS} FROM {scan}"
        )


def create_family_trades_view(
    con: duckdb.DuckDBPyConnection, dataset_dir: Path | str, family: str
) -> None:
    """Create a raw trade view for one family, including scoped appends."""
    if not family_trade_files(dataset_dir, family):
        raise FileNotFoundError(f"No published family partition for {_family(family)}")
    create_organized_trades_view(con, dataset_dir, families=[family])
