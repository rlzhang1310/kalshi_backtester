from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import analyze as analyze_cli


def _install_fake_sports_markets(monkeypatch: pytest.MonkeyPatch) -> None:
    sports_markets = ModuleType("src.analysis.kalshi.util.sports_markets")
    sports_markets.SPORT_LABELS = {
        "nfl": "NFL",
        "nba": "NBA",
    }
    sports_markets.MARKET_CATEGORY_LABELS = {
        "all": "All",
        "moneyline": "Moneyline",
        "spread": "Spread",
    }
    monkeypatch.setitem(
        sys.modules,
        "src.analysis.kalshi.util.sports_markets",
        sports_markets,
    )


def test_numbered_fallback_selects_sport_then_category(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_sports_markets(monkeypatch)
    monkeypatch.setattr(analyze_cli, "TerminalMenu", None)
    answers = iter(["1", "0"])
    prompts: list[str] = []

    def choose(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", choose)

    selected = analyze_cli._prompt_for_sports_filters(None, None)

    assert selected == ("nba", "all")
    assert prompts == [
        "Select a sport number: ",
        "Select a market category number: ",
    ]
    output = capsys.readouterr().out
    assert output.index("Select a sport:") < output.index("Select a market category:")
    assert "0: NFL" in output
    assert "1: NBA" in output
    assert "0: All" in output
    assert "1: Moneyline" in output
    assert "2: Spread" in output


def test_direct_filters_skip_prompts_and_use_selection_specific_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeWinRateAnalysis:
        instances: list[FakeWinRateAnalysis] = []

        def __init__(self) -> None:
            self.name = "win_rate_by_price"
            self.description = "Win rate by price"
            self.configured_with: tuple[str, str] | None = None
            self.save_call: tuple[Path, list[str]] | None = None
            self.__class__.instances.append(self)

        def configure_filters(self, sport: str, category: str) -> None:
            self.configured_with = (sport, category)
            self.name = f"win_rate_by_price_{sport}_{category}"

        def save(self, output_dir: Path, formats: list[str]) -> dict[str, Path]:
            self.save_call = (output_dir, formats)
            return {"csv": output_dir / f"{self.name}.csv"}

    monkeypatch.setattr(
        analyze_cli.Analysis,
        "load",
        classmethod(lambda cls: [FakeWinRateAnalysis]),
    )

    def unexpected_prompt(*args: object, **kwargs: object) -> int:
        raise AssertionError(f"unexpected prompt: {args!r} {kwargs!r}")

    monkeypatch.setattr(analyze_cli, "_select_option", unexpected_prompt)

    result = analyze_cli.main(["--sport", "nfl", "--category", "spread"])

    assert result == 0
    assert len(FakeWinRateAnalysis.instances) == 1
    instance = FakeWinRateAnalysis.instances[0]
    assert instance.configured_with == ("nfl", "spread")
    assert instance.name == "win_rate_by_price_nfl_spread"
    assert instance.save_call == (Path("output"), analyze_cli.OUTPUT_FORMATS)
    output = capsys.readouterr().out
    assert "Running: win_rate_by_price_nfl_spread" in output
    expected_csv = Path("output") / "win_rate_by_price_nfl_spread.csv"
    assert str(expected_csv) in output


def test_filter_pairing_is_rejected_when_prompting_is_disabled() -> None:
    class FakeFilterableAnalysis:
        def configure_filters(self, sport: str, category: str) -> None:
            raise AssertionError("incomplete filters must not be configured")

    with pytest.raises(
        ValueError,
        match="--sport and --category must be provided together",
    ):
        analyze_cli._configure_analysis(
            FakeFilterableAnalysis(),
            sport="nfl",
            market_category=None,
            prompt_for_missing=False,
        )


def test_invalid_filter_value_returns_cli_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RejectingWinRateAnalysis:
        name = "win_rate_by_price"
        description = "Win rate by price"

        def configure_filters(self, sport: str, category: str) -> None:
            raise ValueError(f"Unsupported sport: {sport}")

        def save(self, output_dir: Path, formats: list[str]) -> dict[str, Path]:
            raise AssertionError("invalid filters must not run the analysis")

    monkeypatch.setattr(
        analyze_cli.Analysis,
        "load",
        classmethod(lambda cls: [RejectingWinRateAnalysis]),
    )
    monkeypatch.setattr(
        analyze_cli,
        "_select_option",
        lambda *args, **kwargs: pytest.fail("complete filters must not prompt"),
    )

    result = analyze_cli.main(
        ["win_rate_by_price", "--sport", "quidditch", "--category", "spread"]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "Error: Unsupported sport: quidditch" in captured.err


def test_cancelling_filter_selection_does_not_run_analysis(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_sports_markets(monkeypatch)

    class FakeWinRateAnalysis:
        name = "win_rate_by_price"
        description = "Win rate by price"

        def configure_filters(self, sport: str, category: str) -> None:
            raise AssertionError("cancelled filters must not be configured")

        def save(self, output_dir: Path, formats: list[str]) -> dict[str, Path]:
            raise AssertionError("cancelled analysis must not be saved")

    monkeypatch.setattr(
        analyze_cli.Analysis,
        "load",
        classmethod(lambda cls: [FakeWinRateAnalysis]),
    )
    prompt_titles: list[str] = []

    def cancel(options: list[str], title: str, prompt: str) -> int:
        del prompt
        prompt_titles.append(title)
        assert options[-1] == "[Cancel]"
        return len(options) - 1

    monkeypatch.setattr(analyze_cli, "_select_option", cancel)

    result = analyze_cli.main(["win_rate_by_price"])

    assert result == 0
    assert prompt_titles == ["Select a sport:"]
    assert "Cancelled." in capsys.readouterr().out


def test_positional_all_runs_all_analyses_without_filter_prompts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    saved_names: list[str] = []

    class FakeWinRateAnalysis:
        name = "win_rate_by_price"
        description = "Win rate by price"

        def configure_filters(self, sport: str, category: str) -> None:
            raise AssertionError("all must not configure sports filters")

        def save(self, output_dir: Path, formats: list[str]) -> dict[str, Path]:
            saved_names.append(self.name)
            return {}

    class FakeOtherAnalysis:
        name = "other_analysis"
        description = "Another analysis"

        def save(self, output_dir: Path, formats: list[str]) -> dict[str, Path]:
            saved_names.append(self.name)
            return {}

    monkeypatch.setattr(
        analyze_cli.Analysis,
        "load",
        classmethod(lambda cls: [FakeWinRateAnalysis, FakeOtherAnalysis]),
    )
    monkeypatch.setattr(
        analyze_cli,
        "_select_option",
        lambda *args, **kwargs: pytest.fail("'all' must not open a menu"),
    )
    monkeypatch.setattr(
        analyze_cli,
        "_prompt_for_sports_filters",
        lambda *args, **kwargs: pytest.fail("'all' must not prompt for filters"),
    )

    result = analyze_cli.main(["all"])

    assert result == 0
    assert saved_names == ["win_rate_by_price", "other_analysis"]
    assert "All analyses complete." in capsys.readouterr().out
