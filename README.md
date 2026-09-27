# Kalshi sports research

Explore historical sports markets: how prices move during games, how often
lower bids are reached, and how market prices compare with actual outcomes.

## Start the dashboard

From the repository folder:

```powershell
python -m pip install -r requirements-ui.txt
python -m streamlit run fill_app.py
```

Open the local URL printed in the terminal. Keep the terminal running while
using the dashboard; press `Ctrl+C` to stop it.

Choose a prepared ticker family or run a new one using your collected data.
Adjust pregame probability, current price, elapsed time, and bid price to
explore historical touch and trade-through rates. **Visualize** opens the
full-page **CLV Visualizer** tab at `/clv`, with a 3D surface, heatmap, and linked
bid curve. Scenario sliders stay in the sidebar while you explore the charts.

These are historical price-reaching estimates. The surface learns its boundary
from the data, and event time uses realized settlement.

Select **No-CLV Visualizer** in the top navigation for a full-page workspace at
`/no-clv`, pooling all pregame probabilities. Both pages share family preparation,
game selection and ticker-summary downloads. The new page has a linked heatmap,
time surface, time series, snapshots and time playback. Its zero-bid boundary is
the explicit `1 − current price` assumption. See the
[no-CLV model and validation details](docs/no_clv_fill.md).

No-CLV playback uses cached frames in the browser, keeping the charts and camera
steady while time advances. Its time slider and Play/Pause controls are also in
the sidebar. Dragging time updates the thumb and time label immediately; charts
update when you release the slider. The **Historical estimates** tab remains available at `/`.

**Oddness Visualizer** at `/odds-moneyness` estimates price-reaching probability using
only `logit(current price) − logit(bid)` and normalized elapsed time. It shares
game preparation and selection, with linked heatmap/3D views, smooth playback,
click-to-bid selection and retained view/camera state. Scenario changes move the
red dot on the existing charts without reloading data or redrawing their grids.
Current price and absolute bid can be typed into number fields or adjusted with
their linked sliders. The shared chart grid uses finer intervals near zero
log-odds distance and wider intervals farther out.
It excludes synthetic
zero-bid anchors and reports whole-game validation and calibration by price band.
See [the odds-moneyness model and UI details](docs/odds_moneyness.md).

## Collect historical data

Run these commands from the repository folder with Python 3.11 or newer:

```powershell
python -m pip install -r requirements.txt
python collect_data.py
```

Choose **Kalshi Global Trades** to download trade history. Trades stream into
Parquet files under `data/kalshi/trades_global_staging/` until both the historical
and live feeds have been read.

Press `Ctrl+C` to pause. Run `python collect_data.py` and choose the same option
to resume from the saved checkpoint. Completed feeds are skipped.

Run `python collect_data.py` again and choose **Kalshi Markets** to download
market metadata into `data/kalshi/markets/`.

Then start the dashboard, open **Run a new family or refresh existing data**,
choose a sports ticker family, and click **Run family** to prepare your local
data for the visualizers.

## Other tools

| What you want to explore | Run |
| --- | --- |
| One game's odds over time | `python visualize_game.py` |
| Win rates by market price | `python analyze.py win_rate_by_price` |
| Fill estimates in the terminal | `python estimate_fill.py` |

The game visualizer also has a notebook and accepts optional sportsbook and
play-by-play overlays. Local analyses use collected data under `data/`; saved
results go under `output/`.

## Technical context

[AI context and detailed workflows](docs/ai_context.md) contains the code map,
data conventions, detailed commands, overlay formats, and testing notes.
See the [fill estimator design](docs/fill_probability.md) and
[surface model details](docs/fill_surface.md) for the modeling methods.
