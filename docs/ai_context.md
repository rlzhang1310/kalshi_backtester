# AI context and documentation index

This is the technical entry point for working in the repository. Commands and
paths below are relative to the repository root. For usage and implementation
details, follow the guide for the part you are changing.

## Feature guides

| Part | Documentation |
| --- | --- |
| Archive collection and preparation | [data_collection.md](data_collection.md) |
| Fill dashboard and routes | [fill_dashboard.md](fill_dashboard.md) |
| Conditional bid-fill estimator | [fill_probability.md](fill_probability.md) |
| CLV surface model and visualizer | [fill_surface.md](fill_surface.md) |
| No-CLV visualizer | [no_clv_fill.md](no_clv_fill.md) |
| Oddness visualizer | [odds_moneyness.md](odds_moneyness.md) |
| Win rate by market price | [win_rate_by_price.md](win_rate_by_price.md) |
| One-game CLI, notebook, and overlays | [game_visualizer.md](game_visualizer.md) |
| Historical/API/WebSocket chart UI | [game_chart_ui.md](game_chart_ui.md) |
| Rolling volatility calculation | [rolling_volatility.md](rolling_volatility.md) |
| Standalone live WebSocket viewer | [live_game_stream.md](live_game_stream.md) |

## Code map

| Area | Implementation |
| --- | --- |
| Fill dashboard / CLI | `fill_app.py`, `estimate_fill.py` |
| Empirical estimator and preparation | `src/analysis/kalshi/fill_probability.py`, `src/analysis/kalshi/fill_probability_data.py` |
| CLV surface and charts | `src/analysis/kalshi/fill_surface.py`, `src/analysis/kalshi/fill_surface_ui.py` |
| No-CLV model and UI | `src/analysis/kalshi/no_clv_fill.py`, `src/analysis/kalshi/no_clv_fill_ui.py` |
| Odds-moneyness model and UI | `src/analysis/kalshi/odds_moneyness.py`, `src/analysis/kalshi/odds_moneyness_ui.py` |
| Collection and calibration CLIs | `collect_data.py`, `analyze.py` |
| Game CLI / notebook | `visualize_game.py`, `single_game_odds_time_series_visualizer.ipynb` |
| Game data, rendering, overlays | `src/analysis/kalshi/local_game_data.py`, `src/analysis/kalshi/single_game_odds_time_series.py`, `src/analysis/kalshi/game_overlays.py` |
| Game chart UI and server | `volatility_app.py`, `src/analysis/kalshi/chart_server.py` |
| Live game sessions | `src/analysis/kalshi/live_game_stream.py`, `src/analysis/kalshi/toggleable_polling.py`, `live_game.py` |
| Rolling volatility | `src/analysis/kalshi/rolling_volatility.py`, `src/analysis/kalshi/game_volatility_browser.js` |

Local archives live under `data/`; generated datasets and charts live under
`output/`. Do not commit credentials from `.env`.

## Data and modeling conventions

- Families contain events (games) and child markets (outcome contracts).
  Ticker counts are not match counts or independently trained model counts.
- Normalize raw cents using the authoritative taker side: `yes_price` for YES
  takers, `100 - no_price` for NO takers, then divide by 100. Deduplicate IDs.
- CLV here is the last traded YES price strictly before scheduled game start,
  a pregame probability proxy, not a post-trade closing-line return.
- Time uses actual settlement and is retrospective. Close and settlement are
  different; payouts are not trades. Future fill evidence stops before close.
- Touch uses `<= bid`; trade-through uses `< bid`. The empirical estimator
  equally weights matching events and excludes the query ticker's whole match.
- The surface learns the bid-zero value from data by default. Its optional
  legacy boundary is a visualization-only assumption. Preserve empirical
  zeros and missing data; never inject synthetic trades.
- Use bounded archive scans and cached family outputs; do not load the entire
  global trade archive into memory. Ignore `._` resource-fork files.
- Discover prepared families from manifests plus `snapshots.parquet` under
  `output/fill_probability/`. Read current counts/coverage from manifests;
  do not assume every supported sport has been prepared.

## Verification

Install `requirements-ui.txt` for dashboard work. Relevant checks:

```powershell
python -m pytest tests/test_fill_probability.py tests/test_fill_surface.py tests/test_no_clv_fill.py tests/test_odds_moneyness.py tests/test_fill_playback.py tests/test_fill_app.py -q
node --test tests/fill_playback.test.mjs tests/odds_moneyness_playback.test.mjs
python -m ruff check fill_app.py src/analysis/kalshi/fill_surface.py src/analysis/kalshi/fill_surface_ui.py
```

For the game chart UI, include `tests/test_rolling_volatility.py`,
`tests/test_api_chart_mode.py`, and `tests/test_live_game_stream.py`. Use
`python -m pytest -q` for broader regressions. Set `MPLBACKEND=Agg` in
headless environments to avoid Tk initialization. Keep machine-specific
interpreter paths and temporary dependencies out of portable instructions.
