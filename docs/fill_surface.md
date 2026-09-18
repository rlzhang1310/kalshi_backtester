# Fill probability surface explorer

**Visualize** opens the existing fullscreen dialog with a Surface/Heatmap switch
and linked bid cross-section. Both views share the four scenario sliders.
The selected bid displays **fitted** and **raw** probabilities separately.
Escape/Close restore focus; camera orientation survives updates until Reset.
Plots stack on narrow screens.

## Target and domain

The production estimator is unchanged: optimistic future minimum <= bid,
conservative future minimum < bid, strictly after observation and before
trading closes. Missing future prints count as misses. These are YES
price-reaching proxies without queue, size, or partial-fill simulation.

Current price p is the last YES trade, and absolute bid b must be below p.
Time remains retrospective start-to-actual-settlement time. At t=1 the surface
is unsupported because the horizon has ended. Inputs are probabilities in
[0,1] internally; the UI displays cents and percentages.

## Revised spline model (version 2)

The old five-component global Beta-CDF mixture could underfit curved data:
equal mixture weights produce a linear function of bid, the penalty favored
similar weights, and broad quadratic conditioning terms limited local bends.

The replacement has local quadratic B-splines in CLV, price, and time, with
interior knots at 0.25, 0.5, and 0.75. Their tensor product supplies 216
nonnegative partition-of-unity weights B_j(c,p,t). Bid uses ten normalized
integrals of nonnegative quadratic splines (monotone I-splines), with interior
knots 0.1, 0.25, 0.45, 0.65, 0.8, 0.9, 0.97 in r=b/p. This internal coordinate
aligns near-current-price behavior across markets; the input remains absolute
bid, never distance below current price.

```text
I_k(r) = integral_0^r spline_k(u) du / integral_0^1 spline_k(u) du
q_j = softmax(theta_j): an intercept, ten increments, one unused remainder
F(c,p,t,b) = sum_j B_j(c,p,t) [q_j0 + sum_k q_jk I_k(b/p)]
```

This function is smooth in all four inputs, bounded in [0,1], and
nondecreasing in absolute bid. The remainder allows an endpoint below one.
The default zero-bid value is learned from observations via q_j0: there is
no forced 1-p floor.

The optional legacy mode evaluates 1-p + p * sum_j B_j sum_k q_jk I_k(b/p),
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
- fill_surface_ui.py: shared scenario state, caches, charts, dialog, and
  raw/fitted selected-bid comparison.
- fill_surface_browser.js: camera/resize/focus handling with one listener set
  that is disposed when closing.
- fill_app.py: original empirical estimator and Visualize entry point.

Model keys include source path/mtime/size, family, excluded ticker, cutoff,
outcome, smoothing, boundary mode, and model version. Grid keys add CLV/time,
tolerances, and minimum support. Price/bid changes do not refit the model.
Streamlit uses current widget state without custom asynchronous requests
that could replace results with stale responses.

Tests cover exact optional legacy boundaries, learned zero values,
monotonicity, continuity, bounds, missing/zero observations, whole-game
splits, known linear and curved functions, spacing-aware curvature,
cross-view agreement, 50-cent rounding regression, shared sliders, and cache
reuse. Browser checks cover camera, Escape/focus, resizing, and screenshots.
