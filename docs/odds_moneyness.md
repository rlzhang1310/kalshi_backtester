# Oddness Visualizer

Open **Oddness Visualizer** in the top navigation (`/odds-moneyness`). Registration
is in `fill_app.py`; family preparation, game selection, reference-match exclusion
and ticker-summary downloads remain shared with the other pages. Existing CLV
and no-CLV models and their numerical behavior are unchanged.

The independent `odds-moneyness-1` model estimates YES **price-reaching probability**:

```text
d = logit(current_price) - logit(absolute_bid)
M(d, normalized_elapsed_time)
```

Prices use probabilities internally. Positive distance means a below-market bid;
larger distance means farther below market. No volatility or time scaling is
applied. CLV, current price and bid never enter prediction separately. Changing
price/bid changes predictions only through distance. The inverse is
`bid = logistic(logit(price) - distance)`.

## Outcomes and finite prices

The dataset uses the existing eligible prepared snapshots and future minimum,
strictly after observation and before trading closes. Optimistic labels use
`future_min_cents <= bid_cents`; conservative labels use `<`. Missing future prints
remain misses under the existing estimator. These are not confirmed executions.
Prepared prices already use the YES convention, including complemented NO-taker
prints; this model does not complement them a second time.

Each eligible source state contributes actual price-reaching targets at the
positive cent bids 1–99¢ strictly below its current price. Zero-bid anchors are
excluded. Current prices at zero/one, nonfinite prices/times and ended horizons
are separately counted in the filtering audit. At/above-market bids are excluded
because the existing estimator only evaluates below-market bids. Observed future
minimum prices of zero still produce legitimate positive-bid hits; settlement
at zero is outside the continuous log-odds surface, not outside the outcome.

The UI supports finite price/bid scenarios from 1–99¢. The distance display spans
zero to the maximum observed finite distance; evaluation requires **d>0** and
**0≤t<1**. The maximum possible distance for 99¢ versus 1¢ is about 9.19024.
Prices at endpoints never receive fabricated finite logits or epsilon clipping.

State-bid observations are grouped by event, market, current-price band,
0.1-distance bins and 0.025-time bins. Hits and trials are summed. Feature/bin
centroids use trial-weighted means. Original price/bid and CLV remain diagnostic
metadata only. Empirical probability is `hits/trials`; observed zeros are kept.
Repeated bids/states are dependent observations, not independent games.

## Fit, support and validation

A cubic tensor-product **binomial spline** uses 12 distance and 9 time basis
functions, with separate second-difference penalties for each axis. A bounded
L-BFGS-B fit minimizes the binomial negative log likelihood and these penalties.
Distance coefficients are nonincreasing, enforcing monotone decrease in distance.
This reflects the nested future-minimum price-reaching target: farther bids need
lower future prices under the same horizon. It remains a pooled-price modeling
assumption; the model reports the empirical distance trend and price-dependent
departures rather than treating standardization as proven. Time is unconstrained.
There is no synthetic zero-bid boundary or `1-p` floor.

Automatic smoothing tries five settings, including different distance/time
penalties. SHA-256 event partitions assign roughly 60% of complete games to
training, 20% to tuning, and 20% to an independent final test. No timestamps or
opposing tickers from one match can enter different partitions. Smoothing is
chosen by tuning log loss, preferring converged fits; the displayed model is
then refitted on all eligible games. Fewer than 30 games, 10 training games,
5 tuning games or 5 test games produce an explicitly unvalidated default fit.
Manual axis smoothing still receives held-out diagnostics when enough games exist.

Reports include trial-weighted Brier score, log loss and calibration on untouched
test games. Brier scores use the binary-outcome formula, not squared error against
aggregated bin rates. Price-band diagnostics cover <20¢, 20–40¢, 40–60¢, 60–80¢,
and ≥80¢, with separate calibration curves and observed-minus-predicted residuals.
Market-type diagnostics are also generated for represented families.

The within-distance/time check uses bins spanning at least two price bands and
five test games. It removes weighted bin means before regressing residuals on
current price, and reports event-cluster standard errors and approximate 95%
intervals. This is a diagnostic association, not a causal or definitive model
comparison. Families are fitted separately in the UI; their market results do
not establish that one model transfers across sports.

Support queries actual empirical-bin centroids within configurable distance/time
tolerances (default ±0.2 and ±0.05). Distinct-game membership is combined without
double-counting a game across nearby bins. At least 20 distinct games are required
by default. Sparse, extrapolated and ended-horizon predictions are masked in both
charts; valid 0% is a visible color, while missing support is blank. Trial counts
are disclosed separately from distinct games. Local empirical rates use summed
state-bid hits/trials, rather than the other visualizers' equal-event readout.

## UI behavior and performance

Heatmap and 3D Surface evaluate one cached grid with one support mask and fixed
0–100% probability scales. The selected point is evaluated directly, independently
of grid resolution. Raw estimates can be overlaid with trial counts and average
underlying price/bid on hover. Grid resolution does not change spline complexity.

There is one heatmap and one 3D surface for the selected model settings. The
standardized surface has no scenario-price or scenario-bid dimension. Changing
price/bid only recomputes `d` and the exact selected estimate: equivalent `(p,b)`
pairs at the same `d,t` have identical predictions. Scenario controls and readouts
run in a sidebar fragment. Their reruns do not reload snapshots, evaluate the
display grid or reconstruct Plotly figures. Only the red selected markers move;
the plotted probability data, raw overlays and camera stay in place. Changing
model/data/support/display settings still updates the surface as needed.

Scenario sliders, editable price/bid fields and time playback stay in the sidebar.
Typing a price or bid updates its slider, and slider changes update the field.
Both accept 1–99 cents with 0.01-cent increments. These changes update only the
scenario fragment and selected markers. Both charts use
an uneven distance grid: half the samples cover the lowest sixteenth of the
distance domain, with progressively wider intervals farther out. Heatmap and
surface share this grid; fitted probabilities, empirical aggregation, support
windows and exact scenario markers retain their existing calculations. Both charts support
click-to-bid mapping while holding current price fixed; only the inverse-mapped
bid's finite 1–99¢ domain is clamped. View, scenario, smoothing and raw-data state
are preserved across reruns. Spacious 3D margins and an orthographic camera keep
axes legible; the browser retains camera orientation until Reset camera.

Both charts place normalized time on x and distance on y. The distance display
uses `log(1 + distance / 0.1)` to expand detail near zero and compress the tail;
ticks and hover labels show original distances. Clicks invert this display
transform before calculating a bid. Default grid resolution is 101 per dimension.

Play/Pause advances time in the browser using exact, batched predictions at 0.01
steps. It moves only the selected markers on the fixed heatmap and 3D surface.
Play/Pause controls are also above the 3D chart. Per-frame updates
remain separate from dragging: the thumb/time label update immediately during a
seek, while markers and the scenario commit update on release. Seeking pauses
playback and cancels a pending preview from an earlier drag. Playback updates
do not rerun Python or rebuild the surface. Slow draws coalesce queued frames;
pause, seek, end and chart clicks synchronize the scenario fragment. Model/scenario tokens reject
stale commits, and cleanup cancels timers/listeners on remount or navigation.

Dataset statistics accumulate in bounded chunks. Predictions run in batches;
support uses compact distinct-game bitsets and a tree over empirical bins.
Model caching includes path/mtime/size, family, excluded game, cutoff, target,
smoothing and version. Grid caching also includes support settings and resolution.
Data/model work is synchronous during a rerun, so an older asynchronous fit cannot
overwrite a newer selection. Loading, filtered/no-data and error states are shown.

`?debug=1` shows slider units, probabilities, logits, feature order, exact/nearest
grid estimates, nearest empirical bin counts, support, smoothing and version.
The metadata expander downloads a separate versioned JSON model artifact.

## Saved-data validation and checks

See [the saved validation report](odds_moneyness_validation.json) for full scores,
calibration, price-band residuals, event partitions and audit counts from local
prepared data. These scores describe the included archived games, not prospective
execution accuracy. The normalized clock depends on realized settlement time.
The tests use small synthetic fixtures only to check invariants; reported model
validation uses actual prepared snapshots.

| Prepared family | Train / tune / test games | Test Brier | Test log loss |
| --- | --- | --- | --- |
| NFL | 168 / 34 / 40 | 0.1740 | 0.5169 |
| College football | 502 / 193 / 185 | 0.1370 | 0.4216 |
| NBA | 428 / 159 / 140 | 0.1581 | 0.4722 |
| ATP Challenger | 2506 / 857 / 861 | 0.1762 | 0.5211 |

These fits selected distance/time penalties `(1e-6, 1e-6)` and converged. The
empirical overall distance slope was negative in all four families, consistent
with the enforced distance direction. Absolute-price standardization is
incomplete in all four: low-price bands (<20¢) have mean residuals of roughly
**+28 to +32 percentage points**, while ≥80¢ bands have roughly **−8 to −11
points**. Thus the collapsed surface underestimates reaching rates in the low
bands and overestimates them in the high bands. Price still explains residuals
within overlapping distance/time bins; the event-cluster intervals exclude zero.
This motivates evaluating richer models later, without adding price or CLV to
this model. No claim of cross-sport transfer is made.

An uncached fit plus 51×51 support grid took about 3 seconds for NFL and 47 seconds
for the largest prepared ATP family in this environment. Cached scenario updates
reuse those fits. Runtime varies with data size; initial fitting is shown as a
loading state.

Reproduce a versioned artifact and report without API calls:

```powershell
python -m scripts.compare_odds_moneyness --family KXNFLGAME
python -m pytest tests/test_fill_probability.py tests/test_fill_surface.py tests/test_no_clv_fill.py tests/test_odds_moneyness.py tests/test_fill_playback.py tests/test_fill_app.py -q
node --test tests/fill_playback.test.mjs tests/odds_moneyness_playback.test.mjs
```

Implementation: `src/analysis/kalshi/odds_moneyness.py` contains transformation,
fit, validation and support; `odds_moneyness_ui.py` contains caching, charts and
controls; `odds_moneyness_browser.js` contains playback, clicks and camera state.
The offline artifact command is `scripts/compare_odds_moneyness.py`. No new runtime
dependencies are required. Browser screenshots were unavailable because no browser
was connected; automated chart, UI-state and browser-controller checks were run.
The UI test additionally sends real fragment slider events and checks that
snapshots, model, grid and chart construction run only once across price/bid changes.
