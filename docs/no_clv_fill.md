# No-CLV Visualizer

Start the existing dashboard with `python -m streamlit run fill_app.py` and
select **No-CLV Visualizer** in the top navigation. Its route is `/no-clv`;
the CLV charts have a full-page tab at `/clv` and historical estimates remain at
`/`. Family preparation/refresh, saved-data
selection, ticker selection/exclusion, coverage and ticker-summary downloads
are shared entry-point controls. Switching pages preserves the selected family,
ticker and each page's scenario. The new visualizer is a full-page workspace.

The three sliders are current YES price, retrospective normalized elapsed time,
and absolute YES bid. Scenario and playback controls live in the sidebar so
they remain accessible while scrolling through charts. Prices display in cents
and are converted to [0,1] once.
Inference has exactly feature order `[current_price, normalized_time, bid]`.
CLV is removed from training features and support neighborhoods, never assigned
a constant. Historical preparation still uses the existing eligible dataset:
removing CLV from inference does not recover tickers excluded during preparation.

## Target and pooling

Use the same future-minimum labels as the original estimator: optimistic means
a future YES print at/below the bid, conservative means strictly below it.
The future window remains strictly after the sampled observation and before
trading closes. Missing future prints count as misses. These are price-reaching
proxies, not confirmed orders, queue simulation, size or partial executions.
Original future minima, including missing values, remain in the source table.

Pool original states over all pregame prices, in 0.1 price/time bins and absolute
five-cent bid steps. Historical price must be above the bid for eligibility.
Keep integer `n_hits`, `n_observations` and `n_missing_future`, distinct-game
`n_events`, event-rate sums, and actual mean price/time coordinates. Observed
zero hit rates at positive bids are retained. There are no synthetic training
rows. The selected ticker's whole game and optional settlement cutoff are
excluded with the existing reference-data rules.

Fitting uses binomial state-level hits/trials. The raw selected-point and
time-series neighborhood rates keep the production estimator's equal weight
per distinct game: average labels within each game, then across games.
Both `state_probability` and equal-game `probability` are available in raw-bin
downloads, so weighting is explicit. Repeated game states and bid labels are
dependent; these weights do not justify binomial confidence intervals.

## Independent fitted model and boundary

Reuse SciPy quadratic B-splines with six local bases each along price and time,
giving 36 conditioning curves, and ten monotone integrated bid bases. Price
uses finite half-cent-regularized log-odds; time remains in its original units.
Bid uses the same normalized log-odds gap as the CLV surface (absolute bid is
the public input). Display-grid resolution (21/51/81) is independent of knots,
coefficients and smoothing.

```text
L(x) = log((x + .005) / (1 - x + .005))
r(b,p) = (L(b) - L(0)) / (L(p) - L(0))
q_j = softmax(theta_j)
G(p,t,b) = 1 - p + p * sum_j B_j(p,t) sum_k q_jk I_k(r(b,p))
```

The unused softmax mass permits an endpoint below one. The bid-zero basis is
exactly zero, so `G(p,t,0) = 1-p` inside the function for every time and price,
including p=0 and p=1. At p=0 the internal bid coordinate safely defaults to
zero; positive bids are outside the valid domain. Outputs are bounded and
continuous. Because the target uses a fixed future horizon, increasing bid
cannot reduce price-reaching probability; nonnegative increments enforce this.

**Consequence:** exact `1-p` at zero together with nondecreasing bid probability
necessarily forces `G >= 1-p` at every positive bid. This can prevent matching
low observed rates. The page explains that floor and reports conflicting bins.
Observed rates are never replaced by the boundary. The amber line is labeled
**Assumed boundary: bid = 0, probability = 1 − current price** and has no
fabricated empirical sample count. The existing CLV model retains its learned
boundary and optional legacy assumption.

## Smoothing and validation

Separate price, time and bid penalties act on second derivatives/differences
in spline coordinates, with spacing and mean scaling. L-BFGS-B optimizes logits.
Automatic selection uses the same deterministic SHA256 whole-event 80/20
split as the CLV model. Both outcomes and every timestamp stay together.
Require 30 total games, 10 training games and 5 holdout games. Compare:

```text
(price, time, bid)
(1e-7, 1e-7, 1e-7)
(1e-5, 1e-5, 1e-5)
(1e-3, 1e-3, 1e-3)
(1e-5, 1e-7, 1e-7)
(1e-7, 1e-5, 1e-7)
```

Select the lowest held-out log loss among converged candidates (report all
convergence flags; if none converge, use the best provisional fit). Refit all
reference data afterward. Smaller datasets use `(1e-5,1e-5,1e-5)` and show an
unvalidated warning. Manual axis controls use log10 strength and initialize
around the last automatic choice. Manual settings receive the same holdout
diagnostics when enough games exist.

Exclude all bid-zero rows from fitting and validation. Reports include Brier
loss, log loss, calibration buckets, price buckets, time buckets, settings and
model version. This is a selection holdout, not an independent final test.

## Linked views, support and playback

- **Price/Bid Heatmap** (default): hold selected time fixed. Price x, absolute
  bid y, fixed 0–100% colors; exact selected-point marker.
- **Time Surface**: hold selected price fixed. Time x, bid y, probability z;
  Play/Pause controls sweep an exact current-time cross-section across bids.
  fixed 0–100% z/colors, assumed boundary and exact selected marker. Camera
  state is retained across redraws by a separate browser presentation adapter;
  Reset camera changes its revision.
- **Selected-Point Time Series**: hold price/bid fixed. Includes the exact
  selected time and neighboring bids ±1¢ within the valid domain. Optional raw
  markers disclose neighborhood rates and actual game/state counts.
- **Time Snapshots**: heatmaps at .10/.30/.50/.70/.90 with identical axis ranges and color
  scale. Separate bordered chart cards leave room for each title and axis; the title
  marks the time closest to selection.

Readouts and markers evaluate G at exact slider values, independently of the
nearest display-grid cell. Support uses original source states, price/time
tolerances and distinct games, pooled over every CLV. Supported means at least
the configured minimum games; sparse means some but fewer than the minimum;
unsupported means no neighbors. Positive-bid sparse/unsupported predictions are
masked, as are bids at/above current price and time=1 (ended horizon).
Valid observed zeros remain visible as zeros. The bid-zero assumption is shown
separately even where empirical coverage is absent or the horizon has ended;
it is classified as an assumed boundary, not a supported historical estimate.

The selected chart tab survives slider changes and playback synchronization.
Play/Pause advances selected elapsed time from 0 to 1 at a fixed rate, retaining
price/bid. A native Streamlit v2 component provides a keyboard-accessible time
slider and browser playback. It uses 101 cached, exact model frames at 0.01
steps. Seeking pauses playback and updates the thumb/time label immediately;
chart updates and the Python commit wait until release. A new drag cancels any
pending seek preview. Playback updates Plotly traces, readouts and linked markers without Python
reruns, chart reconstruction or camera resets. The installed Plotly bundle is
shipped locally; no CDN is needed. Only pause, seeking or completion syncs time
back to Python. Updates are serialized, keeping only the newest pending frame;
cleanup cancels timers on changes/navigation and model tokens reject stale
state commits. Playback stops at 1, on errors or when leaving the page. Resource/grid
caches include data path/mtime/size, family, excluded ticker, cutoff, outcome,
smoothing, independent model version, fixed slice inputs, support and resolution.
Price, time and bid changes do not refit the model. Playback matrices are cached
independently of selected price/bid, while exact selected-point predictions use
the current selection. Larger 3D margins, an orthographic camera and legends
above the chart prevent lower labels from running outside the plot frame.

Add `?debug=1` to the route for development diagnostics: original slider values,
normalized feature order, exact and displayed predictions, nearest grid cell,
empirical neighborhood counts/rate, support, version and smoothing. It is hidden
in the normal UI. Raw bins and validation reports have separate downloads.

## Offline comparison and verification

```powershell
python -m scripts.compare_no_clv_fill --family KXNBAGAME
python -m pytest tests/test_fill_probability.py tests/test_fill_surface.py tests/test_no_clv_fill.py tests/test_fill_playback.py tests/test_fill_app.py -q
node --test tests/fill_playback.test.mjs
python -m ruff check fill_app.py src/analysis/kalshi/no_clv_fill.py src/analysis/kalshi/no_clv_fill_ui.py scripts/compare_no_clv_fill.py tests/test_no_clv_fill.py tests/test_fill_app.py
```

The comparison writes `no_clv_comparison_optimistic.json` (or conservative) into
the chosen prepared-family folder. Both models score identical positive-bid,
observation-level held-out labels, with equal total weight per event/bid.
It saves calibration, price/time buckets and `abs(price-CLV)>=.20` diagnostics.
CLV is used only by the original model and offline subgroup evaluation.
The two models differ in boundary assumption and fitting weights as well as
CLV conditioning; their comparison does not isolate CLV's incremental value.

Tests verify pooling/counts/zeros/missing prints, three-input units/order,
endpoint boundaries, continuity/monotonicity/bounds, independent axis penalties,
known curved data, game-level splitting, exclusion of synthetic rows from
scoring, support masks, exact agreement across views at multiple resolutions,
browser playback state, pause/seek synchronization, canceled/coalesced frames,
route/shared-selector persistence, no-data/error states,
cache reuse and the original CLV regressions. Visual camera/resizing behavior
still requires a connected browser when automated browser access is unavailable.

Implementation: `no_clv_fill.py` owns pooling/model/evaluation; `no_clv_fill_ui.py`
owns page state, caches, charts and playback; `no_clv_fill_browser.js` retains
only camera presentation state. `fill_playback.py`/`fill_playback.js` own the
cached browser playback and session bridge. `fill_app.py` owns navigation and shared top
controls. No deployment or publication is part of this feature.
