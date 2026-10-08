"""Small published-dataset fixtures for selected Kalshi trade ingestion."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.analysis.kalshi.trade_reorganization import _schema_json, _lock
from src.analysis.kalshi.util.trades import (
    create_family_trades_view, create_organized_trades_view, family_trade_files,
)
from src.indexers.kalshi import scoped_trades as scoped
from src.analysis.kalshi.meta_stats import MetaStatsAnalysis
from src.analysis.kalshi.volume_over_time import VolumeOverTimeAnalysis


FAMILY = "KXNFLGAME"
A = "KXNFLGAME-ONE-YES"
B = "KXNFLGAME-TWO-YES"
OTHER = "KXNBAGAME-ONE-YES"
SCHEMA = pa.schema([
    ("trade_id", pa.string()), ("ticker", pa.string()), ("count", pa.int64()),
    ("yes_price", pa.int64()), ("no_price", pa.int64()),
    ("taker_side", pa.string()), ("created_time", pa.timestamp("ns", tz="UTC")),
    ("_fetched_at", pa.timestamp("ns", tz="UTC")),
])


def trade(trade_id, ticker=A, when=110, price=42):
    return {"trade_id": trade_id, "ticker": ticker, "count_fp": "1.00",
            "yes_price_dollars": f"{price / 100:.2f}",
            "no_price_dollars": f"{(100 - price) / 100:.2f}",
            "taker_side": "yes",
            "created_time": datetime.fromtimestamp(when, timezone.utc).isoformat()}


def baseline_row(trade_id="base"):
    instant = datetime.fromtimestamp(105, timezone.utc)
    return {"trade_id": trade_id, "ticker": A, "count": 1,
            "yes_price": 42, "no_price": 58, "taker_side": "yes",
            "created_time": instant, "_fetched_at": instant}


@pytest.fixture
def published(tmp_path):
    destination = tmp_path / "trades_by_series"
    folder = destination / f"series_ticker={FAMILY}"
    folder.mkdir(parents=True)
    metadata = destination / "_metadata"
    metadata.mkdir()
    pq.write_table(pa.Table.from_pylist([baseline_row()], schema=SCHEMA), folder / "part-000.parquet")
    pq.write_table(pa.table({"family": [FAMILY], "row_count": [1]}), metadata / "family_catalog.parquet")
    (metadata / "dataset.json").write_text(json.dumps({
        "run_id": "baseline-1", "validation_status": "passed",
        "partition_key": "series_ticker", "schema": _schema_json(SCHEMA),
    }))
    markets = tmp_path / "markets"
    markets.mkdir()
    pq.write_table(pa.table({"ticker": [A, OTHER],
                             "event_ticker": ["KXNFLGAME-ONE", "KXNBAGAME-ONE"]}),
                   markets / "markets-0.parquet")
    return destination, markets


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.data = {}
        self.fail = None

    def get(self, endpoint, *, params):
        self.calls.append((endpoint, dict(params)))
        if self.fail:
            result = self.fail(endpoint, params)
            if result is not None:
                return result
        value = self.data.get((endpoint, params.get("ticker"), params.get("cursor")))
        if value is not None:
            return value
        if endpoint.endswith("/markets") or endpoint == "/markets":
            return {"markets": [], "cursor": None}
        return {"trades": [], "cursor": None}


class FakeClient:
    def __init__(self, http):
        self.http = http

    def get_market(self, ticker, historical=False):
        return SimpleNamespace(ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0],
                               status="active", close_time=None)


def collector(published, http, now=200):
    destination, markets = published
    return scoped.ScopedTrades(FakeClient(http), destination=destination,
                               markets_dir=markets, min_free_gib=0, now=lambda: now)


def rows(destination):
    return [item for path in (destination / f"series_ticker={FAMILY}").glob("*.parquet")
            for item in pq.ParquetFile(path).read().to_pylist()]


def state(destination):
    return json.loads((destination / "_metadata" / scoped.STATE_NAME).read_text())


def test_family_filters_every_trade_request_and_rerun_deduplicates(published):
    http = FakeHTTP()
    http.data[("/markets", None, None)] = {"markets": [
        {"ticker": A, "event_ticker": "KXNFLGAME-ONE", "status": "active"}], "cursor": None}
    http.data[("/historical/markets", None, None)] = {"markets": [
        {"ticker": B, "event_ticker": "KXNFLGAME-TWO", "status": "settled"}], "cursor": None}
    http.data[("/markets/trades", A, None)] = {"trades": [trade("new", A, 115), trade("base", A, 105)], "cursor": None}
    http.data[("/historical/trades", A, None)] = {"trades": [trade("new", A, 115)], "cursor": None}
    http.data[("/historical/trades", B, None)] = {"trades": [trade("b", B, 180)], "cursor": None}
    first = collector(published, http).run(family=FAMILY, since=100)
    assert first["stats"]["failed"] == []
    assert {r["trade_id"] for r in rows(published[0])} == {"base", "new", "b"}
    assert first["stats"]["new"] == 2
    assert {p["ticker"] for e, p in http.calls if e.endswith("/trades")} == {A, B}
    assert all(p["series_ticker"] == FAMILY for e, p in http.calls if e.endswith("/markets"))
    http.calls.clear()
    second = collector(published, http, now=220).run(family=FAMILY)
    assert second["stats"]["new"] == 0
    assert state(published[0])["markets"][A]["coverage"] == [[0, 220]]
    assert state(published[0])["markets"][B]["coverage"] == [[0, 220]]


def test_exact_empty_window_and_disjoint_repair(published):
    http = FakeHTTP()
    updater = collector(published, http, now=300)
    assert updater.run(ticker=A, since=200, until=210)["stats"]["failed"] == []
    assert state(published[0])["markets"][A]["coverage"] == [[200, 210]]
    collector(published, http, now=300).run(ticker=A, since=250, until=260)
    assert state(published[0])["markets"][A]["coverage"] == [[200, 210], [250, 260]]
    assert {p["ticker"] for e, p in http.calls if e.endswith("/trades")} == {A}
    assert len(rows(published[0])) == 1


def test_newest_first_cursor_and_late_tier_movement(published):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("newer", when=190)], "cursor": "p2"}
    http.data[("/markets/trades", A, "p2")] = {"trades": [
        trade("older", when=130), trade("same-time", when=130)], "cursor": None}
    collector(published, http).run(ticker=A, since=100)
    assert {r["trade_id"] for r in rows(published[0])} == {"base", "newer", "older", "same-time"}
    http.data[("/markets/trades", A, None)] = {"trades": [], "cursor": None}
    http.data[("/historical/trades", A, None)] = {"trades": [
        trade("newer", when=190), trade("late", when=195)], "cursor": None}
    report = collector(published, http, now=240).run(ticker=A)
    assert report["stats"]["new"] == 1
    assert {r["trade_id"] for r in rows(published[0])} == {"base", "newer", "older", "same-time", "late"}


def test_crash_after_publish_recovers_without_duplicate(published, monkeypatch):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("new")], "cursor": None}
    called = False
    def crash(stage):
        nonlocal called
        if stage == "after_publish_before_checkpoint" and not called:
            called = True
            raise RuntimeError("injected crash")
    monkeypatch.setattr(scoped, "_fault", crash)
    first = collector(published, http).run(ticker=A, since=100)
    assert first["stats"]["failed"]
    monkeypatch.setattr(scoped, "_fault", lambda _: None)
    result = collector(published, http).run(ticker=A, since=100)
    assert result["stats"]["failed"] == []
    assert result["stats"]["recovered_files"] == 1
    assert [r["trade_id"] for r in rows(published[0])].count("new") == 1


def test_expired_cursor_replays_window_and_fails_on_conflict(published):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("new")], "cursor": "expired"}
    failed_once = False
    def expire(endpoint, params):
        nonlocal failed_once
        if endpoint == "/markets/trades" and params.get("cursor") == "expired":
            if not failed_once:
                failed_once = True
                request = httpx.Request("GET", "https://example.test")
                raise httpx.HTTPStatusError("expired", request=request,
                                            response=httpx.Response(400, request=request))
            return {"trades": [], "cursor": None}
    http.fail = expire
    result = collector(published, http).run(ticker=A, since=100)
    assert result["stats"]["failed"] == []
    assert [r["trade_id"] for r in rows(published[0])].count("new") == 1
    http.data[("/historical/trades", A, None)] = {"trades": [trade("new", price=43)], "cursor": None}
    result = collector(published, http, now=220).run(ticker=A)
    assert "Conflicting payload" in result["stats"]["failed"][0]["error"]


def test_invalid_and_missing_published_dataset_are_safe(published, tmp_path):
    http = FakeHTTP()
    with pytest.raises(ValueError, match="exactly one"):
        collector(published, http).run()
    with pytest.raises(ValueError, match="full market ticker"):
        collector(published, http).run(ticker=FAMILY)
    with pytest.raises(RuntimeError, match="Published family dataset"):
        scoped.ScopedTrades(FakeClient(http), destination=tmp_path / "missing").run(ticker=A, since=100)
    assert not http.calls
    with _lock(published[0] / "_metadata"):
        with pytest.raises(RuntimeError, match="lock"):
            collector(published, http).run(ticker=A, since=100)


def test_dry_run_and_failed_discovery_do_not_claim_success(published):
    http = FakeHTTP()
    before = set(published[0].rglob("*"))
    report = collector(published, http).run(family=FAMILY, since=100, dry_run=True)
    assert report["dry_run"] and report["markets"] == 1
    assert set(published[0].rglob("*")) == before
    def fail(endpoint, params):
        if endpoint == "/historical/markets":
            raise RuntimeError("historical discovery unavailable")
    http.fail = fail
    with pytest.raises(RuntimeError, match="discovery unavailable"):
        collector(published, http).run(family=FAMILY, since=100)
    assert state(published[0])["families"][FAMILY]["discovery_active"] is not None


def test_new_market_uses_family_start_and_each_market_has_own_coverage(published):
    http = FakeHTTP()
    collector(published, http).run(family=FAMILY, since=100)
    http.data[("/historical/markets", None, None)] = {"markets": [
        {"ticker": B, "event_ticker": "KXNFLGAME-TWO", "status": "settled"}], "cursor": None}
    http.data[("/historical/trades", B, None)] = {"trades": [trade("b", B, 120)], "cursor": None}
    collector(published, http, now=220).run(family=FAMILY)
    saved = state(published[0])
    assert saved["markets"][B]["coverage"] == [[100, 220]]
    assert saved["markets"][A]["coverage"][-1][1] == 220
    assert {r["trade_id"] for r in rows(published[0])} == {"base", "b"}


def test_second_feed_failure_keeps_window_incomplete_then_resumes(published):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("live")], "cursor": None}
    failed = True
    def fail(endpoint, params):
        if endpoint == "/historical/trades" and failed:
            raise RuntimeError("historical feed unavailable")
    http.fail = fail
    result = collector(published, http).run(ticker=A, since=100)
    assert result["stats"]["failed"]
    saved = state(published[0])["markets"][A]
    assert saved["coverage"] == [] and saved["active"] is not None
    failed = False
    result = collector(published, http).run(ticker=A, since=100)
    assert result["stats"]["failed"] == []
    assert state(published[0])["markets"][A]["coverage"] == [[100, 200]]
    assert [r["trade_id"] for r in rows(published[0])].count("live") == 1


def test_disk_full_and_precision_changes_do_not_commit_coverage(published, monkeypatch):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("new")], "cursor": None}
    original = scoped._check_space
    monkeypatch.setattr(scoped, "_check_space", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Low space")))
    result = collector(published, http).run(ticker=A, since=100)
    assert "Low space" in result["stats"]["failed"][0]["error"]
    assert state(published[0])["markets"][A]["coverage"] == []
    monkeypatch.setattr(scoped, "_check_space", original)
    http.data[("/markets/trades", A, None)] = {"trades": [{**trade("new"), "count_fp": "1.5"}], "cursor": None}
    result = collector(published, http).run(ticker=A, since=100)
    assert "fractional contracts" in result["stats"]["failed"][0]["error"]
    assert state(published[0])["markets"][A]["coverage"] == []


def test_unpublished_and_incompatible_schema_rejected_before_api(published):
    http = FakeHTTP()
    metadata = published[0] / "_metadata" / "dataset.json"
    item = json.loads(metadata.read_text())
    item["schema"][0]["type"] = "int64"
    metadata.write_text(json.dumps(item))
    with pytest.raises(RuntimeError, match="schema"):
        collector(published, http).run(ticker=A, since=100)
    assert http.calls == []


def test_organized_reader_sees_baseline_and_appends(published):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("new")], "cursor": None}
    collector(published, http).run(ticker=A, since=100)
    with duckdb.connect() as con:
        create_family_trades_view(con, published[0], FAMILY)
        assert set(row[0] for row in con.execute("SELECT trade_id FROM analysis_trades").fetchall()) == {"base", "new"}


def test_whole_dollar_price_is_preserved(published):
    http = FakeHTTP()
    http.data[("/markets/trades", A, None)] = {"trades": [trade("certain", price=100)], "cursor": None}
    result = collector(published, http).run(ticker=A, since=100)
    assert result["stats"]["failed"] == []
    assert next(row for row in rows(published[0]) if row["trade_id"] == "certain")["yes_price"] == 100


def test_discovery_resumes_after_expired_saved_cursor(published):
    http = FakeHTTP()
    http.data[("/markets", None, None)] = {"markets": [], "cursor": "old"}
    initially_failed = True
    def fail(endpoint, params):
        nonlocal initially_failed
        if endpoint == "/markets" and params.get("cursor") == "old":
            request = httpx.Request("GET", "https://example.test")
            if initially_failed:
                initially_failed = False
                raise RuntimeError("interrupted discovery")
            raise httpx.HTTPStatusError("expired", request=request,
                                        response=httpx.Response(400, request=request))
    http.fail = fail
    with pytest.raises(RuntimeError, match="interrupted discovery"):
        collector(published, http).run(family=FAMILY, since=100)
    http.data[("/markets", None, None)] = {"markets": [], "cursor": None}
    report = collector(published, http).run(family=FAMILY, since=100)
    assert report["stats"]["failed"] == []
    assert state(published[0])["families"][FAMILY]["discovery_active"] is None


def test_reader_prunes_other_families_and_accepts_alternate_partition_key(published):
    root = published[0]
    other = root / "series_ticker=KXNBAGAME"
    other.mkdir()
    other_file = other / "part-000.parquet"
    other_file.write_text("invalid unrelated Parquet")
    with duckdb.connect() as con:
        create_organized_trades_view(con, root, families=[FAMILY])
        assert [row[0] for row in con.execute("SELECT trade_id FROM analysis_trades").fetchall()] == ["base"]
        pq.write_table(pa.Table.from_pylist([{**baseline_row("other"),
                                              "ticker": OTHER}], schema=SCHEMA),
                       other_file)
        create_organized_trades_view(con, root)
        assert {row[0] for row in con.execute("SELECT trade_id FROM analysis_trades").fetchall()} == {"base", "other"}
    for folder in (root / f"series_ticker={FAMILY}", other):
        folder.rename(root / folder.name.replace("series_ticker=", "ticker_family="))
    metadata = root / "_metadata" / "dataset.json"
    info = json.loads(metadata.read_text())
    info["partition_key"] = "ticker_family"
    metadata.write_text(json.dumps(info))
    assert len(family_trade_files(root, FAMILY)) == 1
    with duckdb.connect() as con:
        create_family_trades_view(con, root, FAMILY)
        assert con.execute("SELECT count(*) FROM analysis_trades").fetchone()[0] == 1


def test_cross_family_analyses_use_published_trades(published):
    root, markets = published
    stats = MetaStatsAnalysis(trades_dir=root, markets_dir=markets).run()
    volume = VolumeOverTimeAnalysis(trades_dir=root).run()
    assert stats.data.loc[stats.data.metric == "num_trades", "value"].iloc[0] == 1
    assert volume.data.volume_usd.iloc[0] == 1
