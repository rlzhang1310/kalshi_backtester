from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.indexers.kalshi import global_trades_refresh as refresh


def trade(trade_id: str, second: int) -> dict:
    return {
        "trade_id": trade_id,
        "ticker": "GAME-YES",
        "count_fp": "1.00",
        "yes_price_dollars": "0.60",
        "no_price_dollars": "0.40",
        "taker_side": "yes",
        "created_time": datetime.fromtimestamp(second, timezone.utc).isoformat(),
    }


def setup_archive(monkeypatch, tmp_path, *, active=True):
    data = tmp_path / "trades_global_staging"
    data.mkdir()
    monkeypatch.setattr(refresh, "DATA_DIR", data)
    monkeypatch.setattr(refresh, "BACKFILL_CHECKPOINT", data / "checkpoint.json")
    monkeypatch.setattr(refresh, "REFRESH_CHECKPOINT", data / "refresh_checkpoint.json")
    monkeypatch.setattr(refresh, "LOCK_FILE", data / "refresh.lock")
    original = {
        "sources": {name: {"completed": True, "cursor": None,
                           "updated_at": "2026-08-05T00:00:00+00:00"}
                    for name, _ in refresh.SOURCES},
    }
    refresh.BACKFILL_CHECKPOINT.write_text(json.dumps(original), encoding="utf-8")
    pd.DataFrame([{
        "trade_id": "old", "ticker": "GAME-YES", "created_time":
        datetime.fromtimestamp(1500, timezone.utc), "count": 1,
        "yes_price": 60, "no_price": 40, "taker_side": "yes",
    }]).to_parquet(data / "historical_00000000.parquet", index=False)
    if active:
        refresh.REFRESH_CHECKPOINT.write_text(json.dumps({
            "version": 1, "last_completed_end_ts": None,
            "active": {"min_ts": 1000, "max_ts": 2000,
                       "sources": {name: {"cursor": None, "completed": False,
                                          "pages": 0, "added": 0}
                                   for name, _ in refresh.SOURCES}},
        }), encoding="utf-8")
    return data, refresh.BACKFILL_CHECKPOINT.read_bytes()


def fake_client(monkeypatch, responses):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def http(self):
            return self

        def get(self, endpoint, params):
            return responses(endpoint, params)

    monkeypatch.setattr(refresh, "KalshiClient", Client)


def test_refresh_appends_only_new_ids_and_keeps_original_archive(monkeypatch, tmp_path):
    data, original_checkpoint = setup_archive(monkeypatch, tmp_path)
    original_batch = (data / "historical_00000000.parquet").read_bytes()

    def responses(endpoint, params):
        assert params["min_ts"] == 1000
        assert params["max_ts"] == 2000
        if endpoint == "/markets/trades":
            return {"trades": [trade("old", 1500), trade("new", 1600)], "cursor": None}
        return {"trades": [trade("new", 1600), trade("older", 1400)], "cursor": None}

    fake_client(monkeypatch, responses)
    refresh.KalshiGlobalTradesRefreshIndexer().run()
    files = list(data.glob("refresh_*.parquet"))
    assert len(files) == 2
    assert set(pd.concat([pd.read_parquet(path) for path in files]).trade_id) == {"new", "older"}
    assert (data / "historical_00000000.parquet").read_bytes() == original_batch
    assert refresh.BACKFILL_CHECKPOINT.read_bytes() == original_checkpoint
    checkpoint = json.loads(refresh.REFRESH_CHECKPOINT.read_text(encoding="utf-8"))
    assert checkpoint["active"] is None
    assert checkpoint["last_completed_end_ts"] == 2000


def test_refresh_resumes_after_committed_batch_without_duplicates(monkeypatch, tmp_path):
    data, _ = setup_archive(monkeypatch, tmp_path)
    monkeypatch.setattr(refresh, "BATCH_SIZE", 1)
    calls = []
    fail_once = True

    def responses(endpoint, params):
        nonlocal fail_once
        calls.append((endpoint, params.get("cursor")))
        if endpoint == "/markets/trades" and not params.get("cursor"):
            return {"trades": [trade("new", 1600)], "cursor": "next"}
        if endpoint == "/markets/trades" and fail_once:
            fail_once = False
            raise ConnectionError("interrupted")
        return {"trades": [trade("new", 1600)], "cursor": None}

    fake_client(monkeypatch, responses)
    with pytest.raises(ConnectionError):
        refresh.KalshiGlobalTradesRefreshIndexer().run()
    checkpoint = json.loads(refresh.REFRESH_CHECKPOINT.read_text(encoding="utf-8"))
    assert checkpoint["active"]["sources"]["live"]["cursor"] == "next"
    assert len(list(data.glob("refresh_*.parquet"))) == 1

    refresh.KalshiGlobalTradesRefreshIndexer().run()
    assert ("/markets/trades", "next") in calls
    files = list(data.glob("refresh_*.parquet"))
    assert len(files) == 1
    assert pd.read_parquet(files[0]).trade_id.tolist() == ["new"]


def test_committed_file_before_checkpoint_is_not_duplicated(monkeypatch, tmp_path):
    data, _ = setup_archive(monkeypatch, tmp_path)
    fake_client(monkeypatch, lambda _endpoint, _params: {
        "trades": [trade("new", 1600)], "cursor": None,
    })
    save = refresh._save_checkpoint
    fail_once = True

    def fail_after_batch(checkpoint):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("interrupted after writing batch")
        save(checkpoint)

    monkeypatch.setattr(refresh, "_save_checkpoint", fail_after_batch)
    with pytest.raises(OSError, match="interrupted"):
        refresh.KalshiGlobalTradesRefreshIndexer().run()
    assert len(list(data.glob("refresh_*.parquet"))) == 1
    monkeypatch.setattr(refresh, "_save_checkpoint", save)
    refresh.KalshiGlobalTradesRefreshIndexer().run()
    files = list(data.glob("refresh_*.parquet"))
    assert len(files) == 1
    assert pd.read_parquet(files[0]).trade_id.tolist() == ["new"]


def test_new_refresh_starts_before_earliest_completed_feed(monkeypatch, tmp_path):
    setup_archive(monkeypatch, tmp_path, active=False)
    checkpoint = refresh._load_or_start_checkpoint()
    original_finished = int(datetime(2026, 8, 5, tzinfo=timezone.utc).timestamp())
    assert checkpoint["active"]["min_ts"] == original_finished - int(refresh.OVERLAP.total_seconds())
    assert checkpoint["active"]["max_ts"] > checkpoint["active"]["min_ts"]


def test_no_original_backfill_or_files_fails_without_refresh_checkpoint(monkeypatch, tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    monkeypatch.setattr(refresh, "DATA_DIR", data)
    monkeypatch.setattr(refresh, "BACKFILL_CHECKPOINT", data / "checkpoint.json")
    monkeypatch.setattr(refresh, "REFRESH_CHECKPOINT", data / "refresh_checkpoint.json")
    monkeypatch.setattr(refresh, "LOCK_FILE", data / "refresh.lock")
    with pytest.raises(RuntimeError, match="Complete the Kalshi Global Trades backfill"):
        refresh.KalshiGlobalTradesRefreshIndexer().run()
    assert not refresh.REFRESH_CHECKPOINT.exists()
    assert not refresh.LOCK_FILE.exists()
