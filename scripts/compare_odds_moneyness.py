"""Offline odds-moneyness validation and a versioned model artifact; no API calls.

python -m scripts.compare_odds_moneyness --family KXNFLGAME
"""

import argparse
import json
from pathlib import Path

import pandas as pd

from src.analysis.kalshi.fill_surface import reference_data
from src.analysis.kalshi.odds_moneyness import FEATURES, MODEL_VERSION, fit_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True)
    parser.add_argument(
        "--data-root", type=Path, default=Path("output/fill_probability")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--outcome", choices=["optimistic", "conservative"], default="optimistic"
    )
    parser.add_argument("--exclude-ticker")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    path = args.data_root / args.family / "snapshots.parquet"
    source = reference_data(
        pd.read_parquet(path), args.family, args.exclude_ticker, args.as_of
    )
    model = fit_model(source, args.outcome)
    result = dict(
        model_version=MODEL_VERSION,
        feature_order=FEATURES,
        family=args.family,
        outcome=args.outcome,
        source=str(path),
        source_mtime_ns=path.stat().st_mtime_ns,
        excluded_ticker=args.exclude_ticker,
        as_of=args.as_of,
        bounds=model.bounds,
        strengths=model.strengths,
        coefficients=model.weights.tolist(),
        audit=model.audit,
        validation=model.validation,
    )
    output = args.output or args.data_root / args.family / "odds_moneyness_model.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    diagnostics = model.validation.get("diagnostics", {})
    summary = {
        "family": args.family,
        "artifact": str(output),
        "validation": model.validation["status"],
        "strengths": model.strengths,
        "test_metrics": {
            key: value
            for key, value in model.validation.get("test_metrics", {}).items()
            if key != "calibration"
        },
        "price_band_residuals": [
            {key: value for key, value in row.items() if key != "calibration"}
            for row in diagnostics.get("price_bands", [])
        ],
        "within_bin_price_effect": diagnostics.get("within_distance_time_price_effect"),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
