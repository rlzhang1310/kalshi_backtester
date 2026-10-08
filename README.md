# Kalshi sports research

The local [manual quoting desk](docs/one_contract_order_tool.md) compares two live Kalshi markets and supports single or paired one-contract maker orders. See its guide for setup, demo verification, production settings, and order recovery.

Explore historical sports markets: how prices move during games, how often
lower bids are reached, and how market prices compare with outcomes.

## Quick start

Install the UI dependencies and start the fill dashboard:

```powershell
python -m pip install -r requirements-ui.txt
python -m streamlit run fill_app.py
```

Open the local URL printed in the terminal. The dashboard has historical
fill estimates and CLV, No-CLV, and Oddness visualizers. To chart one game's
odds, traded volume, and price-movement metrics using stored data, the Kalshi
API, or a live WebSocket, run:

```powershell
python -m streamlit run volatility_app.py
```

Both apps run locally. Keep the terminal open and press `Ctrl+C` to stop.
See the [fill dashboard guide](docs/fill_dashboard.md) and
[game chart UI guide](docs/game_chart_ui.md) for modes and controls.

## Get historical data

```powershell
python -m pip install -r requirements.txt
python collect_data.py
```

Use the menu to collect Kalshi trades and market metadata, then complete and
verify the [family-partitioned migration](docs/trade_reorganization.md). For
initial adoption of one family, run:

```powershell
python collect_data.py trades --family KXNFLGAME --since 2026-09-01T00:00:00Z
```

Local charts and analyses read the published family dataset; fill dashboards read
prepared observations and need family preparation after trade updates. See
[data collection](docs/data_collection.md) for checkpoints and paths. The data loader is
inspired by [Jon Becker's prediction-market-analysis project](https://github.com/Jon-Becker/prediction-market-analysis).

## Other commands

| Task | Command | Guide |
| --- | --- | --- |
| One-game odds chart | `python visualize_game.py` | [Game visualizer](docs/game_visualizer.md) |
| Live WebSocket chart | `python live_game.py TICKER` | [Live viewer](docs/live_game_stream.md) |
| Win rates by market price | `python analyze.py win_rate_by_price` | [Win-rate analysis](docs/win_rate_by_price.md) |
| Terminal fill estimates | `python estimate_fill.py` | [Fill estimates](docs/fill_probability.md) |
| Plan trade archive reorganization | `python reorganize_trades.py plan` | [Trade reorganization](docs/trade_reorganization.md) |

The [AI context and documentation index](docs/ai_context.md) has the code map,
data conventions, verification commands, and links to the feature guides.
