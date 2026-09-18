"""Exercise dashboard interactions without API calls or archive scans."""

import json
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

import fill_app


APP = str(Path(fill_app.__file__).resolve())


def write_family(root, family="KXTEST"):
    folder = root / family
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            dict(
                ticker=f"{family}-{i}-A",
                event_ticker=f"{family}-{i}",
                clv=0.6,
                current_price=0.6,
                normalized_time=0.5,
                future_min_cents=50 if i < 15 else 55,
                settlement_time=pd.Timestamp("2026-01-02", tz="UTC"),
            )
            for i in range(25)
        ]
    ).to_parquet(folder / "snapshots.parquet")
    (folder / "manifest.json").write_text(
        json.dumps(
            dict(
                family=family,
                audit_counts={"included": 25},
                n_snapshots=25,
            )
        )
    )


def app_at(root):
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.sidebar.text_input[0].set_value(str(root)).run()
    assert not app.exception
    return app


def test_discovery_ignores_incomplete_and_corrupt(tmp_path):
    write_family(tmp_path)
    (tmp_path / "KXPARTIAL").mkdir()
    (tmp_path / "KXPARTIAL/manifest.json").write_text('{"family":"KXPARTIAL"}')
    (tmp_path / "KXBROKEN").mkdir()
    (tmp_path / "KXBROKEN/manifest.json").write_text("{")
    assert list(fill_app.prepared_families(tmp_path)) == ["KXTEST"]


def test_query_updates_and_validates(tmp_path):
    write_family(tmp_path)
    pd.DataFrame([dict(ticker="KXTEST-EXCLUDED-A", status="no_pregame_price")]).to_csv(
        tmp_path / "KXTEST/ticker_summary.csv",
        index=False,
    )
    app = app_at(tmp_path)
    assert app.selectbox(key="family").value == "KXTEST"
    assert [item.value for item in app.metric][-3:] == ["60.00%", "100.00%", "40.00 pp"]
    app.selectbox(key="ticker_KXTEST").select("KXTEST-EXCLUDED-A").run()
    assert any("no_pregame_price" in item.value for item in app.info)
    app.selectbox(key="ticker_KXTEST").select("KXTEST-0-A").run()
    assert any("24 matches" in item.value for item in app.caption)
    next(item for item in app.slider if "bid y" in item.label).set_value(60).run()
    assert any("below" in item.value for item in app.warning)
    assert len(app.metric) == 3  # No stale estimates after invalid input.
    next(item for item in app.slider if "bid y" in item.label).set_value(55)
    next(item for item in app.slider if "CLV" in item.label).set_value(10).run()
    assert any("Only 0 reference" in item.value for item in app.warning)
    assert not app.exception


def test_prepare_adds_family_and_recovers_from_failure(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    assert any("No prepared families" in item.value for item in app.info)

    def prepare(**kwargs):
        kwargs["progress"]("Test preparation progress")
        write_family(kwargs["output_dir"], kwargs["family"])

    monkeypatch.setattr(
        "src.analysis.kalshi.fill_probability_data.prepare_fill_data", prepare
    )
    app.text_input(key="new_family").set_value("KXNEW")
    next(item for item in app.button if item.label == "Run family").click().run()
    assert not app.exception
    assert app.selectbox(key="family").value == "KXNEW"

    def fail(**kwargs):
        raise ValueError("No local markets for KXMISSING.")

    monkeypatch.setattr(
        "src.analysis.kalshi.fill_probability_data.prepare_fill_data", fail
    )
    app.text_input(key="new_family").set_value("KXMISSING")
    next(item for item in app.button if item.label == "Run family").click().run()
    assert any("No local markets" in item.value for item in app.error)
    assert app.selectbox(key="family").value == "KXNEW"
    assert not app.exception


def test_explorer_shares_scenario_and_does_not_refit_for_bid(tmp_path, monkeypatch):
    import src.analysis.kalshi.fill_surface_ui as ui

    original = ui.fit_surface
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ui, "fit_surface", counted)
    write_family(tmp_path)
    app = app_at(tmp_path)
    app.slider(key="estimator_p").set_value(65).run()
    app.button(key="visualize").click().run(timeout=60)
    assert not app.exception
    assert app.slider(key="explorer_p").value == 65
    app.slider(key="explorer_b").set_value(40).run(timeout=60)
    app.slider(key="explorer_clv").set_value(61).run(timeout=60)
    app.button(key="close_surface").click().run(timeout=60)
    assert not app.exception
    assert app.slider(key="estimator_b").value == 40
    assert app.slider(key="estimator_clv").value == 61
    assert len(calls) == 1


def test_explorer_builds_at_fifty_cent_price_and_clv(tmp_path):
    write_family(tmp_path)
    path = tmp_path / "KXTEST/snapshots.parquet"
    pd.read_parquet(path).assign(clv=0.5, current_price=0.5).to_parquet(path)
    app = app_at(tmp_path)
    app.slider(key="estimator_clv").set_value(50)
    app.slider(key="estimator_p").set_value(50)
    app.slider(key="estimator_b").set_value(45).run()
    app.button(key="visualize").click().run(timeout=60)
    assert not app.exception
    assert not app.error
    assert app.slider(key="explorer_b").value == 45
    assert any(
        item.label == "Fitted probability at selected bid" for item in app.metric
    )
    assert any(item.label == "Raw probability at selected bid" for item in app.metric)
    assert (
        next(item for item in app.selectbox if item.label == "Zero-bid boundary").value
        == "empirical"
    )
    assert len(app.get("plotly_chart")) == 3  # Existing curve plus two explorer plots.
