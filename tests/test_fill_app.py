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
    clv_route(app)
    assert not app.exception
    assert app.slider(key="explorer_p").value == 65
    assert app.sidebar.slider(key="explorer_p").value == 65
    assert len(app.get("dialog")) == 0
    app.slider(key="explorer_b").set_value(40).run(timeout=60)
    app.slider(key="explorer_clv").set_value(61).run(timeout=60)
    assert app.selectbox(key="surface_coordinate").value == "log_odds"
    assert len(calls) == 1
    app.selectbox(key="surface_coordinate").select("probability").run(timeout=60)
    assert not app.exception
    assert len(calls) == 2
    app.selectbox(key="surface_coordinate").select("log_odds").run(timeout=60)
    app._page_hash = ""
    app.run(timeout=60)
    assert not app.exception
    assert app.slider(key="estimator_b").value == 40
    assert app.slider(key="estimator_clv").value == 61
    assert len(calls) == 2  # Returning to log-odds reuses its cached fit.


def test_explorer_builds_at_fifty_cent_price_and_clv(tmp_path):
    write_family(tmp_path)
    path = tmp_path / "KXTEST/snapshots.parquet"
    pd.read_parquet(path).assign(clv=0.5, current_price=0.5).to_parquet(path)
    app = app_at(tmp_path)
    app.slider(key="estimator_clv").set_value(50)
    app.slider(key="estimator_p").set_value(50)
    app.slider(key="estimator_b").set_value(45).run()
    app.button(key="visualize").click().run(timeout=60)
    clv_route(app)
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
    assert len(app.get("plotly_chart")) == 2  # Full-page surface and linked curve.


def test_normalized_time_controls_do_not_force_the_thumb_back_on_reruns(tmp_path):
    write_family(tmp_path)
    app = app_at(tmp_path)
    for time in (0.17, 0.63):
        app.slider(key="estimator_t").set_value(time).run(timeout=60)
        assert not app.exception
        assert app.slider(key="estimator_t").value == time
        assert not app.slider(key="estimator_t").proto.set_value
    clv_route(app)
    assert app.slider(key="explorer_t").value == 0.63
    for time in (0.22, 0.71):
        app.slider(key="explorer_t").set_value(time).run(timeout=60)
        assert not app.exception
        assert app.slider(key="explorer_t").value == time
        assert not app.slider(key="explorer_t").proto.set_value
    app._page_hash = ""
    app.run(timeout=60)
    assert app.slider(key="estimator_t").value == 0.71


def clv_route(app):
    from streamlit.util import calc_hash

    app._page_hash = calc_hash("clv")
    # AppTest can retain elements emitted before st.switch_page even after their
    # widget state was cleaned up. Navigation must not serialize those orphans.
    return app._run(timeout=60)


def no_clv_route(app):
    # AppTest.switch_page only supports file pages. Callable st.Page uses the
    # hash of its explicit URL path; use the same page selection protocol here.
    from streamlit.util import calc_hash

    app._page_hash = calc_hash("no-clv")
    return app.run(timeout=60)


def test_no_clv_route_shared_game_selection_and_original_scenario(tmp_path):
    write_family(tmp_path)
    app = app_at(tmp_path)
    app.slider(key="estimator_clv").set_value(42).run()
    app.selectbox(key="ticker_KXTEST").select("KXTEST-0-A").run()
    no_clv_route(app)
    assert not app.exception
    assert app.selectbox(key="family").value == "KXTEST"
    assert app.selectbox(key="ticker_KXTEST").value == "KXTEST-0-A"
    assert not any("CLV" in slider.label for slider in app.slider)
    assert app.slider(key="no_clv_p").value == 60
    assert app.sidebar.slider(key="no_clv_p").value == 60
    assert app.get("bidi_component")
    assert len(app.get("plotly_chart")) == 8
    assert any("24 matches" in caption.value for caption in app.caption)
    app.slider(key="no_clv_b").set_value(0).run(timeout=60)
    assert (
        next(
            metric.value
            for metric in app.metric
            if metric.label == "Fitted probability at selected bid"
        )
        == "40.00%"
    )
    app.selectbox(key="ticker_KXTEST").select("KXTEST-1-A").run(timeout=60)
    app._page_hash = ""
    app.run(timeout=60)
    assert not app.exception
    assert app.selectbox(key="ticker_KXTEST").value == "KXTEST-1-A"
    assert app.slider(key="estimator_clv").value == 42
    no_clv_route(app)
    assert app.slider(key="no_clv_b").value == 0


def test_no_clv_controls_cache_animation_and_invalid_bid(tmp_path, monkeypatch):
    import src.analysis.kalshi.no_clv_fill_ui as ui

    calls = []
    original = ui.fit_model

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ui, "fit_model", counted)
    write_family(tmp_path)
    app = no_clv_route(app_at(tmp_path))
    assert not app.exception and len(calls) == 1
    app.session_state["no_clv_active_view"] = "Time Surface"
    app.slider(key="no_clv_b").set_value(40).run(timeout=60)
    assert app.session_state["no_clv_view"] == "Time Surface"
    app.session_state["no_clv_scenario"]["t"] = 0.3
    app.run(timeout=60)
    assert app.session_state["no_clv_view"] == "Time Surface"
    app.checkbox(key="no_clv_show_raw").uncheck().run(timeout=60)
    assert not app.exception and len(calls) == 1
    assert app.get("bidi_component")  # Native browser time slider and Play/Pause.
    app.slider(key="no_clv_b").set_value(60).run(timeout=60)
    assert not app.exception
    assert any("below" in warning.value for warning in app.warning)
    app.selectbox(key="no_clv_smoothing_mode").select("Manual by axis").run(timeout=60)
    assert not app.exception
    assert app.slider(key="no_clv_smooth_Price")


def test_no_clv_no_data_and_filtered_data_errors(tmp_path):
    app = no_clv_route(app_at(tmp_path))
    assert not app.exception
    assert any("No prepared families" in info.value for info in app.info)
    write_family(tmp_path)
    app.run(timeout=60)
    app.text_input(key="no_clv_cutoff").set_value("invalid timestamp").run(timeout=60)
    assert not app.exception
    assert any("cutoff" in error.value for error in app.error)
    app.text_input(key="no_clv_cutoff").set_value("2020-01-01").run(timeout=60)
    assert any("No reference observations" in error.value for error in app.error)


def test_odds_moneyness_route_controls_views_and_shared_game(tmp_path):
    from streamlit.util import calc_hash

    write_family(tmp_path)
    app = app_at(tmp_path)
    app._page_hash = calc_hash("odds-moneyness")
    app.run(timeout=60)
    app.selectbox(key="ticker_KXTEST").select("KXTEST-0-A").run(timeout=60)
    assert not app.exception and not app.error
    assert app.selectbox(key="family").value == "KXTEST"
    assert app.selectbox(key="ticker_KXTEST").value == "KXTEST-0-A"
    assert not any("CLV" in slider.label for slider in app.slider)
    assert app.sidebar.slider(key="odds_p").value == 60
    assert app.get("bidi_component")
    assert len(app.get("plotly_chart")) == 2
    app.session_state["odds_active_view"] = "3D Surface"
    app.slider(key="odds_b").set_value(40.0).run(timeout=60)
    assert not app.exception
    assert app.session_state["odds_view"] == "3D Surface"
    app.session_state["odds_scenario"]["t"] = 0.3
    app.checkbox(key="odds_show_raw").check().run(timeout=60)
    assert app.session_state["odds_view"] == "3D Surface"
    assert app.slider(key="odds_b").value == 40
    app.button(key="odds_reset_camera").click().run(timeout=60)
    assert app.session_state["odds_view"] == "3D Surface"
    assert app.session_state["odds_camera_revision"] == 1
    app.selectbox(key="ticker_KXTEST").select("KXTEST-1-A").run(timeout=60)
    assert not app.exception
    app._page_hash = ""
    app.run(timeout=60)
    assert not app.exception
    assert app.selectbox(key="ticker_KXTEST").value == "KXTEST-1-A"


def test_odds_moneyness_filter_error_and_stale_click(tmp_path, monkeypatch):
    from streamlit.util import calc_hash
    from src.analysis.kalshi import odds_moneyness_ui as ui
    from src.analysis.kalshi.odds_moneyness import distance

    state = dict(odds_scenario=dict(p=60.0, b=55.0, t=0.5), odds_token="current")
    with monkeypatch.context() as patch:
        patch.setattr(ui.st, "session_state", state)
        state["odds_playback"] = dict(
            selection=dict(
                token="stale",
                price=0.6,
                bid=0.55,
                time=0.3,
                distance=1.0,
                view="3D Surface",
            )
        )
        ui.commit_selection()
        assert state["odds_scenario"]["t"] == 0.5
        assert "odds_active_view" not in state
        state["odds_playback"]["selection"]["token"] = "current"
        ui.commit_selection()
        assert state["odds_scenario"]["t"] == 0.3
        assert state["odds_active_view"] == "3D Surface"
        assert distance(0.6, state["odds_scenario"]["b"] / 100) == pytest.approx(1.0)
    write_family(tmp_path)
    app = app_at(tmp_path)
    app._page_hash = calc_hash("odds-moneyness")
    app.run(timeout=60)
    app.text_input(key="odds_cutoff").set_value("invalid timestamp").run(timeout=60)
    assert not app.exception and any("cutoff" in error.value for error in app.error)


def test_oddness_scenario_fragment_does_not_reload_data_or_redraw_charts(
    tmp_path, monkeypatch
):
    """Exercise real fragment reruns; AppTest.run normally reruns the whole app."""
    from streamlit.proto.WidgetStates_pb2 import WidgetState, WidgetStates
    from streamlit.runtime.scriptrunner import RerunData, ScriptRunnerEvent
    from streamlit.testing.v1 import app_test
    from streamlit.testing.v1.local_script_runner import LocalScriptRunner
    from streamlit.util import calc_hash
    from src.analysis.kalshi import odds_moneyness_ui as ui

    write_family(tmp_path)
    app = app_at(tmp_path)
    app._page_hash = calc_hash("odds-moneyness")
    calls = dict(source=0, model=0, grid=0, charts=0)
    missing_widgets = []
    for module, name, label in [
        (pd, "read_parquet", "source"),
        (ui, "cached_model", "model"),
        (ui, "cached_grid", "grid"),
        (ui, "chart_figures", "charts"),
    ]:
        original = getattr(module, name)

        def counted(*args, _function=original, _label=label, **kwargs):
            calls[_label] += 1
            return _function(*args, **kwargs)

        monkeypatch.setattr(module, name, counted)

    class ScenarioRunner(LocalScriptRunner):
        changes = 0

        def _on_script_finished(self, ctx, event, premature_stop):
            super()._on_script_finished(ctx, event, premature_stop)
            if (
                event
                not in (
                    ScriptRunnerEvent.SCRIPT_STOPPED_WITH_SUCCESS,
                    ScriptRunnerEvent.FRAGMENT_STOPPED_WITH_SUCCESS,
                )
                or self.changes >= 4
            ):
                return
            states = self.session_state.get_widget_states()
            key, value = [
                ("odds_b", 40.0),
                ("odds_p", 80.0),
                ("odds_b_input", 40.25),
                ("odds_p_input", 81.25),
            ][self.changes]
            registry = self.session_state._state._new_widget_state.widget_metadata
            # The browser applies set_value responses to linked controls before
            # sending its next event. Reproduce that here rather than sending
            # the slider's previous value alongside a number-input edit.
            for paired_key in ("odds_p", "odds_b", "odds_p_input", "odds_b_input"):
                entry = next(
                    item
                    for widget_id, item in registry.items()
                    if widget_id.endswith(paired_key)
                )
                paired = next((item for item in states if item.id == entry.id), None)
                if paired is None:
                    paired = WidgetState(id=entry.id)
                    states.append(paired)
                current = self.session_state[paired_key]
                if paired_key.endswith("_input"):
                    paired.double_value = current
                else:
                    paired.double_array_value.data[:] = [current]
            metadata = next(
                (
                    entry
                    for widget_id, entry in registry.items()
                    if widget_id.endswith(key)
                ),
                None,
            )
            if metadata is None:
                missing_widgets.append((self.changes, list(registry)))
                return
            widget = next((state for state in states if state.id == metadata.id), None)
            if widget is None:
                widget = WidgetState(id=metadata.id)
                states.append(widget)
            assert metadata.fragment_id is not None
            if key.endswith("_input"):
                widget.double_value = value
            else:
                widget.double_array_value.data[:] = [value]
            self.changes += 1
            self.request_rerun(
                RerunData(
                    widget_states=WidgetStates(widgets=states),
                    page_script_hash=calc_hash("odds-moneyness"),
                    fragment_id=metadata.fragment_id,
                )
            )

    monkeypatch.setattr(app_test, "LocalScriptRunner", ScenarioRunner)
    app.run(timeout=60)
    assert not app.exception and not app.error
    assert not missing_widgets
    assert app.session_state["odds_scenario"]["p"] == 81.25
    assert app.session_state["odds_scenario"]["b"] == 40.25
    assert app.slider(key="odds_p").value == 81.25
    assert app.slider(key="odds_b").value == 40.25
    assert app.number_input(key="odds_p_input").value == 81.25
    assert app.number_input(key="odds_b_input").value == 40.25
    assert calls == dict(source=1, model=1, grid=1, charts=1)
    assert len(app.get("plotly_chart")) == 2
    assert len(app.metric) == 4  # Updated readouts replace their previous values.
    assert any(item.value == "Oddness Visualizer" for item in app.subheader)
