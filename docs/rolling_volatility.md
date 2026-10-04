# Rolling game-price volatility

The [game chart UI](game_chart_ui.md) shows volatility for **1m, 5m, 10m,
and 30m** below the volume panel, alongside [Up, Down, and Two-way
movement](price_movement_metrics.md).
Each window and the all-game summary show accumulated movement in percentage
points and a 1-minute-equivalent value. The other three displayed metrics
are also square-rooted into percentage points and use the same time scaling.

For decimal target-side probabilities (60% is `0.60`):

```text
volatility_pp = 100 * sqrt(sum((p[i] - p[i-1]) ** 2))
one_minute_equivalent_pp = volatility_pp / sqrt(available_history_seconds / 60)
```

The first expression does not subtract the mean or divide by observation
count. The second puts windows on a common time basis; during warm-up it uses
available history coverage rather than claiming the full window. Neither is
a forecast or an annualized standard deviation.

## Shared sampled price series

`src/analysis/kalshi/rolling_volatility.py` calculates all four movement
metrics for Historical, Kalshi API polling, and Live WebSocket chart modes.
The common source is the chart's timestamped trade-implied probability,
expressed on the selected outcome side. Timestamped bid/ask history is not
available, so historical bid/ask midpoints cannot be reconstructed.

At each regular sample time, the last known trade price is carried forward.
Quiet samples contribute zero change. Prices are never filled before the
first valid trade, and historical as-of calculations ignore trades after the
cursor. The **Price metric sample cadence (seconds)** UI setting controls the
time grid from 1 to 60 seconds, defaulting to 3 seconds. Choose 1 for a
1-second series; the selected value applies to all four metrics. It is separate
from the price-line display interval. Reload the chart to apply a new
cadence. Volume remains actual traded contracts, not carried-forward data.

## Warm-up and live behavior

Before the first price, the metric row says Loading. From the first sample
onward, metrics are numeric; a window shorter than its target length is
labeled partial with its sampled coverage. Flat observed prices yield
`0.00 pp`, not N/A. Calculations advance with time even when no new trade
arrives, so an old move eventually leaves its trailing window.

The UI shows how long the displayed price has been carried forward and,
separately, the feed's connected/paused/reconnecting status. Carried values
are not new quotes. Historical hover uses the chart cursor as its as-of time
and never uses future prices. Changing tickers resets the history. The
shared time-sampled metric helper is queried through the local chart server's
`/metrics` endpoint; Plotly itself does not need to redraw every second.

`tests/test_rolling_volatility.py` checks flat prices, jumps, reversals,
quiet periods, warm-up, cadence selection, ticker switching, and the
square-rooted movement relationships.
