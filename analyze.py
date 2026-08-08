from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from simple_term_menu import TerminalMenu
except (ImportError, NotImplementedError):
    TerminalMenu = None

from src.common.analysis import Analysis
from src.common.util.strings import snake_to_title


FILTERABLE_WIN_RATE_ANALYSIS = "win_rate_by_price"
OUTPUT_FORMATS = ["png", "pdf", "csv", "json", "gif"]


def _select_option(options: list[str], title: str, prompt: str) -> int | None:
    """Return a terminal-menu selection, with a numbered prompt fallback."""
    if TerminalMenu is not None:
        menu = TerminalMenu(
            options,
            title=title,
            cycle_cursor=True,
            clear_screen=False,
        )
        return menu.show()

    print(f"\n{title}")
    for index, option in enumerate(options):
        print(f"{index}: {option}")

    while True:
        try:
            raw_choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return None

        if not raw_choice:
            continue

        try:
            choice = int(raw_choice)
        except ValueError:
            print("Please enter a number.")
            continue

        if 0 <= choice < len(options):
            return choice

        print("Invalid selection.")


def _prompt_for_sports_filters(
    sport: str | None,
    market_category: str | None,
) -> tuple[str, str] | None:
    """Prompt for any missing sports win-rate filters, sport first."""
    from src.analysis.kalshi.util.sports_markets import (
        MARKET_CATEGORY_LABELS,
        SPORT_LABELS,
    )

    if sport is None:
        sport_slugs = list(SPORT_LABELS)
        sport_options = [SPORT_LABELS[slug] for slug in sport_slugs] + ["[Cancel]"]
        choice = _select_option(
            sport_options,
            title="Select a sport:",
            prompt="Select a sport number: ",
        )
        if choice is None or choice == len(sport_slugs):
            return None
        sport = sport_slugs[choice]

    if market_category is None:
        category_slugs = list(MARKET_CATEGORY_LABELS)
        category_options = [MARKET_CATEGORY_LABELS[slug] for slug in category_slugs] + [
            "[Cancel]"
        ]
        choice = _select_option(
            category_options,
            title="Select a market category:",
            prompt="Select a market category number: ",
        )
        if choice is None or choice == len(category_slugs):
            return None
        market_category = category_slugs[choice]

    return sport, market_category


def _configure_analysis(
    instance: Analysis,
    sport: str | None,
    market_category: str | None,
    *,
    prompt_for_missing: bool,
) -> bool:
    """Apply sports filters when supported; return False when cancelled."""
    configure_filters = getattr(instance, "configure_filters", None)

    if configure_filters is None:
        if sport is not None or market_category is not None:
            raise ValueError(
                "--sport and --category can only be used with "
                f"'{FILTERABLE_WIN_RATE_ANALYSIS}'"
            )
        return True

    if prompt_for_missing and (sport is None or market_category is None):
        selected = _prompt_for_sports_filters(sport, market_category)
        if selected is None:
            print("Cancelled.")
            return False
        sport, market_category = selected

    if (sport is None) != (market_category is None):
        raise ValueError("--sport and --category must be provided together")

    if sport is not None and market_category is not None:
        configure_filters(sport, market_category)

    return True


def _run_analysis(instance: Analysis, output_dir: Path) -> None:
    print(f"\nRunning: {instance.name}\n")
    saved = instance.save(output_dir, formats=OUTPUT_FORMATS)
    print("Saved files:")
    for fmt, path in saved.items():
        print(f"  {fmt}: {path}")


def analyze(
    name: str | None = None,
    *,
    sport: str | None = None,
    market_category: str | None = None,
) -> None:
    """Run analysis by name or show interactive menu."""
    analyses = Analysis.load()

    if not analyses:
        print("No analyses found in src/analysis/")
        return

    output_dir = Path("output")

    # If name provided, run that specific analysis
    if name:
        if name == "all":
            if sport is not None or market_category is not None:
                raise ValueError("Sports filters cannot be combined with 'all'")
            print("\nRunning all analyses...\n")
            for analysis_cls in analyses:
                instance = analysis_cls()
                print(f"Running: {instance.name}")
                saved = instance.save(output_dir, formats=OUTPUT_FORMATS)
                for fmt, path in saved.items():
                    print(f"  {fmt}: {path}")
            print("\nAll analyses complete.")
            return

        # Find matching analysis
        for analysis_cls in analyses:
            instance = analysis_cls()
            if instance.name == name:
                should_prompt = name == FILTERABLE_WIN_RATE_ANALYSIS
                if not _configure_analysis(
                    instance,
                    sport,
                    market_category,
                    prompt_for_missing=should_prompt,
                ):
                    return
                _run_analysis(instance, output_dir)
                return

        # No match found
        print(f"Analysis '{name}' not found. Available analyses:")
        for analysis_cls in analyses:
            instance = analysis_cls()
            print(f"  - {instance.name}")
        sys.exit(1)

    # Interactive menu mode
    options = ["[All] Run all analyses"]
    for analysis_cls in analyses:
        instance = analysis_cls()
        options.append(f"{snake_to_title(instance.name)}: {instance.description}")
    options.append("[Exit]")

    choice = _select_option(
        options,
        title="Select an analysis to run:",
        prompt="Select an analysis number: ",
    )
    if choice is None or choice == len(options) - 1:
        print("Exiting.")
        return

    if choice == 0:
        # Run all analyses
        print("\nRunning all analyses...\n")
        for analysis_cls in analyses:
            instance = analysis_cls()
            print(f"Running: {instance.name}")
            saved = instance.save(output_dir, formats=OUTPUT_FORMATS)
            for fmt, path in saved.items():
                print(f"  {fmt}: {path}")
        print("\nAll analyses complete.")
    else:
        # Run selected analysis
        analysis_cls = analyses[choice - 1]
        instance = analysis_cls()
        if not _configure_analysis(
            instance,
            sport=None,
            market_category=None,
            prompt_for_missing=instance.name == FILTERABLE_WIN_RATE_ANALYSIS,
        ):
            return
        _run_analysis(instance, output_dir)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a stored-market analysis and save its outputs.",
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Analysis name, 'all', or omit to choose interactively",
    )
    parser.add_argument(
        "--sport",
        help=(
            "Sport filter for win_rate_by_price "
            "(for example: nfl, esports, or ncaa_football)"
        ),
    )
    parser.add_argument(
        "--category",
        dest="market_category",
        help=(
            "Market category for win_rate_by_price: all, moneyline, spread, "
            "score_total, player_props, team_props, or futures"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    name = args.name
    if name is None and (args.sport is not None or args.market_category is not None):
        name = FILTERABLE_WIN_RATE_ANALYSIS

    try:
        analyze(
            name,
            sport=args.sport,
            market_category=args.market_category,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
