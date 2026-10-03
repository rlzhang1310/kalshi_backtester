# Game chart UI

Start the local Streamlit app from the repository root:

```powershell
python -m pip install -r requirements-ui.txt
python -m streamlit run volatility_app.py
```

Choose a mode, enter an event or child-market ticker, then click **Load chart**
or **Start**. The chart shows implied probability, optional approximate
maker/taker fee lines, and contract volume rebucketed for the visible time
range. Price and volume remain interactive: zoom, pan, hover, legend toggles,
and PNG export are available. The theme follows Streamlit's light/dark setting
in its top-right menu.

## Modes

| Mode | Data and updates | Controls |
| --- | --- | --- |
| Historical | Locally stored market and trade Parquet; no network updates | Browse stored games, load a ticker and optional time range |
| Kalshi API | Kalshi live and historical REST trades; settled games are allowed | Load chart, optionally toggle **Update automatically** for unsettled games without an end time |
| Live WebSocket | Initial REST history followed by Kalshi's trade stream; requires an unsettled game | Start/Stop, ticker and outcome |

API polling is off until enabled. **Poll every (seconds)** sets the REST check
interval (default 10 seconds); updates stop after settlement. API charts do
not import trades into the local Parquet archive. WebSocket mode reads
`KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` from the project-root `.env`.
Relative private-key paths are resolved from the project root. Keep the key
local; the browser talks only to the loopback chart server. Switching modes
closes the active poller or WebSocket session. If an event is settled, use
Historical or Kalshi API mode rather than WebSocket.

In **Chart options**, the **Price line display interval** changes only how the
line is drawn; it does not change the volatility calculation. **Volatility
sample cadence (seconds)** sets fixed time buckets for the metric (default 3,
range 1-60). Change it and load/start the chart again to apply it. Changing
games starts a fresh volatility history. The chart keeps the selected legend
visibility and zoom when live data arrives.

The left edge of the time axis is firm; the right edge has extra room for
zooming out. Use the chart's **+**, **-**, and **Fit** controls or the mouse
wheel. **Open chart in new tab** gives the chart its own viewport, with an
optional **Enter browser fullscreen** button. Reload an already-open chart
after changing app source code: its HTML is generated when the chart loads.

Below volume are rolling realized-volatility readings for 1m, 5m, 10m, and
30m. Each shows accumulated movement and a 1-minute-equivalent value. Hover
the historical chart to inspect an as-of reading; live values update with
the chart. See [rolling_volatility.md](rolling_volatility.md) for the formula,
sampling cadence, source, and reasons for N/A.

## Implementation and checks

`volatility_app.py` builds all three modes with
`src/analysis/kalshi/single_game_odds_time_series.py`. The chart is served by
`src/analysis/kalshi/chart_server.py`; live sessions produce replacement
figures without remounting the page. The common calculation is in
`src/analysis/kalshi/rolling_volatility.py`; the browser row in
`src/analysis/kalshi/game_volatility_browser.js` selects precomputed values
for the chart cursor. This keeps the three modes on the same formula and
cadence. Relevant tests are `tests/test_rolling_volatility.py`,
`tests/test_api_chart_mode.py`, `tests/test_live_game_stream.py`, and
`tests/test_chart_server.py`.
