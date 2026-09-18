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
explore historical touch and trade-through rates. **Visualize** opens a
fullscreen 3D surface, heatmap, and linked bid curve.

These are historical price-reaching estimates. The surface learns its boundary
from the data, and event time uses realized settlement.

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
