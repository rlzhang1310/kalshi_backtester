"""Bounded archive migration checks; no production archive is rewritten."""

import json
import sqlite3
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.analysis.kalshi import trade_reorganization as migrate


def table(tickers, ids, times, extra=None):
    data = {
        "ticker": pa.array(tickers, type=pa.string()),
        "trade_id": pa.array(ids, type=pa.string()),
        "created_time": pa.array(times, type=pa.timestamp("ns", tz="UTC")),
        "_fetched_at": pa.array([1000000001 + i for i in range(len(tickers))], type=pa.timestamp("ns", tz="UTC")),
        "price": pa.array([Decimal("0.10")] * len(tickers), type=pa.decimal128(8, 2)),
        "count": pa.array(range(1, len(tickers) + 1), type=pa.int32() if extra is None else pa.int64()),
    }
    if extra is not None:
        data["extra"] = pa.array(extra, type=pa.string())
    return pa.table(data)


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / "data" / "kalshi" / "trades_global_staging"
    markets = tmp_path / "data" / "kalshi" / "markets"
    destination = tmp_path / "data" / "kalshi" / "trades_by_series"
    source.mkdir(parents=True)
    markets.mkdir(parents=True)
    pq.write_table(table(["KXNFLGAME-A-X", "KXNBAGAME-B-Y", "KXNFLGAME-A-X", None], ["1", "2", "1", "z"], [1001, 2001, 1001, None]), source / "historical_00000000.parquet")
    pq.write_table(table(["KXNFLGAME-A-X", "KXUNKNOWN-C-Z", "KXNBAGAME-B-Y"], ["3", "4", "2"], [999, 1500, 2001], ["new", None, "dup"]), source / "refresh_live_00000000.parquet")
    pq.write_table(pa.table({"ticker": ["KXNFLGAME-A-X", "KXNBAGAME-B-Y"], "event_ticker": ["KXNFLGAME-A", "KXNBAGAME-B"]}), markets / "markets_0.parquet")
    return source, markets, destination


def run(archive):
    source, markets, destination = archive
    args = ["--source", str(source), "--destination", str(destination), "--markets", str(markets), "--min-free-gib", "0", "--target-mib", "1", "--files-per-unit", "1"]
    assert migrate.main(["run", *args]) == 0
    root = destination.parent / ".trades_by_series_work"
    run_id = next(root.iterdir()).name
    return run_id, root, destination


def test_mixed_and_refresh_exact_multiset(archive):
    source, markets, _ = archive
    before = {p.name: migrate._file_digest(p) for p in source.glob("*.parquet")}
    run_id, root, destination = run(archive)
    assert {p.name: migrate._file_digest(p) for p in source.glob("*.parquet")} == before
    _, common, _ = migrate._snapshot(source)
    source_rows = []
    for path in source.glob("*.parquet"):
        source_rows.extend(migrate._physical(migrate._read_file(path, common), common).to_pylist())
    output_rows = []
    for path in destination.glob("series_ticker=*/*.parquet"):
        output_rows.extend(migrate._physical(pq.ParquetFile(path).read(), common).to_pylist())
    def key(row):
        return migrate._json([[k, migrate._canonical(v)] for k, v in sorted(row.items())])
    assert sorted(map(key, source_rows)) == sorted(map(key, output_rows))
    assert len(list((destination / "series_ticker=KXNFLGAME").glob("*.parquet"))) == 1
    nfl_file = next((destination / "series_ticker=KXNFLGAME").glob("*.parquet"))
    nfl_parquet = pq.ParquetFile(nfl_file)
    assert nfl_parquet.read(columns=["created_time"]).column(0).cast(pa.int64()).to_pylist() == [999, 1001, 1001]
    names = nfl_parquet.schema_arrow.names
    time_stats = nfl_parquet.metadata.row_group(0).column(names.index("created_time")).statistics
    assert time_stats.has_min_max
    assert json.loads((destination / "_metadata" / "dataset.json").read_text())["unresolved_rows"] == 2
    assert migrate.main(["status", "--run-id", run_id, "--work-root", str(root)]) == 0
    assert migrate.main(["resume", "--run-id", run_id, "--work-root", str(root)]) == 0
    assert migrate.main(["verify", "--run-id", run_id, "--work-root", str(root)]) == 0


def test_changed_source_rejected(archive):
    run_id, root, _ = run(archive)
    source = archive[0]
    path = source / "historical_00000000.parquet"
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(RuntimeError, match="changed"):
        migrate._source_unchanged(sqlite3.connect(root / run_id / "manifest.sqlite"), source)


def test_incompatible_schema_rejected(archive):
    source = archive[0]
    pq.write_table(pa.table({"ticker": ["KXNBAGAME-B-Y"], "created_time": ["not a time"]}), source / "bad.parquet")
    with pytest.raises(ValueError, match="Incompatible|timestamp"):
        migrate._snapshot(source)


@pytest.mark.parametrize("stage", ["unit_before_rename", "unit_after_rename", "family_before_rename", "family_after_rename", "family_after_commit", "publish_after_rename"])
def test_resume_crash_boundaries(archive, monkeypatch, stage):
    source, markets, destination = archive
    original = migrate._fault
    triggered = False
    def failpoint(value):
        nonlocal triggered
        if value == stage and not triggered:
            triggered = True
            raise RuntimeError("injected failure")
    monkeypatch.setattr(migrate, "_fault", failpoint)
    args = ["--source", str(source), "--destination", str(destination), "--markets", str(markets), "--min-free-gib", "0", "--files-per-unit", "1"]
    with pytest.raises(RuntimeError, match="injected failure"):
        migrate.main(["run", *args])
    monkeypatch.setattr(migrate, "_fault", original)
    root = destination.parent / ".trades_by_series_work"
    run_id = next(root.iterdir()).name
    assert migrate.main(["resume", "--run-id", run_id, "--work-root", str(root)]) == 0
    assert migrate.main(["verify", "--run-id", run_id, "--work-root", str(root)]) == 0


def test_low_space_and_existing_destination(archive, monkeypatch):
    source, markets, destination = archive
    report, *_ = migrate.plan(source, destination, markets, reserve_gib=0)
    assert report["source_rows"] == 7
    destination.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        migrate.plan(source, destination, markets, reserve_gib=0)


def test_conflicting_market_relationship_unresolved(archive):
    _, markets, _ = archive
    pq.write_table(pa.table({"ticker": ["KXNFLGAME-X-Y"], "event_ticker": ["KXOTHER-X"]}), markets / "markets_1.parquet")
    good, bad = migrate._metadata_families(markets)
    assert "KXNFLGAME" not in good
    assert "KXNFLGAME" in bad


def test_explicit_market_series_conflict_is_rejected(archive):
    _, markets, _ = archive
    pq.write_table(pa.table({
        "ticker": ["KXNBAGAME-C-Z"],
        "event_ticker": ["KXNBAGAME-C"],
        "series_ticker": ["KXOTHER"],
    }), markets / "markets_1.parquet")
    good, bad = migrate._metadata_families(markets)
    assert "KXNBAGAME" in bad
    assert "KXNBAGAME" not in good


def test_original_series_column_is_preserved_and_conflict_is_unresolved(archive):
    source, _, destination = archive
    for path in source.glob("*.parquet"):
        raw = pq.ParquetFile(path).read()
        original = raw["ticker"].to_pylist()
        series = [value.split("-", 1)[0] if value else None for value in original]
        if path.name.startswith("refresh_"):
            series[0] = "KXOTHER"
        pq.write_table(raw.append_column("series_ticker", pa.array(series)), path)
    run_id, root, _ = run(archive)
    metadata = json.loads((destination / "_metadata" / "dataset.json").read_text())
    assert metadata["partition_key"] == "ticker_family"
    assert metadata["unresolved_rows"] == 3
    assert metadata["mapping_methods"]["explicit_source_series_confirmed"] == 4
    output = list(destination.glob("ticker_family=*/*.parquet"))
    assert sum(pq.ParquetFile(p).metadata.num_rows for p in output) == 7
    assert migrate.main(["verify", "--run-id", run_id, "--work-root", str(root)]) == 0


def test_low_space_pauses_before_committing_and_resumes(archive, monkeypatch):
    source, markets, destination = archive
    original = migrate._check_space
    monkeypatch.setattr(migrate, "_check_space", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Low space")))
    args = ["--source", str(source), "--destination", str(destination), "--markets", str(markets), "--min-free-gib", "0"]
    with pytest.raises(RuntimeError, match="Low space"):
        migrate.main(["run", *args])
    monkeypatch.setattr(migrate, "_check_space", original)
    root = destination.parent / ".trades_by_series_work"
    run_id = next(root.iterdir()).name
    assert migrate.main(["resume", "--run-id", run_id, "--work-root", str(root)]) == 0


def test_second_writer_cannot_take_run_lock(tmp_path):
    with migrate._lock(tmp_path):
        with pytest.raises(RuntimeError, match="lock"):
            with migrate._lock(tmp_path):
                pass


def test_windows_transient_directory_rename_is_retried(tmp_path, monkeypatch):
    source = tmp_path / "unit.pending"
    destination = tmp_path / "unit"
    source.mkdir()
    real_replace = migrate.os.replace
    attempts = 0
    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("temporary Windows sharing violation")
            error.winerror = 5
            raise error
        return real_replace(src, dst)
    monkeypatch.setattr(migrate.os, "replace", flaky_replace)
    monkeypatch.setattr(migrate.time, "sleep", lambda _: None)
    migrate._rename_directory(source, destination)
    assert attempts == 3
    assert destination.is_dir()
