# Realized price-movement metrics

The [game chart UI](game_chart_ui.md) shows an all-game summary at the top of
the metric panel, followed by the same four metrics for each trailing **1m,
5m, 10m, and 30m** window. All three modes use the same calculation. These
are descriptive measures of sampled historical movement, not forecasts.

For successive decimal probabilities on the selected outcome side, let
`delta[i] = p[i] - p[i-1]`. First calculate the squared-movement sums
`U = sum(delta[i]**2 for delta[i] > 0)`,
`D = sum(delta[i]**2 for delta[i] < 0)`, and `S = 2 * min(U, D)`.
The displayed values are their square roots in percentage points:

| Metric | Formula | Display unit |
| --- | --- | --- |
| Up movement | `sqrt(U)` | `100 * sqrt(U)` pp |
| Down movement | `sqrt(D)` | `100 * sqrt(D)` pp |
| Two-way movement | `sqrt(S)` | `100 * sqrt(2 * min(U, D))` pp |
| Realized volatility (Vol) | `sqrt(U + D)` | `100 * sqrt(U + D)` pp |

Down is a nonnegative magnitude. There is no mean subtraction, division by
observation count, annualization, or 0–100 cap. The custom two-way measure is
zero for purely one-way movement and gives more weight to large opposing
moves than small ones. It does not prove uncertainty or fully remove trends.
For displayed pp values, `Vol² = Up² + Down²` and
`0 <= Two-way² <= Vol²` (subject to rounding).

Each accumulated metric also has a 1-minute-equivalent reading. If `T` is
the covered time in seconds, **all four displayed metrics** scale by
`sqrt(60 / T)`. This is equivalent to scaling the underlying squared sums
by `60 / T` before taking their square roots. For a
rolling window, `T` is the lesser of its target length and elapsed time since
the first valid price. For the game summary, `T` is the elapsed time since
that first price. These are time-standardized comparisons, not forecasts;
the accumulated values remain visible alongside them. See
[rolling_volatility.md](rolling_volatility.md) for Vol's interpretation.

## Source and time behavior

The available historical source is the chart's timestamped trade-implied
probability, not bid/ask midpoint history. The selected target-side price is
sampled on a regular grid, with the last known price held between trades;
there is no fill before the first valid price. The UI-selectable cadence is
1–60 seconds, default 3 seconds. Holding prices forward does not recover
movements missed between samples. The price-line drawing interval is separate.

The all-game summary accumulates from the first valid price through the
current chart time; it does not drop older movement when a rolling window
expires. Historical hover computes both game and rolling values as of the
cursor without using future trades. Live
API and WebSocket modes recalculate as time advances, including during quiet
periods: flat windows give zero and old moves eventually expire. Before the
first valid price, the row says Loading; thereafter values are numeric, with
partial coverage shown during warm-up. The row also indicates how long a price
has been held and shows feed status separately. Switching tickers starts fresh
history. Tests are in `tests/test_rolling_volatility.py`.
