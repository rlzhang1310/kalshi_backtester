"""Resumable, copy-only trade archive migration.

The implementation deliberately does not modify the collector or current readers.
Each source unit and each compacted family has a separate filesystem commit and
SQLite commit. Resume reconciles the gap between those commits.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


VERSION = 1
FINGERPRINT_VERSION = "sha256-schema-typed-json-sum128-sumsq128-v2"
MOD = 1 << 128
FAMILY_RE = re.compile(r"^[A-Z0-9]{1,64}$")
RESERVED_FOLDERS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
DEFAULT_SOURCE = Path("data/kalshi/trades_global_staging")
DEFAULT_DESTINATION = Path("data/kalshi/trades_by_series")
DEFAULT_MARKETS = Path("data/kalshi/markets")


def _fault(stage):
    """Test seam for crash-boundary recovery; production does nothing."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _owned(path: Path, root: Path):
    path.resolve().relative_to(root.resolve())
    if path.resolve() == root.resolve():
        raise ValueError("Refusing to remove the work root itself")
    return path


def _remove(path: Path, root: Path):
    _owned(path, root)
    if path.is_dir():
        _windows_file_retry(lambda: shutil.rmtree(path), f"remove {path}")
    elif path.exists():
        _windows_file_retry(path.unlink, f"remove {path}")


def _windows_file_retry(operation, description):
    """Let short-lived Windows readers/scanners release migration-owned paths."""
    for attempt in range(9):
        try:
            return operation()
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or attempt == 8:
                raise
            gc.collect()
            time.sleep(min(0.25 * (2 ** attempt), 8))
    raise RuntimeError(f"Could not {description}")


def _rename_directory(source: Path, destination: Path):
    _windows_file_retry(lambda: os.replace(source, destination), f"rename {source} to {destination}")


def _atomic_json(path: Path, value):
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _lock(work_root: Path):
    work_root.mkdir(parents=True, exist_ok=True)
    handle = (work_root / "writer.lock").open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise RuntimeError("Another migration writer holds this run's lock") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _schema_json(schema: pa.Schema):
    return [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]


def _files(root: Path):
    return sorted(p for p in root.glob("*.parquet") if not p.name.startswith("._"))


def _metadata_families(markets: Path):
    """Trust a prefix only if every local market relationship for it agrees."""
    files = _files(markets)
    if not files:
        raise ValueError(f"No market metadata Parquet files in {markets}")
    good, bad = set(), set()
    with duckdb.connect() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=1")
        for start in range(0, len(files), 128):
            group = [str(p) for p in files[start : start + 128]]
            fields = pq.ParquetFile(group[0]).schema_arrow.names
            if "ticker" not in fields or "event_ticker" not in fields:
                raise ValueError("Market metadata needs ticker and event_ticker")
            has_series = any("series_ticker" in pq.ParquetFile(path).schema_arrow.names for path in group)
            explicit = ", series_ticker" if has_series else ""
            series_bad = " OR (series_ticker IS NOT NULL AND series_ticker <> family)" if has_series else ""
            rows = con.execute(
                "WITH markets AS (SELECT ticker, event_ticker" + explicit + ", split_part(ticker,'-',1) AS family "
                "FROM read_parquet(?, union_by_name=true)) "
                "SELECT family, count(*) FILTER (WHERE NOT starts_with(ticker, event_ticker || '-') "
                "OR (event_ticker <> family AND NOT starts_with(event_ticker, family || '-')) "
                "OR event_ticker IS NULL" + series_bad + ") AS bad_rows "
                "FROM markets WHERE ticker IS NOT NULL GROUP BY family",
                [group],
            ).fetchall()
            for family, bad_rows in rows:
                if not FAMILY_RE.fullmatch(family) or family in RESERVED_FOLDERS or family == "UNRESOLVED" or bad_rows:
                    bad.add(family)
                else:
                    good.add(family)
            if (start // 128 + 1) % 8 == 0 or start + 128 >= len(files):
                print(f"Preflight markets: {min(start + 128, len(files)):,}/{len(files):,}", flush=True)
    return sorted(good - bad), sorted(bad)


def _snapshot(source: Path):
    files = _files(source)
    if not files:
        raise ValueError(f"No trade Parquet files in {source}")
    inventory, schemas = [], {}
    def inspect(path):
        stat = path.stat()
        parquet = pq.ParquetFile(path)  # Footer failure rejects incomplete files.
        schema = parquet.schema_arrow.remove_metadata()
        key = _json(_schema_json(schema))
        return path.name, stat.st_size, stat.st_mtime_ns, parquet.metadata.num_rows, key, schema
    with ThreadPoolExecutor(max_workers=12) as pool:
        inspected = pool.map(inspect, files)
        for index, (name, size, mtime, rows, key, schema) in enumerate(inspected, 1):
            schemas[key] = schema
            inventory.append((name, size, mtime, rows, key))
            if index % 10000 == 0:
                print(f"Preflight footers: {index:,}/{len(files):,}", flush=True)
    try:
        common = pa.unify_schemas(list(schemas.values()), promote_options="permissive")
    except pa.ArrowException as exc:
        raise ValueError(f"Incompatible source schemas: {exc}") from exc
    common = pa.schema([
        pa.field(field.name, field.type, nullable=field.nullable or any(field.name not in variant.names for variant in schemas.values()))
        for field in common
    ])
    for required in ("ticker", "created_time"):
        if required not in common.names:
            raise ValueError(f"Trade schema lacks {required}")
    if not pa.types.is_string(common.field("ticker").type):
        raise ValueError("ticker must be a string")
    if not pa.types.is_timestamp(common.field("created_time").type):
        raise ValueError("created_time must be a timestamp")
    return inventory, common, schemas


def _sample_keys(source: Path, inventory, common):
    picks = [inventory[0], inventory[len(inventory) // 2], inventory[-1]]
    refresh = next((x for x in inventory if x[0].startswith("refresh_")), None)
    if refresh:
        picks.append(refresh)
    findings = []
    for name, *_ in dict.fromkeys(picks):
        path = source / name
        available = pq.ParquetFile(path).schema_arrow.names
        fields = [key for key in ("ticker", "trade_id", "created_time") if key in available]
        table = pq.read_table(path, columns=fields)
        findings.append({
            "file": name,
            "rows_measured": table.num_rows,
            "fields": {key: {"type": str(table[key].type), "nulls": table[key].null_count} for key in fields},
            "missing_keys": [key for key in ("ticker", "trade_id", "created_time") if key not in available],
        })
    return findings


def _separate_paths(source: Path, destination: Path, markets: Path):
    source, destination, markets = (x.resolve() for x in (source, destination, markets))
    for a, b in ((source, destination), (destination, source), (markets, destination)):
        if a == b or a in b.parents:
            raise ValueError("Source, market metadata, and destination must not overlap")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    return source, destination, markets


def _space(path: Path):
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def plan(source: Path, destination: Path, markets: Path, reserve_gib=25, spill=None,
         memory_mib=1024, workers=1, target_mib=256, files_per_unit=64):
    source, destination, markets = _separate_paths(source, destination, markets)
    inventory, common, variants = _snapshot(source)
    families, conflicts = _metadata_families(markets)
    sampled_prefixes = set()
    for name, *_ in inventory[:files_per_unit]:
        sampled_prefixes.update(
            value.split("-", 1)[0] if isinstance(value, str) else "UNRESOLVED"
            for value in pq.read_table(source / name, columns=["ticker"])["ticker"].to_pylist()
        )
    compressed = sum(item[1] for item in inventory)
    # Conservative planning envelope, not a guarantee of actual compression.
    estimated_final = compressed
    estimated_peak = int(compressed * 2.5) + reserve_gib * (1 << 30)
    spill = (spill or destination.parent / ".trades_by_series_work" / "spill").resolve()
    same_volume = spill.anchor.lower() == destination.anchor.lower()
    destination_required = estimated_peak if same_volume else int(compressed * 2.2) + reserve_gib * (1 << 30)
    spill_required = estimated_peak if same_volume else int(compressed * 0.3) + reserve_gib * (1 << 30)
    result = {
        "source_files": len(inventory),
        "source_rows": sum(item[3] for item in inventory),
        "source_bytes": compressed,
        "largest_source_file_rows": max(item[3] for item in inventory),
        "schema_variants": [_schema_json(s) for s in variants.values()],
        "common_schema": _schema_json(common),
        "representative_key_nulls": _sample_keys(source, inventory, common),
        "validated_families": len(families),
        "conflicting_family_prefixes": conflicts[:20],
        "mapping_ready": bool(families),
        "mapping_rule": "market-event-prefix-agreement-v1; unknown/conflict -> UNRESOLVED",
        "destination_free_bytes": _space(destination.parent),
        "spill_free_bytes": _space(spill.parent),
        "estimated_final_bytes": estimated_final,
        "estimated_peak_additional_bytes_including_reserve": estimated_peak,
        "estimated_destination_required_bytes": destination_required,
        "estimated_spill_required_bytes": spill_required,
        "estimate_note": "Assumes final and fragments each near original compressed size, plus 0.5x source for current family/spill; compression and other processes can change peak use.",
        "reserve_gib": reserve_gib,
        "target_file_mib": target_mib,
        "workers": workers,
        "memory_mib": memory_mib,
        "files_per_unit": files_per_unit,
        "sampled_first_unit_families": len(sampled_prefixes),
        "estimated_intermediate_fragments": ((len(inventory) + files_per_unit - 1) // files_per_unit) * len(sampled_prefixes),
        "fragment_estimate_note": "Extrapolated from the first transaction unit; actual family mix varies, and fragments are temporary.",
    }
    return result, inventory, common, families


def _db(path: Path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, rows INTEGER, schema_key TEXT, digest TEXT);"
        "CREATE TABLE IF NOT EXISTS units (id INTEGER PRIMARY KEY, first_file INTEGER, last_file INTEGER, state TEXT, rows INTEGER, fingerprint TEXT, files_json TEXT);"
        "CREATE TABLE IF NOT EXISTS fragments (unit_id INTEGER, family TEXT, relative_path TEXT, PRIMARY KEY(unit_id,family));"
        "CREATE INDEX IF NOT EXISTS fragments_family ON fragments(family,unit_id);"
        "CREATE TABLE IF NOT EXISTS families (family TEXT PRIMARY KEY, state TEXT, rows INTEGER, fingerprint TEXT, files_json TEXT, baseline_json TEXT);"
        "CREATE TABLE IF NOT EXISTS tickers (ticker_key TEXT, family TEXT, rows INTEGER, PRIMARY KEY(family,ticker_key));"
    )
    return con


def _config(con):
    return {key: json.loads(value) for key, value in con.execute("SELECT key,value FROM config")}


def _set(con, key, value):
    con.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, _json(value)))


def _canonical(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, bytes):
        return ["binary", value.hex()]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["list", [_canonical(v) for v in value]]
    if isinstance(value, dict):
        return ["struct", [[k, _canonical(v)] for k, v in sorted(value.items())]]
    # Datetimes are encoded from Arrow's physical integer before this function.
    raise ValueError(f"Unsupported fingerprint value type: {type(value).__name__}")


def _physical(table: pa.Table, schema: pa.Schema):
    """Keep every timestamp's exact nanosecond/tz meaning outside DuckDB."""
    arrays, fields = [], []
    for field in schema:
        column = table[field.name]
        if pa.types.is_timestamp(field.type):
            arrays.append(column.cast(pa.int64()))
            fields.append(pa.field(field.name, pa.int64(), nullable=field.nullable))
        else:
            arrays.append(column)
            fields.append(field)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _restore(table: pa.Table, schema: pa.Schema):
    arrays = []
    for field in schema:
        column = table[field.name]
        arrays.append(column.cast(field.type, safe=True))
    return pa.Table.from_arrays(arrays, schema=schema)


def _row_payload(row, schema):
    return _json([[field.name, str(field.type), _canonical(row[field.name])] for field in schema]).encode("utf-8")


def _fp(table: pa.Table, schema: pa.Schema):
    """Order-independent typed row fingerprint; duplicate rows add twice."""
    total, square = 0, 0
    for row in table.to_pylist():
        payload = _row_payload(row, schema)
        digest = int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")
        total = (total + digest) % MOD
        square = (square + digest * digest) % MOD
    return [table.num_rows, total, square]


def _combine(items):
    return [sum(x[0] for x in items), sum(x[1] for x in items) % MOD, sum(x[2] for x in items) % MOD]


def _family(ticker, valid, original_series=None):
    if not isinstance(ticker, str):
        return "UNRESOLVED"
    prefix = ticker.split("-", 1)[0]
    if original_series is not None and original_series != prefix:
        return "UNRESOLVED"
    return prefix if prefix in valid and ticker.startswith(prefix + "-") else "UNRESOLVED"


def _key_summary(table):
    times = table["created_time"].to_pylist()
    present = [t for t in times if t is not None]
    return {
        "timestamp_min_physical": min(present) if present else None,
        "timestamp_max_physical": max(present) if present else None,
        "nulls": {key: table[key].null_count for key in ("ticker", "created_time", "trade_id") if key in table.column_names},
    }


def _combine_summary(old, new):
    result = {}
    for key, op in (("timestamp_min_physical", min), ("timestamp_max_physical", max)):
        values = [value for value in (old.get(key), new.get(key)) if value is not None]
        result[key] = op(values) if values else None
    keys = set(old.get("nulls", {})) | set(new.get("nulls", {}))
    result["nulls"] = {key: old.get("nulls", {}).get(key, 0) + new.get("nulls", {}).get(key, 0) for key in keys}
    return result


def _source_unchanged(con, source):
    for name, size, mtime, digest in con.execute("SELECT name,size,mtime_ns,digest FROM sources"):
        path = source / name
        if not path.is_file():
            raise RuntimeError(f"Captured source missing: {path}")
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
            raise RuntimeError(f"Captured source changed: {path}; start a new run")
        if digest and _file_digest(path) != digest:
            raise RuntimeError(f"Captured source content changed: {path}; start a new run")


def _file_digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _check_space(config, extra=0):
    reserve = config["min_free_gib"] * (1 << 30)
    for label in ("destination", "spill"):
        root = Path(config[label]).parent
        free = _space(root)
        if free - extra < reserve:
            raise RuntimeError(f"Low space on {root}: {free:,} free bytes; reserve is {reserve:,}")


def _read_file(path, schema):
    table = pq.read_table(path).replace_schema_metadata(None)
    arrays = []
    for field in schema:
        if field.name in table.column_names:
            original = table[field.name]
            promoted = original.cast(field.type, safe=True)
            if not promoted.cast(original.type, safe=True).equals(original):
                raise ValueError(f"Lossy schema conversion in {path}: {field.name}")
            arrays.append(promoted)
        else:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def _unit(con, config, schema, valid, work, unit_id, names):
    pending = work / "units" / f"u{unit_id:06d}.pending"
    committed = work / "units" / f"u{unit_id:06d}"
    if pending.exists():
        _remove(pending, work)
    if committed.exists():
        _remove(committed, work)  # Filesystem commit without SQLite commit.
    pending.mkdir(parents=True)
    source = Path(config["source"])
    source_stats = {}
    tables = []
    for name in names:
        path = source / name
        expected = con.execute("SELECT size,mtime_ns FROM sources WHERE name=?", (name,)).fetchone()
        before = path.stat()
        if (before.st_size, before.st_mtime_ns) != expected:
            raise RuntimeError(f"Captured source changed: {path}; start a new run")
        source_stats[name] = _file_digest(path)
        tables.append(_physical(_read_file(path, schema), schema))
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != expected:
            raise RuntimeError(f"Captured source changed during read: {path}; stop collection")
    table = pa.concat_tables(tables) if tables else _physical(pa.Table.from_arrays([pa.array([], type=f.type) for f in schema], schema=schema), schema)
    by_family = defaultdict(list)
    mapping_methods = Counter()
    tickers = table["ticker"].to_pylist()
    original_series = table["series_ticker"].to_pylist() if "series_ticker" in table.column_names else [None] * len(tickers)
    for i, (ticker, series) in enumerate(zip(tickers, original_series)):
        family = _family(ticker, valid, series)
        by_family[family].append(i)
        if family == "UNRESOLVED":
            method = "unresolved"
        elif series is not None:
            method = "explicit_source_series_confirmed"
        else:
            method = "metadata_validated_prefix_rule"
        mapping_methods[method] += 1
    parts = {}
    family_fp = {}
    family_summary = {}
    ticker_counts = Counter()
    for family, indices in by_family.items():
        selected = table.take(pa.array(indices, type=pa.int64()))
        subdir = pending / family
        subdir.mkdir()
        part = subdir / "part-000000.parquet"
        pq.write_table(selected, part, compression="zstd", row_group_size=100_000)
        with part.open("rb") as handle:
            written = pq.read_table(handle)
        if _fp(written, schema) != _fp(selected, schema):
            raise RuntimeError(f"Unit {unit_id}: fragment verification failed for {family}")
        del written
        parts[family] = f"units/u{unit_id:06d}/{family}/part-000000.parquet"
        family_fp[family] = _fp(selected, schema)
        family_summary[family] = _key_summary(selected)
        ticker_counts.update((family, "N:" if ticker is None else "S:" + ticker) for ticker in selected["ticker"].to_pylist())
    expected = _combine(family_fp.values())
    if expected[0] != sum(pq.ParquetFile(source / n).metadata.num_rows for n in names):
        raise RuntimeError(f"Unit {unit_id}: source row count mismatch")
    _check_space(config)
    _fault("unit_before_rename")
    _rename_directory(pending, committed)
    _fault("unit_after_rename")
    with con:
        con.execute("UPDATE units SET state='done',rows=?,fingerprint=?,files_json=? WHERE id=?", (expected[0], _json(expected), _json({"parts": parts, "family_fp": family_fp, "mapping_methods": dict(mapping_methods)}), unit_id))
        con.executemany("INSERT INTO fragments VALUES (?,?,?)", [(unit_id, family, relative) for family, relative in parts.items()])
        for name, digest in source_stats.items():
            con.execute("UPDATE sources SET digest=? WHERE name=?", (digest, name))
        for family, fp in family_fp.items():
            con.execute("INSERT OR IGNORE INTO families VALUES (?,'pending',0,?, '[]', ?)", (family, _json([0, 0, 0]), _json({})))
            old_fp, old_summary = con.execute("SELECT fingerprint,baseline_json FROM families WHERE family=?", (family,)).fetchone()
            old = json.loads(old_fp)
            combined = _combine([old, fp])
            baseline = _combine_summary(json.loads(old_summary), family_summary[family])
            con.execute("UPDATE families SET rows=?,fingerprint=?,baseline_json=? WHERE family=?", (combined[0], _json(combined), _json(baseline), family))
        con.executemany(
            "INSERT INTO tickers VALUES (?,?,?) ON CONFLICT(family,ticker_key) DO UPDATE SET rows=rows+excluded.rows",
            [(ticker_key, family, count) for (family, ticker_key), count in ticker_counts.items()],
        )
    return expected[0], len(by_family)


def _fragments(con, family, work):
    return [work / relative for (relative,) in con.execute(
        "SELECT relative_path FROM fragments WHERE family=? ORDER BY unit_id", (family,)
    )]


def _sort_key(row, columns):
    return tuple((row[name] is None, row[name]) for name in columns)


def _validate_family(path, family, schema, expected, ticker_expected):
    files = sorted(path.glob("part-*.parquet"))
    actual = []
    counts = Counter()
    keys = [name for name in ("ticker", "created_time", "trade_id") if name in schema.names]
    timestamp_min = timestamp_max = None
    nulls = Counter()
    for file in files:
        parquet = pq.ParquetFile(file)
        if parquet.schema_arrow.remove_metadata() != schema:
            raise RuntimeError(f"Output schema mismatch: {file}")
        previous = None
        for batch in parquet.iter_batches(batch_size=65536):
            table = pa.Table.from_batches([batch])
            physical = _physical(table, schema)
            actual.append(_fp(physical, schema))
            check_fields = keys + (["series_ticker"] if "series_ticker" in physical.column_names else [])
            for row in physical.select(check_fields).to_pylist():
                key = _sort_key(row, keys)
                if previous is not None and key < previous:
                    raise RuntimeError(f"Sort order failed in {file}")
                previous = key
                ticker = row["ticker"]
                if _family(ticker, ticker_expected["valid"], row.get("series_ticker")) != family:
                    raise RuntimeError(f"Misrouted row in {file}: {ticker}")
                counts["N:" if ticker is None else "S:" + ticker] += 1
                ts = row["created_time"]
                if ts is None:
                    nulls["created_time"] += 1
                else:
                    timestamp_min = ts if timestamp_min is None else min(timestamp_min, ts)
                    timestamp_max = ts if timestamp_max is None else max(timestamp_max, ts)
                if ticker is None:
                    nulls["ticker"] += 1
                if "trade_id" in row and row["trade_id"] is None:
                    nulls["trade_id"] += 1
    if _combine(actual) != expected:
        raise RuntimeError(f"Family {family}: row fingerprint mismatch")
    for key, count in counts.items():
        if ticker_expected["counts"].get(key) != count:
            raise RuntimeError(f"Family {family}: ticker count mismatch for {key}")
    if len(counts) != len(ticker_expected["counts"]):
        raise RuntimeError(f"Family {family}: missing ticker")
    return {"rows": expected[0], "files": len(files), "bytes": sum(p.stat().st_size for p in files), "markets": len(counts) - int("N:" in counts), "timestamp_min_physical": timestamp_min, "timestamp_max_physical": timestamp_max, "nulls": {key: nulls[key] for key in keys}, "fingerprint": expected}


def _exact_sample(fragments, output, schema, ticker_keys):
    """Compare a few complete ticker row multisets before fragments are removed."""
    tickers = [key[2:] for key in ticker_keys if key.startswith("S:")][:3]
    if not tickers:
        return []
    wanted = pa.array(tickers, type=pa.string())
    def rows(paths):
        found = Counter()
        for path in paths:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=65536):
                table = pa.Table.from_batches([batch])
                matched = table.filter(pc.is_in(table["ticker"], value_set=wanted))
                if matched.num_rows:
                    for row in _physical(matched, schema).to_pylist():
                        payload = _row_payload(row, schema)
                        found[payload] += 1
        return found
    if rows(fragments) != rows(sorted(output.glob("part-*.parquet"))):
        raise RuntimeError(f"Exact sample multiset mismatch: {tickers}")
    return tickers


def _compact(con, config, schema, valid, work, family):
    fragments = _fragments(con, family, work)
    if not fragments:
        raise RuntimeError(f"No committed fragments for {family}")
    partition_key = config["partition_key"]
    pending = work / "candidate" / f"{partition_key}={family}.pending"
    output = work / "candidate" / f"{partition_key}={family}"
    if pending.exists():
        _remove(pending, work)
    if output.exists():
        _remove(output, work)  # Not committed in SQLite.
    pending.mkdir(parents=True)
    expected = json.loads(con.execute("SELECT fingerprint FROM families WHERE family=?", (family,)).fetchone()[0])
    ticker_expected = {k: n for k, n in con.execute("SELECT ticker_key,rows FROM tickers WHERE family=?", (family,))}
    keys = [name for name in ("ticker", "created_time", "trade_id") if name in schema.names]
    sql_keys = ", ".join('"' + key.replace('"', '""') + '" NULLS LAST' for key in keys)
    target = config["target_mib"] * (1 << 20)
    with duckdb.connect() as duck:
        duck.execute(f"SET memory_limit='{config['memory_mib']}MB'")
        duck.execute(f"SET threads={config['workers']}")
        spill = Path(config["spill"])
        spill.mkdir(exist_ok=True)
        duck.execute("SET temp_directory=?", [str(spill)])
        stop = threading.Event()
        low_space = []
        def monitor():
            while not stop.wait(5):
                try:
                    _check_space(config)
                except RuntimeError as exc:
                    low_space.append(exc)
                    duck.interrupt()
                    return
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        writer = None
        try:
            reader = duck.execute(f"SELECT * FROM read_parquet(?, union_by_name=true) ORDER BY {sql_keys}", [[str(p) for p in fragments]]).fetch_record_batch(rows_per_batch=65536)
            count = 0
            index = 0
            for batch in reader:
                _check_space(config)
                table = _restore(pa.Table.from_batches([batch]), schema)
                if writer is None:
                    index += 1
                    writer = pq.ParquetWriter(pending / f"part-{index:06d}.parquet", schema, compression="zstd", write_statistics=True)
                    count = 0
                writer.write_table(table, row_group_size=100_000)
                count += table.nbytes
                if count >= target * 2:
                    writer.close()
                    writer = None
        except duckdb.Error:
            if low_space:
                raise low_space[0]
            raise
        finally:
            stop.set()
            watcher.join()
            if writer is not None:
                writer.close()
    if low_space:
        raise low_space[0]
    report = _validate_family(pending, family, schema, expected, {"valid": valid, "counts": ticker_expected})
    report["exact_sample_tickers"] = _exact_sample(fragments, pending, schema, sorted(ticker_expected, key=lambda key: -ticker_expected[key]))
    baseline = json.loads(con.execute("SELECT baseline_json FROM families WHERE family=?", (family,)).fetchone()[0])
    if any(report[key] != baseline[key] for key in ("timestamp_min_physical", "timestamp_max_physical", "nulls")):
        raise RuntimeError(f"Family {family}: timestamp range or key null counts changed")
    _fault("family_before_rename")
    _rename_directory(pending, output)
    _fault("family_after_rename")
    with con:
        con.execute("UPDATE families SET state='done',files_json=? WHERE family=?", (_json(report), family))
    _fault("family_after_commit")
    for fragment in fragments:
        _remove(fragment, work)
    return report


def _publish(con, config, schema, work):
    candidate = work / "candidate"
    destination = Path(config["destination"])
    if destination.exists():
        existing = destination / "_metadata" / "dataset.json"
        if not existing.exists() or json.loads(existing.read_text()).get("run_id") != config["run_id"]:
            raise RuntimeError("An unrelated destination exists; refusing to overwrite")
        verify_output(con, config, schema)
        with con:
            _set(con, "state", "published")
        return json.loads((destination / "_metadata" / "validation.json").read_text())
    for name, digest in config.get("checkpoint_digests", {}).items():
        path = Path(config["source"]) / name
        if not path.is_file() or _file_digest(path) != digest:
            raise RuntimeError(f"Collector checkpoint changed during migration: {path}")
    families = [(f, json.loads(info)) for f, info in con.execute("SELECT family,files_json FROM families WHERE state='done' ORDER BY family")]
    if len(families) != con.execute("SELECT count(*) FROM families").fetchone()[0]:
        raise RuntimeError("Not all families are compacted")
    total_source = con.execute("SELECT sum(rows) FROM sources").fetchone()[0]
    total_output = sum(info["rows"] for _, info in families)
    if total_source != total_output:
        raise RuntimeError("Dataset row count mismatch")
    catalog = pa.table({
        "family": [f for f, _ in families],
        "relative_directory": [f"{config['partition_key']}={f}" for f, _ in families],
        "row_count": [v["rows"] for _, v in families],
        "distinct_market_count": [v["markets"] for _, v in families],
        "timestamp_min_physical": [v["timestamp_min_physical"] for _, v in families],
        "timestamp_max_physical": [v["timestamp_max_physical"] for _, v in families],
        "file_count": [v["files"] for _, v in families],
        "compressed_bytes": [v["bytes"] for _, v in families],
    })
    metadata = candidate / "_metadata"
    metadata.mkdir(exist_ok=True)
    pq.write_table(catalog, metadata / "family_catalog.parquet", compression="zstd")
    source_inventory = [{"name": n, "bytes": s, "mtime_ns": m, "rows": r, "sha256": d} for n, s, m, r, d in con.execute("SELECT name,size,mtime_ns,rows,digest FROM sources ORDER BY name")]
    _atomic_json(work / "source_inventory.json", source_inventory)
    _atomic_json(metadata / "source_inventory.json", source_inventory)
    validation = {
        "algorithm": FINGERPRINT_VERSION,
        "source_rows": total_source,
        "output_rows": total_output,
        "family_fingerprints": {f: v["fingerprint"] for f, v in families},
        "family_nulls_and_ranges": {f: {k: v[k] for k in ("nulls", "timestamp_min_physical", "timestamp_max_physical")} for f, v in families},
        "checks": ["source-unit-fragment", "family-count", "typed-row-fingerprint", "ticker-count", "partition", "per-file-sort", "parquet-readability"],
        "limitations": ["Hash-based equality is probabilistic", "Exact source-to-output multiset audits are available in fixture tests"],
        "unresolved_rows": dict(families).get("UNRESOLVED", {}).get("rows", 0),
        "unresolved_examples": [
            {"ticker": None if key == "N:" else key[2:], "rows": rows}
            for key, rows in con.execute(
                "SELECT ticker_key,rows FROM tickers WHERE family='UNRESOLVED' ORDER BY rows DESC LIMIT 20"
            )
        ],
    }
    methods = Counter()
    for (payload,) in con.execute("SELECT files_json FROM units WHERE state='done'"):
        methods.update(json.loads(payload)["mapping_methods"])
    validation["mapping_methods"] = dict(methods)
    _atomic_json(metadata / "validation.json", validation)
    _atomic_json(metadata / "dataset.json", {
        "format_version": VERSION,
        "run_id": config["run_id"],
        "snapshot_time": config["snapshot_time"],
        "source": config["source"],
        "source_inventory": "source_inventory.json",
        "schema": _schema_json(schema),
        "partition_key": config["partition_key"],
        "partition_rule": "validated local market/event prefix, uppercase; otherwise UNRESOLVED",
        "mapping_version": config["mapping_version"],
        "mapping_methods": dict(methods),
        "sort": "ticker, created_time, trade_id (when present), ascending NULLS LAST within each file; readers must ORDER BY",
        "source_rows": total_source,
        "output_rows": total_output,
        "validation_status": "passed",
        "unresolved_rows": validation["unresolved_rows"],
    })
    _rename_directory(candidate, destination)
    _fault("publish_after_rename")
    with con:
        _set(con, "state", "published")
    return validation


def verify_output(con, config, schema):
    """Reread every published file and compare it with retained baselines."""
    destination = Path(config["destination"])
    metadata = destination / "_metadata"
    dataset = json.loads((metadata / "dataset.json").read_text())
    validation = json.loads((metadata / "validation.json").read_text())
    if dataset.get("run_id") != config["run_id"] or dataset.get("validation_status") != "passed":
        raise RuntimeError("Published metadata does not match this validated run")
    if dataset.get("partition_key") != config["partition_key"]:
        raise RuntimeError("Published partition key mismatch")
    source_rows = con.execute("SELECT sum(rows) FROM sources").fetchone()[0]
    output_rows = 0
    valid = set(config["valid_families"])
    expected_families = set()
    for family, fingerprint, report_json in con.execute("SELECT family,fingerprint,files_json FROM families"):
        expected_families.add(family)
        path = destination / f"{config['partition_key']}={family}"
        if not path.is_dir():
            raise RuntimeError(f"Missing published family {family}")
        tickers = {k: n for k, n in con.execute("SELECT ticker_key,rows FROM tickers WHERE family=?", (family,))}
        report = _validate_family(path, family, schema, json.loads(fingerprint), {"valid": valid, "counts": tickers})
        stored = json.loads(report_json)
        if report != {key: value for key, value in stored.items() if key != "exact_sample_tickers"}:
            raise RuntimeError(f"Published family report changed for {family}")
        output_rows += report["rows"]
    found = {p.name.split("=", 1)[1] for p in destination.glob(f"{config['partition_key']}=*") if p.is_dir()}
    if found != expected_families or output_rows != source_rows or output_rows != validation["output_rows"]:
        raise RuntimeError("Published family set or dataset row total mismatch")
    if pq.ParquetFile(metadata / "family_catalog.parquet").metadata.num_rows != len(expected_families):
        raise RuntimeError("Family catalog row count mismatch")
    _source_unchanged(con, Path(config["source"]))
    return validation


def _run(config, work):
    with _lock(work):
        with _db(work / "manifest.sqlite") as con:
            state = _config(con)
            if (config.get("format_version") != VERSION or
                    config.get("fingerprint_version") != FINGERPRINT_VERSION or
                    config.get("duckdb_version") != duckdb.__version__ or
                    config.get("pyarrow_version") != pa.__version__):
                raise RuntimeError("Run format or dependency version changed; resume with the original tool environment")
            if state.get("state") == "published":
                print("Already complete:", config["destination"])
                return
            source = Path(config["source"])
            _source_unchanged(con, source)
            # Arrow IPC retains precise timestamp units, timezones, and decimals.
            schema = pa.ipc.read_schema(pa.BufferReader(bytes.fromhex(config["schema_ipc"])))
            valid = set(config["valid_families"])
            began = time.monotonic()
            units = con.execute("SELECT id,first_file,last_file,state FROM units ORDER BY id").fetchall()
            names = [row[0] for row in con.execute("SELECT name FROM sources ORDER BY name")]
            for unit_id, first, last, state in units:
                if state == "done":
                    continue
                _check_space(config, extra=sum((source / n).stat().st_size for n in names[first:last]))
                rows, family_count = _unit(con, config, schema, valid, work, unit_id, names[first:last])
                print(f"Distribute {unit_id}/{len(units)}: {rows:,} rows, {family_count} families; elapsed {time.monotonic()-began:.0f}s; free {shutil.disk_usage(work).free:,}", flush=True)
            for family, state in con.execute("SELECT family,state FROM families ORDER BY family").fetchall():
                if state == "done":
                    # Finish cleanup after a previous family commit.
                    for fragment in _fragments(con, family, work):
                        if fragment.exists():
                            _remove(fragment, work)
                    continue
                _check_space(config)
                info = _compact(con, config, schema, valid, work, family)
                print(f"Compact {family}: {info['rows']:,} rows, {info['files']} files; elapsed {time.monotonic()-began:.0f}s; free {shutil.disk_usage(work).free:,}", flush=True)
            report = _publish(con, config, schema, work)
            print(f"Published {config['destination']}: {report['output_rows']:,} rows; unresolved {report['unresolved_rows']:,}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        part = commands.add_parser(name)
        part.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        part.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
        part.add_argument("--markets", type=Path, default=DEFAULT_MARKETS)
        part.add_argument("--spill", type=Path)
        part.add_argument("--min-free-gib", type=float, default=25)
        part.add_argument("--memory-mib", type=int, default=1024)
        part.add_argument("--workers", type=int, default=1)
        part.add_argument("--target-mib", type=int, default=256)
        part.add_argument("--files-per-unit", type=int, default=64)
    for name in ("resume", "status", "verify"):
        part = commands.add_parser(name)
        part.add_argument("--run-id", required=True)
        part.add_argument("--work-root", type=Path, default=DEFAULT_DESTINATION.parent / ".trades_by_series_work")
    args = parser.parse_args(argv)
    if args.command in ("plan", "run"):
        if args.workers < 1 or args.memory_mib < 256 or args.target_mib < 1 or not 1 <= args.files_per_unit <= 256 or args.min_free_gib < 0:
            raise ValueError("Invalid worker, memory, target, unit size, or reserve")
        report, inventory, schema, valid = plan(
            args.source, args.destination, args.markets, args.min_free_gib,
            args.spill, args.memory_mib, args.workers, args.target_mib,
            args.files_per_unit,
        )
        print(json.dumps(report, indent=2))
        if args.command == "plan":
            return 0
        if not valid:
            raise RuntimeError("No validated local families; migration cannot start")
        if (args.source / "refresh.lock").exists():
            raise RuntimeError("Collector refresh.lock exists; stop collection and resolve the lock before capturing a snapshot")
        if (report["destination_free_bytes"] < report["estimated_destination_required_bytes"] or
                report["spill_free_bytes"] < report["estimated_spill_required_bytes"]):
            raise RuntimeError("Estimated disk shortfall; use another destination/spill volume or free space")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        destination = args.destination.resolve()
        work = destination.parent / ".trades_by_series_work" / run_id
        work.mkdir(parents=True, exist_ok=False)
        (work / "units").mkdir()
        (work / "candidate").mkdir()
        config = {
            "run_id": run_id, "source": str(args.source.resolve()), "destination": str(destination),
            "format_version": VERSION, "fingerprint_version": FINGERPRINT_VERSION,
            "markets": str(args.markets.resolve()), "spill": str(((args.spill / run_id) if args.spill else (work / "spill")).resolve()),
            "snapshot_time": _now(), "schema": _schema_json(schema), "schema_ipc": schema.serialize().to_pybytes().hex(),
            "partition_key": "ticker_family" if "series_ticker" in schema.names else "series_ticker",
            "valid_families": valid, "mapping_version": 1, "min_free_gib": args.min_free_gib,
            "memory_mib": args.memory_mib, "workers": args.workers, "target_mib": args.target_mib,
            "files_per_unit": args.files_per_unit, "duckdb_version": duckdb.__version__, "pyarrow_version": pa.__version__,
            "checkpoint_digests": {name: _file_digest(args.source / name) for name in ("checkpoint.json", "refresh_checkpoint.json") if (args.source / name).is_file()},
        }
        with _db(work / "manifest.sqlite") as con, con:
            for key, value in config.items():
                _set(con, key, value)
            _set(con, "state", "running")
            con.executemany("INSERT INTO sources(name,size,mtime_ns,rows,schema_key,digest) VALUES (?,?,?,?,?,NULL)", inventory)
            for index, first in enumerate(range(0, len(inventory), args.files_per_unit), 1):
                con.execute("INSERT INTO units VALUES (?,?,?,'pending',NULL,NULL,NULL)", (index, first, min(first + args.files_per_unit, len(inventory))))
        print(f"Run ID: {run_id}; manifest: {work / 'manifest.sqlite'}", flush=True)
        _run(config, work)
        return 0
    work = args.work_root.resolve() / args.run_id
    if not (work / "manifest.sqlite").is_file():
        raise ValueError(f"Unknown run ID: {args.run_id}")
    with _db(work / "manifest.sqlite") as con:
        config = _config(con)
        if not config or config.get("run_id") != args.run_id:
            raise ValueError(f"Unknown run ID: {args.run_id}")
        if args.command == "status":
            counts = dict(con.execute("SELECT state,count(*) FROM units GROUP BY state"))
            families = dict(con.execute("SELECT state,count(*) FROM families GROUP BY state"))
            processed_rows = con.execute("SELECT coalesce(sum(rows),0) FROM units WHERE state='done'").fetchone()[0]
            source_rows = con.execute("SELECT coalesce(sum(rows),0) FROM sources").fetchone()[0]
            reports = [json.loads(payload) for (payload,) in con.execute("SELECT files_json FROM families WHERE state='done'")]
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(config["snapshot_time"])).total_seconds()
            unresolved = con.execute("SELECT rows FROM families WHERE family='UNRESOLVED'").fetchone()
            print(json.dumps({
                "run_id": args.run_id,
                "state": config["state"],
                "units": counts,
                "families": families,
                "processed_rows": processed_rows,
                "source_rows": source_rows,
                "family_output_bytes": sum(item["bytes"] for item in reports),
                "unresolved_rows": unresolved[0] if unresolved else 0,
                "elapsed_seconds": round(elapsed),
                "free_bytes": shutil.disk_usage(work).free,
            }, indent=2))
        elif args.command == "resume":
            _run(config, work)
        else:
            if config["state"] != "published":
                raise RuntimeError("Run has not published a validated dataset")
            schema = pa.ipc.read_schema(pa.BufferReader(bytes.fromhex(config["schema_ipc"])))
            report = verify_output(con, config, schema)
            print(json.dumps(report, indent=2))
    return 0
