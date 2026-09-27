# Fill probability surface explorer

**Visualize** navigates to the full-page **CLV Visualizer** tab at `/clv`, with a
Surface/Heatmap switch and linked bid cross-section. Both views share the four
scenario sliders, always accessible in the sidebar.
The selected bid displays **fitted** and **raw** probabilities separately.
Top navigation returns to Historical estimates or the No-CLV Visualizer.
Camera orientation survives updates until Reset.
Plots stack on narrow screens. Larger margins, a framed camera and legends above
the plots leave room for lower axes and labels.

## Target and domain

The production estimator is unchanged: optimistic future minimum <= bid,
conservative future minimum < bid, strictly after observation and before
trading closes. Missing future prints count as misses. These are YES
price-reaching proxies without queue, size, or partial-fill simulation.

Current price p is the last YES trade, and absolute bid b must be below p.
Time remains retrospective start-to-actual-settlement time. At t=1 the surface
is unsupported because the horizon has ended. Inputs are probabilities in
[0,1] internally; the UI displays cents and percentages.

## Log-odds coordinates (version 3)

The default Model scale is **Log-odds**. **Probability (previous model)**
retains version 2's coordinates for comparison on the same data and whole-game
holdout. Neither scale is presumed more accurate. Both charts, the linked
curve, and the selected readout use the selected model.

CLV is the pregame probability. CLV and current price use finite log-odds:

```text
e = 0.005  (half a cent)
L(x) = log((x + e) / (1 - x + e))
u(x) = (L(x) - L(0)) / (L(1) - L(0))
r(b,p) = 1 + (L(b) - L(p)) / (L(p) - L(0))
```

This regularization maps exact zero and one to finite values while remaining
strictly increasing throughout [0,1]. It is equivalent to applying ordinary
logit to `(x+e)/(1+2e)`. It does not clip a band of small positive bids to zero.
The bid log-odds gap is normalized for each current price: zero bid maps to
0 and bid equal to price maps to 1. Thus existing monotone splines and the
optional exact zero-bid boundary remain valid. Price zero has no eligible
bids and stays unsupported. This normalization means equal log-odds gaps at
different prices need not have identical spline coordinates.

Only model input geometry changes: the conditioning basis uses `(u(c),u(p),t)`
and the bid basis uses `r(b,p)`. Time, empirical pooling, binary hit/miss labels,
support tolerances, and chart units remain unchanged. Predictions are directly
bounded probabilities; historical rates are never averaged as odds. Roughness
is penalized in the transformed spline coordinates, so automatic smoothing
is selected separately for each scale. The half-cent regularization is a fixed
modeling choice, not a fitted or validated accuracy claim.

## Spline model

The old five-component global Beta-CDF mixture could underfit curved data:
equal mixture weights produce a linear function of bid, the penalty favored
similar weights, and broad quadratic conditioning terms limited local bends.

The replacement has local quadratic B-splines in CLV, price, and time, with
interior knots at 0.25, 0.5, and 0.75 in the selected coordinates. Their tensor product supplies 216
nonnegative partition-of-unity weights B_j(c,p,t). Bid uses ten normalized
integrals of nonnegative quadratic splines (monotone I-splines), with interior
knots 0.1, 0.25, 0.45, 0.65, 0.8, 0.9, 0.97 in r (above for log-odds;
r=b/p for probability scale). This internal coordinate
aligns near-current-price behavior across markets; the input remains absolute
bid, never distance below current price.

```text
I_k(r) = integral_0^r spline_k(u) du / integral_0^1 spline_k(u) du
q_j = softmax(theta_j): an intercept, ten increments, one unused remainder
F(c,p,t,b) = sum_j B_j(c,p,t) [q_j0 + sum_k q_jk I_k(r(b,p))]
```

This function is smooth in all four inputs, bounded in [0,1], and
nondecreasing in absolute bid. The remainder allows an endpoint below one.
The default zero-bid value is learned from observations via q_j0: there is
no forced 1-p floor.

The optional legacy boundary evaluates 1-p + p * sum_j B_j sum_k q_jk I_k(r),
with the intercept omitted. It preserves F(0)=1-p exactly but necessarily
forces F>=1-p at every higher bid. The UI warns about this and counts
conflicting empirical bins. This boundary is explicitly synthetic, with no
sample count. Neither mode changes the raw data or production estimator.

## Fitting and validation

Source CLV/price/time observations are pooled in 0.1 bins, retaining actual
mean coordinates. Absolute bids use integer 5-cent steps. Each bid includes
only states with observed price above it. Average labels within each
event/bin first, then across events. Keep probability, fractional
event_rate_sum, n_events, n_observations, integer state-label n_hits, and
synthetic=False. Hits are price-reaching labels, not executed orders.

Fit event-count-weighted Bernoulli cross entropy plus a curve roughness
penalty. The bid term averages squared second derivatives on a fixed 81-point
relative-bid grid, dividing differences by actual spacing squared. The
conditioning term regularizes second differences of neighboring control
curves on their coefficient lattice with mean scaling. Unlike the old
penalty, this does not force mixture-density weights to be equal. Straight
data can still produce a straight fit; curvature is not added for appearance.

L-BFGS-B optimizes logits and softmax enforces constraints. This is a
quasi-binomial objective: event means and labels repeated across bids are
dependent, so fitting weights do not justify confidence intervals.
Observed bid-zero rows participate in the learned-boundary fit and scoring.
In legacy mode their value is fixed and cannot inform free coefficients.

Automatic smoothing compares 1e-7, 1e-5, and 1e-3 using a deterministic
SHA256-based whole-game 80/20 split. Both outcomes and every timestamp of a
match stay together. With at least 30 games overall, 10 training games and
5 validation games, select the lowest held-out Brier loss, then refit all
reference data. Smaller datasets default to 1e-5, marked unvalidated.
Manual strengths, including zero, use the same holdout when available.
Candidate and final optimizer convergence are reported.

Diagnostics also report event-count-weighted holdout log loss and ten
calibration buckets per smoothing candidate (mean predicted and observed
probabilities). `event_bin_weight` counts repeated event/bin/bid contributions,
not independent games. These use identical empirical bins and deterministic
splits across coordinate choices, making the scores comparable; choosing a
scale from them still requires an independent final test for an unbiased
performance estimate.

Synthetic anchors never enter scoring. This is a selection holdout, not an
independent final test. positive_bid_scores separately reports Brier loss
without bid-zero observations so it can be compared with version 1's
positive-bid-only validation metric.

## Support, raw points, and diagnostics

Display support uses original source states and the estimator's CLV/price/time
tolerances, minimum distinct games, and observed-price-above-bid rule. The
selected ticker's whole event and any settlement cutoff are excluded before
fitting. Unsupported values stay NaN. Surface and Heatmap share the identical
51 x 51 evaluated grid; the curve and readout use the same fitted function.

Cross-section raw points use the production estimator and selected
neighborhoods, with minimum one match for inspection; hover shows actual
game/state counts. The selected raw readout uses the configured minimum
support. These are pooled neighboring-state estimates, not exact observations
at a continuous slider value. No confidence band is fabricated. Changing
peer membership with bid can make raw estimates nonmonotone; the fitted
function enforces monotonicity for the common price-reaching horizon.

Curvature diagnostics use mean squared central second derivatives along bid
and price in [0,1] units, computed with actual display-grid spacing. Only
three-cell fully supported stencils contribute. Lower curvature means less
bending, not better predictions. Resolution can slightly change the averaging
region at mask edges, but never the fitted coefficients or regularization.

## Implementation and cache

- fill_surface.py: filtering, empirical bins, local spline model, support
  masks, batch grid evaluation, and curvature.
- fill_surface_ui.py: shared scenario state, caches, charts, full-page workspace, and
  raw/fitted selected-bid comparison.
- fill_surface_browser.js: camera/resize handling with one listener set.
- fill_app.py: original empirical estimator and Visualize entry point.

Model keys include source path/mtime/size, family, excluded ticker, cutoff,
outcome, smoothing, boundary mode, coordinate scale, and model version. Grid keys add CLV/time,
tolerances, and minimum support. Price/bid changes do not refit the model.
Streamlit uses current widget state without custom asynchronous requests
that could replace results with stale responses.

Tests cover exact optional legacy boundaries, learned zero values,
monotonicity, continuity, bounds, missing/zero observations, whole-game
splits, known linear and curved functions, spacing-aware curvature,
cross-view agreement, 50-cent rounding regression, shared sliders, and cache
reuse and full-page navigation. Camera and resizing checks require a connected
browser; chart framing is also checked in the figure-layout tests.
