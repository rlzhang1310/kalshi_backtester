# Conditional bid-fill estimates

For a YES bid at price `y < x`, estimate two historical execution proxies
within one event family, such as `KXATPCHALLENGERMATCH-*`:

```text
c = pregame YES implied probability (called CLV in this analysis)
x = last observed YES trade price at the observation time
t = (observation_time - game_start) / (settlement_time - game_start)
m = lowest strictly future YES trade price before trading closes

P_optimistic(c, x, t, y)   = P(m <= y | similar c, x, t in the same family)
P_conservative(c, x, t, y) = P(m <  y | similar c, x, t in the same family)
fill_probability_gap      = P_optimistic - P_conservative
```

The optimistic estimate counts a touch as filled. The conservative estimate
requires trading through the bid. The difference is the probability assigned
to a touch without trading through. It is **not a standalone fill chance**:
both estimates can be 100% while the gap is zero. There is no uncalibrated
midpoint presented as a third probability.

Prices are probabilities in `[0, 1]`, so 60 cents is `0.60`, not `60`.
This version models a resting YES bid that is not cancelled before market
close. It does not model a fixed shorter holding period or a NO bid.

## Run from a menu

```powershell
python estimate_fill.py
```

Option 1 prepares a ticker family. Option 2 asks for the family, optional
specific market/event ticker, CLV, current price `x`, normalized time `t`, and
bid price `y`. Preparation is needed once; later queries use the saved data.

Equivalent commands:

```powershell
python estimate_fill.py prepare --family KXATPCHALLENGERMATCH
python estimate_fill.py query --family KXATPCHALLENGERMATCH --clv 0.60 --x 0.60 --t 0.50 --y 0.55
```

Use `--ticker <event-or-market-ticker>` to exclude the entire queried match
from its reference pool, including the opposing player's market. Add
`--as-of 2026-04-01T00:00:00Z` to use only reference matches settled before
that date. For walk-forward research, supply the historical decision date;
otherwise the estimate uses all eligible archived matches.

## Data and normalization

### What data the fill models load

The local loading path is:

```text
data/kalshi/markets/*.parquet + data/kalshi/trades_by_series/<FAMILY>/*.parquet
  -> output/fill_probability/<FAMILY>/markets.parquet + trades.parquet
  -> normalized_trades.parquet -> snapshots.parquet
  -> dashboard estimates and CLV / No-CLV / Oddness visualizers
```

Preparation reads local market metadata and selects event tickers with the
exact requested family prefix followed by `-`. It reads only that family's
published `part-*.parquet` and appended `scoped_*.parquet` files, then joins
trades to the selected market/timing tickers, repairs YES prices, deduplicates
trade IDs, and sorts each ticker history. Other families do not feed the model.
An unpublished migration is rejected rather than falling back to the mixed
archive.

Not every extracted print becomes a model row. A ticker needs valid scheduled
start, actual settlement, and close times; settled/finalized status; per-market
checked coverage through settlement; trades before close; and a pregame trade for CLV.
An optional maximum CLV age can exclude more tickers. The preparation audit
records these exclusions in `ticker_audit.csv` and `ticker_summary.csv`.
Eligible ticker histories become states on the configured normalized-time grid,
default `0, 0.05, ..., 0.95`, only where a price has already traded and the
market is still open. All trades before close can inform each state's current
price or its strictly future minimum; they do not each become a separate state.
States with no later trade remain in the data and count as misses.

The dashboard and query command read the saved `snapshots.parquet`. The
empirical estimator narrows those states to the scenario's CLV, price, and
time tolerances. The fitted visualizers use the eligible family states to
construct their own below-current-price bid examples; the No-CLV model omits
CLV as an input, and Oddness uses positive cent bids and finite price inputs.
Selecting a ticker excludes its **entire event** from the reference pool;
an optional settlement cutoff excludes events settled on or after that time.
The dashboard does not pool different prepared families into one model.

**Cache freshness:** Dashboards read saved observations. Preparation compares
the selected family's file list and per-market checked ranges with the source
signatures in `trade_coverage.json`. New files refresh the trade and timing
caches; coverage-only changes rebuild observations from cached normalized
trades. Run `prepare --refresh` after market metadata changes that do not add
trade files or checked ranges. `manifest.json` reports the latest observed
trade time, which is not proof of complete history.

The preparation pipeline filters **exact family membership** at the event
and market ticker boundary. A family ending in `MATCH` does not also match
`MATCHTOTAL` or `MATCHEXTRA`. It reads all matching markets from
`data/kalshi/markets/`, then reads only the matching published family partition.
Extraction uses a bounded-memory DuckDB Parquet copy. Normalization uses a disk-backed
sort with a 2 GB DuckDB memory limit, followed by one ticker history at a
time, so the entire family's prints need not fit in memory.

Public metadata comes from both Kalshi's regular and historical market feeds.
Scheduled game starts come from linked sports milestones. Settlement comes
from `settlement_ts`; trading ends at `close_time`. These are distinct fields:
the [market lifecycle documentation](https://docs.kalshi.com/getting_started/market_lifecycle)
describes trading closure separately from finalized settlement. Expected
expiration, milestone end time, first trade, and last trade are never silently
substituted for actual settlement.

Normalized time is zero at scheduled game start and one at settlement. A
trade before start has `t < 0`; this is retained in the normalized trade
export. Trading can close before `t = 1`. Such post-close times are excluded
from sampled decision states, and an order at `t = 1` has no remaining fill
opportunity.

**This clock is retrospective.** Realized settlement is unknown during a
live game. To deploy an estimator live, first replace it with a duration
forecast or another clock observable at the decision time, then rebuild and
validate the estimator on that same clock. A chronological reference cutoff
alone does not remove this timing issue.

CLV is the last observed YES price strictly before the scheduled start,
separately for each child ticker. It is a market-implied probability proxy,
not a known win probability or a closing-line-value return calculation.
`clv_time` and `clv_age_seconds` expose stale pregame prices; use
`prepare --max-clv-age-minutes 60` to exclude prices more than an hour old.
Missing pregame prices are excluded, not backfilled from in-game trades.

Stored prices are cents. The existing backtester's authoritative taker-side
repair is applied, followed by trade-ID deduplication. Opposing players'
markets are not merged: a print in another ticker is not evidence that a
resting order in this ticker executed. Settlement payouts are not appended
as trade prints.

## Sampling and conditioning

Each eligible market is sampled on a fixed normalized-time grid, initially
`0, 0.05, ..., 0.95`, restricted to the period before trading closes. Change
this with `prepare --time-step 0.025`. This prevents high-volume games from
contributing a sample for every print while quiet games contribute very few.

At each state, the current price uses only prints at or before that timestamp.
Future labels use prints strictly later than the state and strictly before
close. Equal-timestamp prints do not create an immediate hypothetical fill.
A reverse cumulative minimum lets the saved state support any bid `y` without
rescanning the raw archive. No future prints means neither touch nor through.

A query selects neighboring states using these default absolute tolerances:

| Variable | Default half-width |
| --- | --- |
| Pregame probability / CLV | 0.05 |
| Current price | 0.05 |
| Normalized time | 0.025 |

The exact queried bid `y` is applied to every selected future path; the bid is
not shifted to preserve a relative discount. Peers with current price at or
below `y` are excluded. Override the bandwidths with `--clv-tolerance`,
`--price-tolerance`, and `--time-tolerance`. A smaller time bandwidth than the
prepared grid spacing can leave a query without any neighbors.

First average each fill indicator within a reference **event**, then average
those event rates. Each independent match gets equal weight, including when
both players or multiple time states match. Results report events, tickers,
and sampled states separately. Fewer than 20 reference events yields
`insufficient_support` and null probability estimates; the estimator does not
silently widen the neighborhood. `--min-events` changes that threshold.

This is an empirical conditional estimator, not a fitted parametric model.
The tolerance choices have not been optimized on a held-out dataset, and
the reported execution scenarios are not statistical confidence intervals.

## Outputs and caching

Files are saved under `output/fill_probability/<FAMILY>/`:

| File | Contents |
| --- | --- |
| `markets.parquet` | Every locally stored market in the family |
| `timings.parquet` | Scheduled start, actual settlement, trading close, and source labels |
| `trades.parquet` | Cached prints from the published family partition |
| `normalized_trades.parquet` | Repaired prices, normalized time, and in-game flag per print |
| `snapshots.parquet` | Conditional states and strictly future minima for all eligible tickers |
| `ticker_audit.csv` | Every ticker's inclusion/exclusion reason and sample count |
| `ticker_summary.csv` | Per-ticker CLV, timing, sample count, and inclusion/exclusion reason |
| `trade_coverage.json` | Latest observed family trade, published source, and file/checked-range signature |
| `manifest.json` | Definitions, counts, sampling settings, limitations |
| `last_estimate.json` | Most recent conditional query and its estimates/support |

Rebuilding reuses unchanged inputs. A market is eligible only when the scoped
checkpoint contains a checked interval from no later than its earliest
observed print or scheduled start through settlement. Markets without that
per-ticker coverage appear as `incomplete_trade_horizon` in the audit. The
copy-only migration's latest trade timestamp cannot establish coverage;
collect the relevant range with `collect_data.py trades --family FAMILY
--since ...` before preparing the family. Checked coverage does not prove
Kalshi history before the requested start.

For offline metadata or corrected starts, pass `prepare --timings times.csv`
(Parquet also works) with columns:

```text
ticker,event_ticker,start_time,settlement_time,close_time
```

Use ISO timestamps with UTC offsets. Missing/ambiguous clocks, invalid
durations, missing pregame data, and incomplete horizons stay in the audit.
They are not counted as zero-fill observations.

## Execution limits

A public trade print does not reveal your position in the bid queue, latency,
available depth, cancellation priority, or how much of an order would execute.
These estimates implement the requested touch/through scenarios; they are
not guaranteed bounds on the fill rate of an arbitrary-sized live order.
The copied archive also does not store the block-trade flag now exposed by
[Kalshi's trades endpoint](https://docs.kalshi.com/api-reference/market/get-trades),
so block prints cannot be separated in these historical files. Prices are
the archived cent-resolution values, not reconstructed subcent quotes.

## Python

```python
import pandas as pd
from src.analysis.kalshi.fill_probability import estimate_fill_probability

states = pd.read_parquet(
    "output/fill_probability/KXATPCHALLENGERMATCH/snapshots.parquet"
)
estimate = estimate_fill_probability(
    states,
    family="KXATPCHALLENGERMATCH",
    clv=0.60,
    current_price=0.60,
    normalized_time=0.50,
    bid_price=0.55,
    # ticker="KXATPCHALLENGERMATCH-<match>-<player>",
    # as_of="2026-04-01T00:00:00Z",
)
```
