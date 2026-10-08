from __future__ import annotations

import json

import pandas as pd
import pytest

import estimate_fill
from src.analysis.kalshi.fill_probability import (
    add_normalized_time,
    build_fill_dataset,
    estimate_fill_probability,
    normalize_family,
    normalize_timings,
    normalize_trade_prices,
)
from src.analysis.kalshi.fill_probability_data import (
    _ticker_frames,
    fetch_family_timings,
    prepare_fill_data,
)


FAMILY = "KXATPCHALLENGERMATCH"
EVENT = FAMILY + "-26APR09AB"
TICKER = EVENT + "-A"
START = pd.Timestamp("2026-04-09T12:00:00Z")


def timings():
    return pd.DataFrame(
        [
            dict(
                ticker=TICKER,
                event_ticker=EVENT,
                start_time=START,
                close_time=START + pd.Timedelta(seconds=10),
                settlement_time=START + pd.Timedelta(seconds=12),
                status="finalized",
            )
        ]
    )


def trades():
    return pd.DataFrame(
        [
            dict(
                trade_id=str(i),
                ticker=TICKER,
                count=1,
                created_time=START + pd.Timedelta(seconds=second),
                yes_price=price,
                no_price=100 - price,
                taker_side="yes",
            )
            for i, (second, price) in enumerate(
                [(-1, 60), (0, 70), (5, 50), (8, 49), (10, 0), (12, 0)]
            )
        ]
    )


def dataset(frame=None, metadata=None, **options):
    return build_fill_dataset(
        trades() if frame is None else frame,
        timings() if metadata is None else metadata,
        family=FAMILY,
        coverage_end=START + pd.Timedelta(hours=1),
        time_step=0.25,
        **options,
    )


def query(frame, **kwargs):
    options = dict(
        family=FAMILY,
        clv=0.6,
        current_price=0.7,
        normalized_time=0.0,
        bid_price=0.49,
        clv_tolerance=0,
        price_tolerance=0,
        time_tolerance=0,
        min_events=1,
    )
    options.update(kwargs)
    return estimate_fill_probability(frame, **options)


def test_normalized_time_and_asof_states_exclude_close_and_payouts():
    result = dataset()
    assert result.audit.status.tolist() == ["included"]
    assert result.snapshots.normalized_time.tolist() == [0, 0.25, 0.5, 0.75]
    assert result.snapshots.clv.tolist() == [0.6] * 4
    assert result.snapshots.current_price.tolist() == [0.7, 0.7, 0.5, 0.49]
    assert result.snapshots.future_trade_count.tolist() == [2, 2, 1, 0]
    assert result.snapshots.future_min_cents.iloc[:3].tolist() == [49, 49, 49]
    assert pd.isna(result.snapshots.future_min_cents.iloc[3])
    normalized = add_normalized_time(
        normalize_trade_prices(trades()), normalize_timings(timings(), FAMILY)
    )
    assert normalized.normalized_time.iloc[0] == pytest.approx(-1 / 12)
    assert normalized.normalized_time.iloc[-1] == 1
    assert not normalized.in_game.iloc[-1]


def test_touch_and_trade_through_are_distinct_and_monotone():
    snapshots = dataset().snapshots
    touch = query(snapshots, bid_price=0.49)
    through = query(snapshots, bid_price=0.50)
    no_fill = query(snapshots, bid_price=0.48)
    assert (
        touch["p_conservative"],
        touch["p_optimistic"],
        touch["fill_probability_gap"],
    ) == (0, 1, 1)
    assert (
        through["p_conservative"],
        through["p_optimistic"],
        through["fill_probability_gap"],
    ) == (1, 1, 0)
    assert (
        no_fill["p_conservative"],
        no_fill["p_optimistic"],
        no_fill["fill_probability_gap"],
    ) == (0, 0, 0)


def test_equal_timestamp_prints_are_not_future_fills():
    frame = trades()
    frame.loc[frame.created_time.eq(START), ["yes_price", "no_price"]] = [1, 99]
    result = dataset(frame).snapshots
    assert result.future_min_cents.iloc[0] == 49


def test_cent_equality_is_not_broken_by_float_conversion():
    snapshots = dataset().snapshots
    snapshots["future_min_cents"] = 29
    result = query(snapshots, bid_price=0.29)
    assert result["p_conservative"] == 0
    assert result["p_optimistic"] == 1


@pytest.mark.parametrize(
    "change,status",
    [
        ({"start_time": None}, "missing_timing"),
        ({"settlement_time": None}, "missing_timing"),
        ({"settlement_time": START}, "invalid_timing"),
        ({"close_time": START}, "invalid_timing"),
        ({"settlement_time": START + pd.Timedelta(days=1)}, "incomplete_trade_horizon"),
        ({"status": "active"}, "not_settled"),
    ],
)
def test_missing_or_censored_events_are_audited_not_counted_as_failures(change, status):
    metadata = timings().assign(**change)
    result = dataset(metadata=metadata)
    assert result.snapshots.empty
    assert result.audit.status.tolist() == [status]


def test_pregame_missing_or_stale_prices_are_audited():
    assert dataset(trades().iloc[1:]).audit.status.tolist() == ["no_pregame_price"]
    assert dataset(max_clv_age_minutes=0).audit.status.tolist() == [
        "stale_pregame_price"
    ]


def test_no_future_prints_is_no_fill_and_settlement_has_no_remaining_horizon():
    snapshots = dataset().snapshots
    result = query(snapshots, current_price=0.49, normalized_time=0.75, bid_price=0.40)
    assert result["p_optimistic"] == result["p_conservative"] == 0
    ended = query(snapshots, normalized_time=1, time_tolerance=1)
    assert ended["status"] == "horizon_ended"
    assert ended["p_optimistic"] == 0


def test_conditional_pooling_weights_events_equally_and_excludes_whole_query_event():
    seed = dataset().snapshots.iloc[[0]].copy()
    seed["future_min_cents"] = 49
    many = pd.concat([seed] * 5, ignore_index=True)
    other = seed.assign(
        ticker=FAMILY + "-SECOND-B",
        event_ticker=FAMILY + "-SECOND",
        future_min_cents=60,
    )
    snapshots = pd.concat([many, other], ignore_index=True)
    result = query(snapshots)
    assert result["p_optimistic"] == 0.5
    assert result["p_conservative"] == 0
    assert result["n_events"] == 2
    excluded = query(snapshots, ticker=TICKER)
    assert excluded["n_events"] == 1
    assert excluded["p_optimistic"] == 0


def test_family_boundary_support_threshold_and_chronological_cutoff():
    snapshots = dataset().snapshots
    extra = snapshots.assign(
        event_ticker=FAMILY + "EXTRA-EVENT", ticker=FAMILY + "EXTRA-EVENT-A"
    )
    result = query(pd.concat([snapshots, extra]), min_events=2)
    assert result["n_events"] == 1
    assert result["status"] == "insufficient_support"
    assert result["p_conservative"] is None
    assert query(snapshots, as_of=START)["n_events"] == 0
    assert normalize_family(FAMILY + "-*") == FAMILY
    with pytest.raises(ValueError):
        normalize_family(FAMILY + "-%")


@pytest.mark.parametrize(
    "options",
    [
        {"bid_price": 0.7},
        {"bid_price": 0.8},
        {"clv": 60},
        {"normalized_time": -1},
        {"normalized_time": float("nan")},
    ],
)
def test_invalid_queries_are_rejected(options):
    with pytest.raises(ValueError):
        query(dataset().snapshots, **options)


def test_metadata_uses_real_settlement_and_prefers_primary_milestone():
    class Http:
        def get(self, endpoint, *, params):
            if endpoint == "/markets":
                return {"markets": []}
            if endpoint == "/historical/markets":
                return {
                    "markets": [
                        dict(
                            ticker=TICKER,
                            close_time=(START + pd.Timedelta(seconds=10)).isoformat(),
                            settlement_ts=(
                                START + pd.Timedelta(seconds=12)
                            ).isoformat(),
                            status="finalized",
                        )
                    ]
                }
            return {
                "milestones": [
                    dict(
                        start_date=(START - pd.Timedelta(hours=2)).isoformat(),
                        related_event_tickers=[EVENT],
                    )
                ]
            }

    class Client:
        http = Http()

        def get_event_milestones(self, event):
            return [
                dict(
                    type="tennis",
                    start_date=START.isoformat(),
                    primary_event_tickers=[event],
                    end_date=(START + pd.Timedelta(days=9)).isoformat(),
                )
            ]

    metadata = fetch_family_timings(
        timings()[["ticker", "event_ticker"]],
        family=FAMILY,
        client=Client(),
        progress=lambda _: None,
    )
    assert metadata.start_time.iloc[0] == START
    assert metadata.settlement_time.iloc[0] == START + pd.Timedelta(seconds=12)
    assert metadata.close_time.iloc[0] == START + pd.Timedelta(seconds=10)


def test_offline_prepare_and_query_end_to_end(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "kalshi" / "markets").mkdir(parents=True)
    dataset_dir = root / "kalshi" / "trades_by_series"
    family_dir = dataset_dir / f"series_ticker={FAMILY}"
    family_dir.mkdir(parents=True)
    metadata_dir = dataset_dir / "_metadata"
    metadata_dir.mkdir()
    (metadata_dir / "dataset.json").write_text(json.dumps({
        "run_id": "fixture", "validation_status": "passed",
        "partition_key": "series_ticker",
    }))
    pd.DataFrame({"family": [FAMILY], "row_count": [6]}).to_parquet(
        metadata_dir / "family_catalog.parquet", index=False
    )
    (metadata_dir / "scoped_checkpoint.json").write_text(json.dumps({
        "base_run_id": "fixture",
        "markets": {TICKER: {"family": FAMILY, "coverage": [[
            int((START - pd.Timedelta(hours=1)).timestamp()),
            int((START + pd.Timedelta(hours=1)).timestamp()),
        ]]}},
    }))
    timings()[["ticker", "event_ticker"]].to_parquet(
        root / "kalshi" / "markets" / "markets_0_10000.parquet"
    )
    frame = trades()
    # A later print in this family establishes the observed family horizon.
    unrelated = frame.iloc[[-1]].assign(
        ticker=EVENT + "-B",
        trade_id="other",
        created_time=START + pd.Timedelta(hours=1),
    )
    pd.concat([frame, unrelated]).to_parquet(family_dir / "part-0.parquet")
    frame.iloc[[1]].to_parquet(family_dir / "scoped_0.parquet")
    other_family = dataset_dir / "series_ticker=KXOTHER"
    other_family.mkdir()
    unrelated.assign(ticker="KXOTHER-EVENT-A", trade_id="other-family").to_parquet(other_family / "part-0.parquet")
    timing_file = tmp_path / "times.csv"
    timings().to_csv(timing_file, index=False)
    output = tmp_path / "output"
    folder = prepare_fill_data(
        family=FAMILY,
        data_dir=root,
        output_dir=output,
        timings_path=timing_file,
        time_step=0.25,
        progress=lambda _: None,
    )
    assert json.loads((folder / "manifest.json").read_text())["n_snapshots"] == 4
    assert pd.read_parquet(
        folder / "snapshots.parquet"
    ).future_trade_count.tolist() == [2, 2, 1, 0]
    assert pd.read_csv(folder / "ticker_summary.csv").clv.tolist() == [0.6]
    assert pd.read_parquet(
        folder / "normalized_trades.parquet"
    ).ticker.unique().tolist() == [TICKER]
    assert (
        estimate_fill.main(
            [
                "query",
                "--family",
                FAMILY,
                "--output-dir",
                str(output),
                "--clv",
                "0.6",
                "--x",
                "0.7",
                "--t",
                "0",
                "--y",
                "0.49",
                "--min-events",
                "1",
            ]
        )
        == 0
    )
    assert (
        json.loads((folder / "last_estimate.json").read_text())["fill_probability_gap"]
        == 1
    )
    modified = (folder / "normalized_trades.parquet").stat().st_mtime_ns

    def no_rescan(*args, **kwargs):
        pytest.fail("A cached rebuild must not rescan the archive")

    monkeypatch.setattr(
        "src.analysis.kalshi.fill_probability_data.extract_family_trades", no_rescan
    )
    prepare_fill_data(
        family=FAMILY,
        data_dir=root,
        output_dir=output,
        time_step=0.5,
        progress=lambda _: None,
    )
    assert (folder / "normalized_trades.parquet").stat().st_mtime_ns == modified
    assert len(pd.read_parquet(folder / "snapshots.parquet")) == 2

    # Coverage changes without a Parquet change still invalidate preparation.
    checkpoint_path = dataset_dir / "_metadata" / "scoped_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["markets"][TICKER]["coverage"] = []
    checkpoint_path.write_text(json.dumps(checkpoint))
    prepare_fill_data(
        family=FAMILY, data_dir=root, output_dir=output,
        timings_path=timing_file, time_step=0.25, progress=lambda _: None,
    )
    assert pd.read_csv(folder / "ticker_audit.csv").status.tolist() == ["incomplete_trade_horizon"]
    assert json.loads((folder / "manifest.json").read_text())["n_snapshots"] == 0
    checkpoint["markets"][TICKER]["coverage"] = [[
        int((START - pd.Timedelta(hours=1)).timestamp()),
        int((START + pd.Timedelta(hours=2)).timestamp()),
    ]]
    checkpoint_path.write_text(json.dumps(checkpoint))
    prepare_fill_data(
        family=FAMILY, data_dir=root, output_dir=output,
        timings_path=timing_file, time_step=0.25, progress=lambda _: None,
    )
    assert json.loads((folder / "manifest.json").read_text())["n_snapshots"] == 4


def test_streaming_keeps_tickers_whole_across_batch_boundaries(tmp_path):
    frame = pd.concat(
        [trades(), trades().assign(ticker=EVENT + "-B")], ignore_index=True
    )
    path = tmp_path / "ordered.parquet"
    frame.to_parquet(path, index=False)
    streamed = list(_ticker_frames(path, batch_size=4))
    assert [ticker for ticker, _ in streamed] == [TICKER, EVENT + "-B"]
    assert [len(group) for _, group in streamed] == [6, 6]


def test_menu_accepts_state_inputs_without_parameters(monkeypatch):
    answers = iter(["2", "", TICKER, "60", "0.6", "0.7", "0.5", "0.4"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    args = estimate_fill.menu_args()
    assert args.family == FAMILY
    assert (args.clv, args.x, args.t, args.y) == (0.6, 0.7, 0.5, 0.4)
