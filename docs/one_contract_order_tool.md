# Manual Kalshi quoting desk

This local FastAPI page watches up to two exact market tickers and prepares manual post-only maker **buy** orders. Single posts one contract. Paired posts two independent one-contract orders from one click. The app never decides which markets are economically related and never rebalances or automatically reposts an order. An exchange fill can happen after the page closes.

The **Paper arbitrage** tab observes an explicitly locked cross-market pair using live ask depth and models capped taker buys without sending orders. See [Paper arbitrage observation](paper_arbitrage.md) for its setup, replay tests, fee assumptions, evidence and latency measurements.

## Start the desk

Use Python 3.12 or newer:

```powershell
python -m venv .venv-order-tool
.venv-order-tool\Scripts\python.exe -m pip install -r requirements-order-tool.txt
.venv-order-tool\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://127.0.0.1:8000`. The default environment is demo with trading off. Copy `.env.example` to `.env` and set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` for the chosen environment. The service reads `.env` unless a process environment variable overrides it. Do not paste a private key into the page or chat. Read-only browsing still needs credentials for the authenticated WebSocket feed.

To permit **demo** posting, set demo credentials, `KALSHI_ENV=demo`, and `KALSHI_TRADING_ENABLED=true`, then restart the server. To permit **production** posting, use production credentials, `KALSHI_ENV=production`, and `KALSHI_TRADING_ENABLED=true`, then restart. A production key needs `write` or `write::trade` scope; the startup key check exposes `trade_scope_status` in `/api/status`. Trading opt-in does not grant exchange permissions. The page marks production as real money.

Use one worker without auto-reload. The database defaults to `data/one_contract_orders.sqlite3` and retains app order identities across restarts. It is scoped by environment and API key ID. Keep it when restarting, and do not share it between running service processes. `ORDER_EXPIRY_SECONDS` defaults to 300 seconds for each order's unfilled remainder.

## Choose the orders

1. Load an exact ticker in Market A. Market B is optional for Single and same-market Paired. Each card shows fixed-height YES and NO rows with best bid, best ask, and visible size. Prices are in cents per contract; size is in contracts. Order book depth, spread, and market rules are available in the collapsed details. A missing quote displays a dash and does not mean zero.
2. Select each desired card outcome. Use **Make primary** on a card or **Swap primary** in the ticket to choose the first leg. The other loaded card stays available as the second target. Changing a ticker, outcome, or primary selection clears the current draft and cross-market lock; the ticket shows the loaded other ticker and mapped outcome while you relock. Loading the same ticker again or reconnecting does not submit or cancel orders. Existing orders stay in App orders.
3. Choose **Single** or **Paired**. For Paired, select **Other market** or **Same market** as the second target. Same-market Paired automatically uses the opposite outcome on the primary ticker.
4. For Other market, choose **Same result** or **Opposite result** for the selected outcomes as your own payoff assertion and click **Lock relationship**. This is a manual declaration, not automatic event matching. The actual second buy outcome is:

   | Declared relationship | Actual second buy outcome |
   | --- | --- |
   | Same result | Opposite of Market B's selected outcome |
   | Opposite result | Market B's selected outcome |

   The preview shows the resulting ticker and YES/NO on each leg. Check the market rules before locking. Changing either ticker or selected outcome clears the lock.
5. Set the primary buy limit. **Follow bid** tracks the live primary best bid. If there is no bid, choose **Fixed** and enter a price. Clicking a depth price or **Copy bid** explicitly fixes the draft; **Follow bid** resumes following. A quote update never changes a fixed price.
6. For Paired, enter the spread in cents, up to two decimal places. The second buy limit is `100¢ − primary buy limit − spread`. For example, 49¢ primary and 2¢ spread yields 49¢ second. When both normalized best bids exist and give a nonnegative valid spread, the desk proposes that spread once. Otherwise you must enter one. After locking Other market, **Use second bid** sets the spread from the actual mapped second outcome's current bid. The preview prices are the actual buy limits. A quote gap does not guarantee profit after fees or partial fills.
7. Read **Buy limits to post** beneath the compact live bid/ask strip. It shows the exact ticker, outcome, and limit for each one-contract order, plus the maximum combined limit cost. Both legs must be live, tradable, on their own tick grids, below their own current best asks when an ask is visible, and free of an active or unresolved app order on the same ticker and outcome. The backend repeats these checks on the frozen prices. Invalid drafts stay visible with Post disabled; the app does not silently reprice them. Click Post once.

The backend records both pair legs in one local transaction before sending either request. It starts the two [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2) requests concurrently to preserve each order's post-only and expiration settings. The [batch create endpoint](https://docs.kalshi.com/api-reference/orders/batch-create-orders-v2) exists, but this implementation uses the verified single-order fields for each leg. An exchange acceptance of one leg does **not** ensure acceptance or fill of the other. Both receipts and later fills appear separately. A rejected or unknown leg never causes the app to send a replacement under a new identity.

## Manage and recover orders

App orders remain visible when markets are changed or the feed reconnects. A paired group has **Cancel remaining pair orders**, which sends cancel requests for each still-resting app leg. Each row also has its own Cancel action. Cancellation can race with a fill; wait for the private stream or REST reconciliation to show the final filled and canceled quantities. A filled leg cannot be undone here. The app cannot cancel orders it did not create.

A browser retry after an uncertain response uses the same request ID or pair ID and frozen payload. The SQLite record and exchange client IDs prevent duplicate creates after a lost response. Active or unresolved orders block a new order on the same ticker and outcome, including overlap between Single and Paired. YES and NO on the same ticker have separate slots. If the browser closes, the exchange orders continue until filled, canceled, or expired.

The authenticated private order stream must be subscribed and initial reconciliation complete before posting. Readiness does not require a prior order event. The full books consume every exchange delta; the browser receives a coalesced current view at up to about 30 changed states per second by default (`UI_UPDATE_INTERVAL_MS=33`). Display coalescing does not delay create, cancel, or private order processing. Expand **Connection timing** below App orders to see rolling median/p95 server-envelope-to-display, local click-to-dispatch, and exchange acknowledgment samples. The backend also returns `Server-Timing` for validation and exchange request durations. These are local observations on the same machine, not a guarantee of exchange matching speed.

`/api/status` gives feed and order stream health and a sanitized `feed_error`. `/api/events` supplies the current in-memory view; `/api/markets/{ticker}/summary` gives metadata and book state; `/api/orders` and `/api/pairs/{pair_id}` expose app-owned order records. The app does not poll REST books. A synchronized empty book is valid and shows no price until a bid or ask arrives. A recovering book or private stream pauses new posts, including for live games, until synchronization returns. You can adjust `UI_UPDATE_INTERVAL_MS`, `UI_STREAM_TIMEOUT_SECONDS`, `FEED_HEALTH_TIMEOUT_SECONDS`, `RECONCILE_INTERVAL_SECONDS`, or `METADATA_TTL_SECONDS` if needed.

## Verify

Run the fake-adapter suite without credentials:

```powershell
.venv-order-tool\Scripts\python.exe -m pytest tests/test_one_contract_tool.py -q
node tests/test_manual_quote_ui.cjs
```

The Python suite checks price grids, live books, both outcome mappings in YES/NO directions, both submission modes, concurrent pair dispatch, duplicate request IDs, one-leg rejection or fill, cancellation races, and restart recovery. The Node smoke test checks selection changes, relationship locks, live/fixed drafts, paper tab configuration and reset, and that paper controls never post orders. Real demo posting and canceling require your own demo credentials and an active demo market. Do not use production orders for automated verification.
