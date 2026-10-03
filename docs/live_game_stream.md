# Standalone live WebSocket viewer

For a single unsettled game outside the Streamlit UI, run:

```powershell
python live_game.py KXATPCHALLENGERMATCH-26OCT01BASKRU
```

This viewer starts from Kalshi REST trade history, subscribes to the
selected game's market trades over an authenticated WebSocket, and backfills
after disconnects. It stops updating after settlement. Keep the terminal
running; press `Ctrl+C` to stop. At least one trade must be available before
the initial chart can be built. A settled game can be opened through
`visualize_game.py` or the [game chart UI](game_chart_ui.md).

Set these in the project-root `.env`:

```text
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=path/to/private-key.pem
```

Relative private-key paths are resolved from the project root. The Kalshi
trade feed needs WebSocket authentication even though the trades are public.
Keep the private key on your computer; the chart browser connects only to the
local loopback server. See Kalshi's [WebSocket authentication](https://docs.kalshi.com/websockets/websocket-connection)
and [public trade feed](https://docs.kalshi.com/websockets/public-trades).

`live_game.py` accepts `--target`, `--start`, `--data-dir`, `--frequency`,
and `--no-fees`. Its chart shows odds, optional approximate fee lines, and
zoom-aware volume. For the multi-mode Streamlit UI, including rolling
volatility and a selectable cadence, run `volatility_app.py` instead.
