from __future__ import annotations

from src.common.indexer import Indexer
from src.common.util.strings import snake_to_title


def select_indexer(indexers: list[type[Indexer]]) -> type[Indexer] | None:
    """Prompt the user to select an indexer using a numbered menu."""
    print("Select an indexer to run:")
    for number, indexer_cls in enumerate(indexers, start=1):
        instance = indexer_cls()
        print(
            f"  {number}. "
            f"{snake_to_title(instance.name)}: {instance.description}"
        )
    print("  0. Exit")

    while True:
        try:
            response = input("\nEnter a number: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return None

        try:
            choice = int(response)
        except ValueError:
            print("Please enter one of the numbers shown above.")
            continue

        if choice == 0:
            return None
        if 1 <= choice <= len(indexers):
            return indexers[choice - 1]

        print(f"Please enter a number from 0 to {len(indexers)}.")


def __main__():
    """Interactive indexer selection menu."""
    indexers = Indexer.load()

    if not indexers:
        print("No indexers found in src/indexers/")
        return

    indexer_cls = select_indexer(indexers)
    if indexer_cls is None:
        print("Exiting.")
        return

    instance = indexer_cls()
    print(f"\nRunning: {instance.name}\n")
    instance.run()
    print("\nIndexer complete.")


if __name__ == "__main__":
    __main__()
