from __future__ import annotations

import sys
from pathlib import Path

try:
    from simple_term_menu import TerminalMenu
except (ImportError, NotImplementedError):
    TerminalMenu = None

from src.common.analysis import Analysis
from src.common.indexer import Indexer
from src.common.util import package_data
from src.common.util.strings import snake_to_title
def analyze(name: str | None = None):
    """Run analysis by name or show interactive menu."""
    analyses = Analysis.load()

    if not analyses:
        print("No analyses found in src/analysis/")
        return

    output_dir = Path("output")

    # If name provided, run that specific analysis
    if name:
        if name == "all":
            print("\nRunning all analyses...\n")
            for analysis_cls in analyses:
                instance = analysis_cls()
                print(f"Running: {instance.name}")
                saved = instance.save(output_dir, formats=["png", "pdf", "csv", "json", "gif"])
                for fmt, path in saved.items():
                    print(f"  {fmt}: {path}")
            print("\nAll analyses complete.")
            return

        # Find matching analysis
        for analysis_cls in analyses:
            instance = analysis_cls()
            if instance.name == name:
                print(f"\nRunning: {instance.name}\n")
                saved = instance.save(output_dir, formats=["png", "pdf", "csv", "json", "gif"])
                print("Saved files:")
                for fmt, path in saved.items():
                    print(f"  {fmt}: {path}")
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

    if TerminalMenu is None:
        print("Interactive menu is unavailable in this environment; using a simple text prompt instead.")
        for index, option in enumerate(options):
            print(f"{index}: {option}")

        while True:
            try:
                raw_choice = input("Select an analysis number: ").strip()
            except EOFError:
                print("Exiting.")
                return

            if not raw_choice:
                continue

            try:
                choice = int(raw_choice)
            except ValueError:
                print("Please enter a number.")
                continue

            if choice is None or choice == len(options) - 1:
                print("Exiting.")
                return

            if choice < 0 or choice >= len(options):
                print("Invalid selection.")
                continue

            break
    else:
        menu = TerminalMenu(
            options,
            title="Select an analysis to run (use arrow keys):",
            cycle_cursor=True,
            clear_screen=False,
        )
        choice = menu.show()

        if choice is None or choice == len(options) - 1:
            print("Exiting.")
            return

    if choice == 0:
        # Run all analyses
        print("\nRunning all analyses...\n")
        for analysis_cls in analyses:
            instance = analysis_cls()
            print(f"Running: {instance.name}")
            saved = instance.save(output_dir, formats=["png", "pdf", "csv", "json", "gif"])
            for fmt, path in saved.items():
                print(f"  {fmt}: {path}")
        print("\nAll analyses complete.")
    else:
        # Run selected analysis
        analysis_cls = analyses[choice - 1]
        instance = analysis_cls()
        print(f"\nRunning: {instance.name}\n")
        saved = instance.save(output_dir, formats=["png", "pdf", "csv", "json", "gif"])
        print("Saved files:")
        for fmt, path in saved.items():
            print(f"  {fmt}: {path}")


def __main__():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    analyze(name)
    sys.exit(0)


if __name__ == "__main__":
    __main__()
