"""Playback/session bridge and locally bundled component checks."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("streamlit")
from src.analysis.kalshi import fill_playback


def test_pause_sync_only_accepts_current_selection(monkeypatch):
    selection = dict(
        time=0.37, token="current", family="KXTEST", ticker=None, price=0.6, bid=0.2
    )
    state = dict(
        no_clv_playback={"selection": selection},
        no_clv_playback_token="current",
        no_clv_scenario=dict(p=60, b=20, t=0.5),
        family="KXTEST",
        ticker_KXTEST=None,
    )
    monkeypatch.setattr(fill_playback, "st", SimpleNamespace(session_state=state))
    fill_playback.commit_playback_time()
    assert state["no_clv_scenario"]["t"] == 0.37
    for changes in [
        dict(token="stale"),
        dict(family="OTHER"),
        dict(ticker="OTHER-A"),
        dict(price=0.5),
        dict(bid=0.3),
        dict(time=50),
        dict(time=float("nan")),
    ]:
        state["no_clv_playback"]["selection"] = {**selection, **changes}
        fill_playback.commit_playback_time()
        assert state["no_clv_scenario"]["t"] == 0.37


def test_bundled_playback_is_a_valid_javascript_module():
    node = shutil.which("node")
    if not node:
        runtime = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        )
        node = str(runtime) if runtime.exists() else None
    if not node:
        pytest.skip("Node is unavailable for component syntax verification.")
    source = fill_playback.component_source()
    assert "export default function(component)" in source
    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=source,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
