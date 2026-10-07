"""Reorganize a frozen local Kalshi trade snapshot by ticker family.

See docs/trade_reorganization.md. This command never changes collector inputs.
"""

from src.analysis.kalshi.trade_reorganization import main


if __name__ == "__main__":
    raise SystemExit(main())
