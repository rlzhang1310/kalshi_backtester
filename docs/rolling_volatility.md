# Rolling game-price volatility

The [game chart UI](game_chart_ui.md) shows four rolling windows below the
volume panel: **1m, 5m, 10m, and 30m**. Each window has accumulated realized
volatility in percentage points and a 1-minute-equivalent value.

For timestamped decimal probabilities (for example, 60% is `0.60`):

```text
volatility_pp = 100 * sqrt(sum((p[i] - p[i-1]) ** 2))
one_minute_equivalent_pp = volatility_pp / sqrt(window_minutes)
```

The measure sums squared successive price changes and takes their square
root; it does not subtract the mean or divide by observation count. The
1-minute equivalent compares windows on a common time basis but is not a
forecast or annualized standard deviation. Market activity can still differ
between windows.

## Source and sampling

The helper in `src/analysis/kalshi/rolling_volatility.py` is shared by
Historical, Kalshi API polling, and Live WebSocket chart builds. It uses the
chart's timestamped trade-implied target probability. Historical bid/ask
quotes are not available, so a bid/ask midpoint cannot be reconstructed.
It takes the last **observed** trade in each fixed time bucket. Empty buckets
are never filled and individual WebSocket messages are not assumed to be
equally spaced. The UI's **Volatility sample cadence (seconds)** control
selects the bucket width from 1 to 60 seconds (default 3); it is independent
of the price-line display interval. Reload the chart to apply a new cadence.

Each as-of sample is timestamped with the actual last trade in its bucket.
Historical hover chooses the latest sample at or before the cursor, never
one from the future. Live updates rebuild the values as trades arrive. A
new ticker gets a new history; the 30-minute window needs historical trades
spanning enough of that period.

## Availability rules

| Window | Sample near start required within | Largest allowed observed-sample gap |
| --- | --- | --- |
| 1m | 10s | 30s |
| 5m | 60s | 120s |
| 10m | 120s | 180s |
| 30m | 360s | 600s |

N/A means insufficient window coverage, unsupported resolution, or a material
gap. Recent trades alone do not make a window available: a trade must also
cover near its start, there must be at least two bucketed observations, and
gaps must stay within the limit. A valid flat observed window is `0.00 pp`,
not N/A. When hovering a historical chart, a sample more than one cadence
behind the cursor is also treated as stale. The tooltip gives the specific
reason and formula. Sparse games may have N/A for short windows even when
longer windows have values.

`tests/test_rolling_volatility.py` covers known movements, flat prices,
warm-up, gaps, ticker changes, cadence selection, and time normalization.
