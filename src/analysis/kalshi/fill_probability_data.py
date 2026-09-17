"""Prepare a reusable family-wide fill research dataset from local trades."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Callable

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.analysis.kalshi.fill_probability import (
    add_normalized_time,
    FillDataset,
    build_fill_dataset,
    normalize_family,
    normalize_timings,
    normalize_trade_prices,
    utc,
)
from src.analysis.kalshi.local_game_data import (
    DEFAULT_DATA_DIR,
    FILE_BATCH_SIZE,
    LocalGameStore,
    _files,
)
from src.indexers.kalshi.client import KalshiClient


def _pages(client, endpoint: str, key: str, params: dict):
    params = params.copy()
    seen = set()
    while True:
        data = client.http.get(endpoint, params=params)
        yield from data.get(key, [])
        cursor = data.get("cursor")
        if not cursor:
            return
        if cursor in seen:
            raise ValueError(f"Repeated pagination cursor from {endpoint}.")
        seen.add(cursor)
        params["cursor"] = cursor


def fetch_family_timings(
    markets: pd.DataFrame,
    *,
    family: str,
    client=None,
    progress: Callable[[str], None] = print,
) -> pd.DataFrame:
    """Fetch actual settlement and linked scheduled starts in bulk, read-only.

    Never replace missing start/settlement with first/last trade, open, close,
    expected expiration, or the milestone end_date.
    """
    family = normalize_family(family)
    owns_client = client is None
    client = client or KalshiClient()
    try:
        wanted = set(markets.ticker)
        enriched = {}
        for endpoint in ("/markets", "/historical/markets"):
            progress(f"Reading {endpoint} metadata for {family}...")
            for market in _pages(
                client, endpoint, "markets", {"series_ticker": family, "limit": 1000}
            ):
                if market["ticker"] in wanted:
                    enriched.setdefault(market["ticker"], market)

        events = set(markets.event_ticker)
        # Discover the provider's milestone type from an actual member of the family.
        sample = client.get_event_milestones(sorted(events)[0])
        milestone_types = {
            milestone.get("type") for milestone in sample if milestone.get("type")
        }
        starts: dict[str, list[tuple[int, pd.Timestamp]]] = {
            event: [] for event in events
        }

        def record_start(milestone):
            value = utc(milestone.get("start_date"))
            if not isinstance(value, pd.Timestamp) or pd.isna(value):
                return
            primary = set(milestone.get("primary_event_tickers", []))
            linked = primary | set(milestone.get("related_event_tickers", []))
            for event in linked & events:
                starts[event].append((0 if event in primary else 1, value))

        for milestone in sample:
            record_start(milestone)
        # A bulk scan avoids a separate request for every outcome or game.
        earliest = utc(markets.open_time).min() if "open_time" in markets else pd.NaT
        params = {"category": "Sports", "limit": 500}
        if pd.notna(earliest):
            params["minimum_start_date"] = (earliest - pd.Timedelta(days=7)).isoformat()
        for milestone_type in sorted(milestone_types) or [None]:
            filters = {**params, **({"type": milestone_type} if milestone_type else {})}
            progress(f"Reading sports milestones ({milestone_type or 'all types'})...")
            for milestone in _pages(client, "/milestones", "milestones", filters):
                record_start(milestone)
        # Missing/ambiguous clocks stay visible in the audit instead of silently
        # inventing times or issuing thousands of unbounded individual requests.
        rows = []
        for local in markets.drop_duplicates("ticker").itertuples():
            live = enriched.get(local.ticker, {})
            candidates = starts.get(local.event_ticker, [])
            if candidates:
                priority = min(item[0] for item in candidates)
                values = {value for rank, value in candidates if rank == priority}
            else:
                values = set()
            rows.append(
                {
                    "ticker": local.ticker,
                    "event_ticker": local.event_ticker,
                    "start_time": next(iter(values)) if len(values) == 1 else pd.NaT,
                    "settlement_time": live.get("settlement_ts"),
                    "close_time": live.get("close_time"),
                    "status": live.get("status", getattr(local, "status", "")),
                    "start_source": "kalshi_milestone"
                    if len(values) == 1
                    else "missing_or_ambiguous",
                    "settlement_source": "kalshi_settlement_ts"
                    if live.get("settlement_ts")
                    else "missing",
                }
            )
        return normalize_timings(pd.DataFrame(rows), family)
    finally:
        if owns_client:
            client.close()


def extract_family_trades(
    data_dir: str | Path,
    *,
    family: str,
    output_path: Path,
    progress: Callable[[str], None] = print,
) -> pd.Timestamp:
    """Scan the archive once for an entire family rather than once per ticker."""
    family = normalize_family(family)
    files = _files(Path(data_dir) / "kalshi" / "trades_global_staging")
    if not files:
        raise ValueError("No local trade Parquet files found.")
    coverage_end = pd.NaT
    pending = output_path.with_suffix(".inprogress.parquet")
    writer = None
    try:
        with duckdb.connect() as con:
            con.execute("SET enable_progress_bar=false")
            con.execute("SET memory_limit='2GB'")
            for offset in range(0, len(files), FILE_BATCH_SIZE):
                batch = files[offset : offset + FILE_BATCH_SIZE]
                # Global archive coverage, not just the last print of this family.
                latest = con.execute(
                    "SELECT max(created_time) FROM read_parquet(?, union_by_name=true)",
                    [batch],
                ).fetchone()[0]
                if latest is not None:
                    timestamp = utc(latest)
                    if pd.isna(coverage_end) or timestamp > coverage_end:
                        coverage_end = timestamp
                frame = con.execute(
                    "SELECT trade_id, ticker, count, yes_price, no_price, taker_side, created_time "
                    "FROM read_parquet(?, union_by_name=true) WHERE starts_with(ticker, ?)",
                    [batch, family + "-"],
                ).fetchdf()
                if not frame.empty:
                    table = pa.Table.from_pandas(frame, preserve_index=False)
                    if writer is None:
                        writer = pq.ParquetWriter(
                            pending, table.schema, compression="zstd"
                        )
                    writer.write_table(table)
                if offset % (
                    FILE_BATCH_SIZE * 10
                ) == 0 or offset + FILE_BATCH_SIZE >= len(files):
                    progress(
                        f"Trade files scanned: {min(offset + FILE_BATCH_SIZE, len(files)):,}/{len(files):,}"
                    )
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise ValueError(f"No stored trades found for {family}.")
    pending.replace(output_path)
    return coverage_end


TRADE_COLUMNS = [
    "trade_id",
    "ticker",
    "count",
    "yes_price",
    "no_price",
    "taker_side",
    "created_time",
]


def normalize_trade_cache(
    trade_cache: Path,
    timings: pd.DataFrame,
    folder: Path,
    progress: Callable[[str], None] = print,
) -> Path:
    """Repair/deduplicate/sort out of core, then reuse the result across queries."""
    output = folder / "normalized_trades.parquet"
    signature_path = folder / "normalized_inputs.json"
    stat = trade_cache.stat()
    signature = {
        "version": 2,
        "trade_size": stat.st_size,
        "trade_mtime_ns": stat.st_mtime_ns,
        "timings_hash": hashlib.sha256(
            timings.to_json(date_format="iso").encode()
        ).hexdigest(),
    }
    if (
        output.exists()
        and signature_path.exists()
        and json.loads(signature_path.read_text()) == signature
    ):
        return output
    pending = output.with_suffix(".inprogress.parquet")
    ordered = folder / ".ordered_trades.parquet"
    sql_path = str(ordered).replace("'", "''")
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar=false")
        con.execute("SET memory_limit='2GB'")
        con.execute("SET threads=1")
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET temp_directory=?", [str(folder / ".duckdb_tmp")])
        con.register("timing_metadata", timings)
        con.execute(
            f"""
            COPY (
                SELECT r.* FROM read_parquet(?) r
                INNER JOIN timing_metadata m USING (ticker)
                ORDER BY r.ticker, r.created_time, r.trade_id
            ) TO '{sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 65536)
        """,
            [str(trade_cache)],
        )
    # Separating the external sort from per-ticker deduplication avoids two
    # simultaneous blocking operators competing for the same memory budget.
    writer = None
    metadata = timings.set_index("ticker", drop=False)
    try:
        for index, (ticker, raw) in enumerate(_ticker_frames(ordered), 1):
            normalized = add_normalized_time(
                normalize_trade_prices(raw),
                metadata.loc[[ticker]].reset_index(drop=True),
            )
            table = pa.Table.from_pandas(normalized, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(pending, table.schema, compression="zstd")
            writer.write_table(table)
            if index % 500 == 0:
                progress(f"Trade histories written: {index:,}/{len(timings):,}")
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise ValueError("No trades match the timing metadata.")
    pending.replace(output)
    ordered.unlink()
    signature_path.write_text(json.dumps(signature, indent=2))
    return output


def _ticker_frames(path: Path, batch_size: int = 100_000):
    """Yield complete ticker histories, carrying the last ticker across batches."""
    remainder = pd.DataFrame()
    for batch in pq.ParquetFile(path).iter_batches(
        batch_size=batch_size, columns=TRADE_COLUMNS
    ):
        frame = pd.concat([remainder, batch.to_pandas()], ignore_index=True)
        last_ticker = frame.ticker.iloc[-1]
        complete = frame[frame.ticker != last_ticker]
        for ticker, group in complete.groupby("ticker", sort=False):
            yield ticker, group
        remainder = frame[frame.ticker == last_ticker].copy()
    if not remainder.empty:
        yield str(remainder.ticker.iloc[0]), remainder


def build_cached_dataset(
    normalized_path: Path,
    timings: pd.DataFrame,
    *,
    family: str,
    coverage_end,
    time_step: float,
    max_clv_age_minutes,
    progress,
) -> FillDataset:
    metadata = timings.set_index("ticker", drop=False)
    snapshots, audits, seen = [], [], set()
    options = dict(
        family=family,
        coverage_end=coverage_end,
        time_step=time_step,
        max_clv_age_minutes=max_clv_age_minutes,
    )
    for ticker, trades in _ticker_frames(normalized_path):
        result = build_fill_dataset(
            trades, metadata.loc[[ticker]].reset_index(drop=True), **options
        )
        if not result.snapshots.empty:
            snapshots.append(result.snapshots)
        audits.append(result.audit)
        seen.add(ticker)
        if len(seen) % 500 == 0:
            progress(f"Tickers normalized: {len(seen):,}/{len(timings):,}")
    missing = build_fill_dataset(
        pd.DataFrame(columns=TRADE_COLUMNS),
        timings[~timings.ticker.isin(seen)],
        **options,
    )
    audits.append(missing.audit)
    return FillDataset(
        pd.concat(snapshots, ignore_index=True) if snapshots else missing.snapshots,
        pd.concat(audits, ignore_index=True),
    )


def prepare_fill_data(
    *,
    family: str = "KXATPCHALLENGERMATCH",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "output/fill_probability",
    timings_path: str | Path | None = None,
    time_step: float = 0.05,
    max_clv_age_minutes: float | None = None,
    refresh: bool = False,
    progress: Callable[[str], None] = print,
) -> Path:
    """Prepare every stored ticker in the family; cache expensive inputs explicitly.

    Repeated builds reuse cached trade and API snapshots. --refresh performs
    a fresh archive scan and metadata fetch after collecting new data.
    """
    family = normalize_family(family)
    folder = Path(output_dir) / family
    folder.mkdir(parents=True, exist_ok=True)
    markets_path = folder / "markets.parquet"
    if markets_path.exists() and not refresh:
        markets = pd.read_parquet(markets_path)
    else:
        progress(f"Reading local market metadata for {family}...")
        store = LocalGameStore(data_dir)
        markets = store._query(
            store.market_files, "*", "starts_with(event_ticker, ?)", (family + "-",)
        )
        if markets.empty:
            raise ValueError(f"No local markets for {family}.")
        markets = markets.drop_duplicates("ticker", keep="last")
        markets.to_parquet(markets_path, index=False)

    timing_cache = folder / "timings.parquet"
    if timings_path is not None:
        path = Path(timings_path)
        timings = (
            pd.read_parquet(path)
            if path.suffix.lower() == ".parquet"
            else pd.read_csv(path)
        )
        timings = normalize_timings(timings, family)
        # Preserve every catalog ticker in the audit even if its timing is absent.
        timings = markets[["ticker", "event_ticker"]].merge(
            timings.drop(columns="event_ticker"),
            how="left",
            on="ticker",
            validate="one_to_one",
        )
        timings = normalize_timings(timings, family)
        timings.to_parquet(timing_cache, index=False)
    elif timing_cache.exists() and not refresh:
        timings = pd.read_parquet(timing_cache)
    else:
        timings = fetch_family_timings(markets, family=family, progress=progress)
        timings.to_parquet(timing_cache, index=False)

    trade_cache, coverage_path = (
        folder / "trades.parquet",
        folder / "trade_coverage.json",
    )
    if trade_cache.exists() and coverage_path.exists() and not refresh:
        progress(
            "Using cached family trades. Run with --refresh after updating the archive."
        )
        coverage = json.loads(coverage_path.read_text())
        if coverage["data_dir"] != str(Path(data_dir).resolve()):
            raise ValueError(
                "Cached trades came from a different data directory; use --refresh."
            )
        coverage_end = utc(coverage["coverage_end"])
    else:
        coverage_end = extract_family_trades(
            data_dir, family=family, output_path=trade_cache, progress=progress
        )
        coverage_path.write_text(
            json.dumps(
                {
                    "coverage_end": coverage_end.isoformat(),
                    "data_dir": str(Path(data_dir).resolve()),
                },
                indent=2,
            )
        )
    n_prints = pq.ParquetFile(trade_cache).metadata.num_rows
    progress(
        f"Normalizing {n_prints:,} prints across {len(timings):,} tickers (bounded memory)..."
    )
    normalized_path = normalize_trade_cache(
        trade_cache, timings, folder, progress=progress
    )
    result = build_cached_dataset(
        normalized_path,
        timings,
        family=family,
        coverage_end=coverage_end,
        time_step=time_step,
        max_clv_age_minutes=max_clv_age_minutes,
        progress=progress,
    )
    result.snapshots.to_parquet(folder / "snapshots.parquet", index=False)
    result.audit.to_csv(folder / "ticker_audit.csv", index=False)
    summary = result.audit.merge(
        timings[["ticker", "start_time", "close_time", "settlement_time"]],
        on="ticker",
        validate="one_to_one",
    ).merge(
        result.snapshots[
            ["ticker", "clv", "clv_time", "clv_age_seconds"]
        ].drop_duplicates("ticker"),
        on="ticker",
        how="left",
        validate="one_to_one",
    )
    summary.to_csv(folder / "ticker_summary.csv", index=False)
    manifest = {
        "family": family,
        "time_step": time_step,
        "max_clv_age_minutes": max_clv_age_minutes,
        "coverage_end": coverage_end.isoformat(),
        "n_tickers": len(timings),
        "n_events": int(timings.event_ticker.nunique()),
        "n_snapshots": len(result.snapshots),
        "n_cached_prints": n_prints,
        "audit_counts": result.audit.status.value_counts().to_dict(),
        "clv_definition": "last_trade_strictly_before_scheduled_start",
        "normalized_time": "(timestamp-start_time)/(settlement_time-start_time)",
        "optimistic": "future_min_price <= bid_price",
        "conservative": "future_min_price < bid_price",
        "fill_probability_gap": "p_optimistic - p_conservative",
        "future_horizon": "strictly after observation, strictly before close_time",
        "limitations": [
            "Retrospective settlement clock",
            "No queue position or order-size simulation",
            "Archive completeness assumed within coverage",
            "Legacy archive has no block-trade flag",
        ],
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    progress(json.dumps(manifest["audit_counts"]))
    progress(f"Saved {len(result.snapshots):,} states to {folder}")
    return folder
