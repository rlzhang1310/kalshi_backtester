"""Offline, common-state whole-game holdout comparison; no API calls.

Run: python -m scripts.compare_no_clv_fill --family KXNBAGAME
Uses the same positive-bid labels and weighting for both independent models.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.analysis.kalshi import fill_surface as clv
from src.analysis.kalshi import no_clv_fill as pooled


def comparison(source, outcome="optimistic"):
    train, test = pooled.whole_game_split(source)
    no_clv_model = pooled.fit_model(source, outcome)
    clv_model = clv.fit_surface(source, outcome)
    if (
        no_clv_model.validation["status"] != "whole-game holdout"
        or clv_model.validation["status"] != "whole-game holdout"
    ):
        raise ValueError("Not enough whole games for a comparable holdout.")
    pooled_weights, pooled_converged = pooled.fit_weights(
        pooled.empirical_bins(train, outcome), no_clv_model.strengths
    )
    clv_weights, clv_converged = clv.fit_weights(
        clv.empirical_bins(train, outcome),
        clv_model.strength,
        clv_model.boundary,
        clv_model.coordinate,
    )
    rows = []
    for bid in clv.RAW_BIDS[1:-1]:
        states = test.loc[
            (test.current_price > bid) & (test.normalized_time < 1)
        ].copy()
        states["bid"] = float(bid)
        threshold = round(bid * 100, 8)
        states["hit"] = (
            states.future_min_cents.le(threshold)
            if outcome == "optimistic"
            else states.future_min_cents.lt(threshold)
        )
        # Each event/bid has unit total weight, regardless of its timestamp count.
        states["weight"] = 1 / states.groupby("event_ticker").event_ticker.transform(
            "size"
        )
        rows.append(states)
    scored = pd.concat(rows, ignore_index=True)
    observed, weight = scored.hit.to_numpy(dtype=float), scored.weight.to_numpy()
    estimates = {
        "clv": clv.predict(
            clv_weights,
            scored[["clv", *pooled.FEATURES]].to_numpy(),
            clv_model.boundary,
            clv_model.coordinate,
        ),
        "no_clv": pooled.predict(pooled_weights, scored[pooled.FEATURES].to_numpy()),
    }

    def metrics(predicted, mask):
        if not mask.any():
            return None
        result = pooled.score_predictions(observed[mask], predicted[mask], weight[mask])
        # Weight is a repeated event/bid contribution, not a count of trials.
        result.pop("trial_weight")
        result.update(
            n_state_bid_labels=int(mask.sum()),
            event_bid_weight=float(weight[mask].sum()),
        )
        return result

    results = {}
    for name, predicted in estimates.items():
        result = metrics(predicted, np.ones(len(scored), dtype=bool))
        result["large_price_clv_gap"] = metrics(
            predicted,
            abs(scored.current_price.to_numpy() - scored.clv.to_numpy()) >= 0.2,
        )
        for field, values in [
            ("price_buckets", scored.current_price.to_numpy()),
            ("time_buckets", scored.normalized_time.to_numpy()),
            ("calibration", predicted),
        ]:
            result[field] = []
            buckets = np.minimum((values * 10).astype(int), 9)
            for bucket in range(10):
                mask = buckets == bucket
                if mask.any():
                    result[field].append(
                        dict(
                            lower=bucket / 10,
                            upper=(bucket + 1) / 10,
                            predicted=float(
                                np.average(predicted[mask], weights=weight[mask])
                            ),
                            observed=float(
                                np.average(observed[mask], weights=weight[mask])
                            ),
                            **metrics(predicted, mask),
                        )
                    )
        results[name] = result
    return dict(
        outcome=outcome,
        train_events=int(train.event_ticker.nunique()),
        holdout_events=int(test.event_ticker.nunique()),
        scoring="Identical observation-level positive-bid price-reaching labels; equal weight per event/bid. No synthetic boundary labels or support masking.",
        limitation="Selection holdout, not an independent final test. Models differ in both CLV conditioning and boundary/fit weighting, so this does not isolate CLV's causal contribution.",
        clv_config=dict(
            version=clv.MODEL_VERSION,
            boundary=clv_model.boundary,
            coordinate=clv_model.coordinate,
            strength=clv_model.strength,
            optimizer_converged=clv_converged,
        ),
        no_clv_config=dict(
            version=pooled.MODEL_VERSION,
            strengths=no_clv_model.strengths,
            optimizer_converged=pooled_converged,
            boundary_conflict_bins=no_clv_model.validation["boundary_conflict_bins"],
        ),
        metrics=results,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", default="KXNBAGAME")
    parser.add_argument(
        "--results-dir", type=Path, default=Path("output/fill_probability")
    )
    parser.add_argument(
        "--outcome", choices=["optimistic", "conservative"], default="optimistic"
    )
    args = parser.parse_args()
    folder = args.results_dir / args.family
    source = pd.read_parquet(folder / "snapshots.parquet")
    report = comparison(source, args.outcome)
    target = folder / f"no_clv_comparison_{args.outcome}.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                report=str(target),
                train_events=report["train_events"],
                holdout_events=report["holdout_events"],
                no_clv_config=report["no_clv_config"],
                metrics={
                    name: {key: values[key] for key in ("brier", "log_loss")}
                    for name, values in report["metrics"].items()
                },
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
