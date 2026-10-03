from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

import visualize_game
from src.analysis.kalshi.game_overlays import (
    load_game_overlays,
    prepare_pbp_events,
    prepare_sportsbook_odds,
)
from src.analysis.kalshi.local_game_data import list_local_games
from src.analysis.kalshi.single_game_odds_time_series import (
    build_game_odds_figure,
    load_game_odds,
    visualize_game_odds,
)
from src.indexers.kalshi.models import Market


EVENT = "KXNFLGAME-25SEP07CINCLE"
START = "2025-09-07T17:00:00Z"


@pytest.fixture
def archive(tmp_path):
    markets = tmp_path / "kalshi" / "markets"
    trades = tmp_path / "kalshi" / "trades_global_staging"
    markets.mkdir(parents=True)
    trades.mkdir()
    rows = [
        asdict(
            Market.from_dict(
                {
                    "ticker": f"{event}-{side}",
                    "event_ticker": event,
                    "yes_sub_title": label,
                    "status": "settled",
                    "title": "Cincinnati vs Cleveland Winner?",
                }
            )
        )
        for event, side, label in [
            (EVENT, "CLE", "Cleveland"),
            (EVENT, "CIN", "Cincinnati"),
            ("KXNFLSPREAD-25SEP07CINCLE", "CLE", "Cleveland"),
        ]
    ]
    pd.DataFrame(rows).to_parquet(markets / "markets_0_10000.parquet")
    frame = pd.DataFrame(
        {
            "trade_id": ["pregame", "home", "away", "one-cent", "bad"],
            "ticker": [
                f"{EVENT}-CLE",
                f"{EVENT}-CLE",
                f"{EVENT}-CIN",
                f"{EVENT}-CLE",
                f"{EVENT}-CLE",
            ],
            "count": [1] * 5,
            "yes_price": [50, 60, 0, 1, 120],
            "no_price": [50, 40, 60, 99, -20],
            "taker_side": ["yes", "yes", "no", "yes", "yes"],
            "created_time": pd.to_datetime(
                [
                    "2025-09-07T16:00:00Z",
                    START,
                    "2025-09-07T17:00:02Z",
                    "2025-09-07T17:00:04Z",
                    "2025-09-07T17:00:05Z",
                ]
            ),
        }
    )
    frame.to_parquet(trades / "historical_0.parquet")
    frame.iloc[[1]].to_parquet(trades / "live_0.parquet")
    (trades / "._historical_0.parquet").write_text("not parquet")
    return tmp_path


def test_catalog_search_and_sport_exclude_spreads(archive):
    games = list_local_games(archive, sport="NFL", search="Cleveland")
    assert games["event_ticker"].tolist() == [EVENT]
    assert games.iloc[0].market_count == 2
    assert list_local_games(archive, sport="nba").empty
    assert list_local_games(archive, search="[").empty  # literal, not a regex


def test_local_prices_repair_no_side_deduplicate_and_filter(archive):
    game = load_game_odds(EVENT, source="local", data_dir=archive, start_time=START)
    assert game.target_side == "CLE"
    assert game.trades.trade_id.tolist() == ["home", "away", "one-cent"]
    assert game.compiled.target_raw_prob.tolist() == pytest.approx([0.6, 0.6, 0.01])
    assert game.start_time_source == "provided"
    assert game.trades.yes_price.tolist() == pytest.approx([0.6, 0.4, 0.01])


def test_local_child_ticker_and_no_start_require_no_api(archive, monkeypatch):
    def no_api():
        pytest.fail("Local loading must not open an API client")

    monkeypatch.setattr(
        "src.analysis.kalshi.single_game_odds_time_series.KalshiClient", no_api
    )
    game = load_game_odds(EVENT + "-CIN", source="local", data_dir=archive)
    assert game.target_side == "CIN"
    assert game.start_time_source == "first_local_trade"
    assert game.compiled.target_raw_prob.tolist() == pytest.approx(
        [0.5, 0.4, 0.4, 0.99]
    )


def test_missing_local_event_and_empty_window_have_actionable_errors(archive):
    with pytest.raises(ValueError, match="source api"):
        load_game_odds("MISSING", source="local", data_dir=archive)
    with pytest.raises(ValueError, match="No local trades"):
        load_game_odds(EVENT, source="local", data_dir=archive, start_time="2030-01-01")
    with pytest.raises(ValueError, match="before"):
        load_game_odds(
            EVENT,
            source="local",
            data_dir=archive,
            start_time="2030-01-01",
            end_time=START,
        )


def test_picker_selects_a_game_and_outcome(archive, monkeypatch):
    answers = iter(["/not-a-team", "/Cleveland", "1", "0", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert visualize_game.choose_local_game(archive, sport="nfl") == EVENT + "-CLE"


@pytest.mark.parametrize(
    "answers",
    [
        ["1", (EVENT + "-CLE").lower(), "1"],
        ["2", "no matching game", "Cincinnati Cleveland", "1", "2"],
    ],
)
def test_menu_selects_and_renders_without_ticker_parameters(
    archive, monkeypatch, answers
):
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    output = archive / "menu-chart.html"
    assert (
        visualize_game.main(
            [
                "--data-dir",
                str(archive),
                "--no-show",
                "--no-resample",
                "--output-html",
                str(output),
            ]
        )
        == 0
    )
    assert output.is_file()
    assert "Cleveland Implied Probability" in output.read_text(encoding="utf-8")


def test_menu_ticker_api_option_does_not_require_local_catalog(tmp_path, monkeypatch):
    responses = iter(["", "", EVENT.lower(), "invalid", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert visualize_game.choose_game(tmp_path) == (EVENT, "api")


def test_menu_honors_explicit_source_and_can_quit(tmp_path, monkeypatch):
    responses = iter(["1", EVENT])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert visualize_game.choose_game(tmp_path, source="local") == (EVENT, "local")
    monkeypatch.setattr("builtins.input", lambda _: "q")
    with pytest.raises(SystemExit) as exc:
        visualize_game.choose_game(tmp_path)
    assert exc.value.code == 0


def test_absent_and_empty_overlays_leave_odds_and_volume_chart(archive):
    game = load_game_odds(EVENT, source="local", data_dir=archive)
    books, events = load_game_overlays(game, data_dir=archive)
    assert books.empty and events.empty
    figure = visualize_game_odds(
        EVENT, source="local", data_dir=archive, show=False, resample_frequency=None
    )
    assert len(figure.data) == 4
    assert figure.data[-1].name == "Volume"
    assert list(figure.data[-1].y) == [1, 3]
    assert "yaxis2" in figure.layout
    assert "yaxis3" not in figure.layout
    folder = archive / "playbyplay"
    folder.mkdir()
    (folder / f"{EVENT}.csv").write_text("timestamp,event_type\n")
    assert load_game_overlays(game, data_dir=archive)[1].empty
    with pytest.raises(ValueError, match="does not exist"):
        load_game_overlays(
            game, data_dir=archive, playbyplay_path=archive / "missing.csv"
        )


def test_sportsbook_uses_direct_team_odds_without_opponent_inversion():
    books = pd.DataFrame(
        {
            "timestamp": [START] * 2,
            "book": ["Book"] * 2,
            "target_team": ["CLE", "CIN"],
            "raw_odds": [-150, 130],
        }
    )
    result = prepare_sportsbook_odds(books, target="cle")
    assert result.target_raw_prob.tolist() == pytest.approx([0.6])
    with pytest.raises(ValueError, match="No sportsbook rows"):
        prepare_sportsbook_odds(books, target="Unknown")


def test_sportsdata_requires_explicit_team_side_and_handles_missing_moneylines():
    books = pd.DataFrame(
        {
            "Updated": [START, START],
            "Sportsbook": ["Book", "Book"],
            "HomeMoneyLine": [-150, None],
            "AwayMoneyLine": [130, 125],
        }
    )
    with pytest.raises(ValueError, match="sportsbook_side"):
        prepare_sportsbook_odds(books, target="CLE")
    assert prepare_sportsbook_odds(
        books, target="CLE", side="home"
    ).target_raw_prob.tolist() == [0.6]


def test_pbp_raw_adapter_handles_false_strings_and_missing_optional_fields():
    plays = pd.DataFrame(
        {
            "Created": [START, "2025-09-07T17:00:01Z"],
            "Description": ["Pass", "Touchdown"],
            "IsScoringPlay": ["False", "True"],
        }
    )
    events = prepare_pbp_events(plays)
    assert events.event_type.tolist().count("Scoring play") == 1
    assert events.event_type.tolist().count("All plays") == 2
    with pytest.raises(ValueError, match="invalid timestamps"):
        prepare_pbp_events(pd.DataFrame({"timestamp": ["not-a-date"]}))


def test_auto_loaded_pbp_adds_track_and_backward_aligned_scoring_marker(archive):
    folder = archive / "playbyplay"
    folder.mkdir()
    pd.DataFrame(
        {
            "timestamp": [
                "2025-09-07T17:00:03Z",
                "2025-09-07T17:00:02Z",
                "2025-09-08T00:00:00Z",
            ],
            "event_type": ["Scoring play", "Quarter end", "All plays"],
            "event_detail": ["Touchdown", "End Q1", "Another day"],
        }
    ).to_csv(folder / f"{EVENT}.csv", index=False)
    fig = visualize_game_odds(
        EVENT,
        source="local",
        data_dir=archive,
        start_time=START,
        show=False,
        resample_frequency=None,
    )
    assert "yaxis2" in fig.layout
    scoring = next(trace for trace in fig.data if trace.name == "Scoring play on odds")
    assert list(scoring.y) == [
        0.6
    ]  # last trade before play, never the future 1-cent trade
    assert len(fig.layout.shapes) == 1
    assert "All plays" not in [trace.name for trace in fig.data]
    assert fig.layout.xaxis2.matches == "x"
    volume = next(trace for trace in fig.data if trace.name == "Volume")
    assert volume.yaxis == "y3"
    assert fig.layout.xaxis3.matches == "x"
    assert fig.layout.xaxis3.minallowed == fig.layout.xaxis.minallowed
    assert fig.layout.xaxis3.maxallowed == fig.layout.xaxis.maxallowed
    assert fig.layout.yaxis3.title.text == "Volume (contracts)"


def test_book_seed_quote_is_carried_into_chart_window(archive):
    game = load_game_odds(EVENT, source="local", data_dir=archive, start_time=START)
    books = prepare_sportsbook_odds(
        pd.DataFrame(
            {
                "timestamp": ["2025-09-07T16:59:00Z"],
                "book": ["Book"],
                "target_team": ["CLE"],
                "implied_prob_raw": [0.55],
            }
        ),
        target="CLE",
    )
    fig = build_game_odds_figure(
        game.compiled,
        event_ticker=EVENT,
        target_label="CLE",
        sportsbook_odds=books,
        resample_frequency=None,
    )
    book = next(trace for trace in fig.data if trace.name == "Book")
    assert list(book.y) == [0.55, 0.55]
    assert pd.to_datetime(book.x[0], utc=True) == pd.Timestamp(START)


def test_cli_local_html_and_catalog(archive, capsys):
    output = archive / "chart.html"
    assert (
        visualize_game.main(
            [
                EVENT,
                "--source",
                "local",
                "--data-dir",
                str(archive),
                "--no-resample",
                "--no-show",
                "--output-html",
                str(output),
            ]
        )
        == 0
    )
    assert output.is_file()
    assert "plotly_relayout" in output.read_text(encoding="utf-8")
    assert "first stored trade; includes pregame" in capsys.readouterr().out
    assert (
        visualize_game.main(
            ["--list-games", "--data-dir", str(archive), "--search", "Cleveland"]
        )
        == 0
    )
    assert EVENT in capsys.readouterr().out


def test_notebook_is_clean_and_code_cells_compile():
    import json

    notebook = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "single_game_odds_time_series_visualizer.ipynb"
        ).read_text()
    )
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            compile("".join(cell["source"]), "notebook", "exec")


def test_notebook_picker_renders_and_reuses_catalog(archive, monkeypatch, capsys):
    pytest.importorskip("ipywidgets")
    from src.analysis.kalshi.notebook_game_picker import game_picker

    monkeypatch.chdir(archive)
    picker = game_picker(archive)
    controls = {
        child.description: child
        for child in picker.children
        if hasattr(child, "description")
    }
    controls["Find local games"].click()
    controls["Game"].value = EVENT
    controls["Outcome"].value = "Cleveland"
    controls["Sample interval"].value = ""
    controls["Visualize game"].click()
    assert list((archive / "output").glob("*.html"))
    assert "Could not" not in capsys.readouterr().out

    def no_second_scan(*args, **kwargs):
        pytest.fail("Searching again should reuse the notebook's catalog")

    monkeypatch.setattr(
        "src.analysis.kalshi.notebook_game_picker.list_local_games", no_second_scan
    )
    controls["Search games"].value = "not-a-game"
    controls["Find local games"].click()
    assert not controls["Game"].options
