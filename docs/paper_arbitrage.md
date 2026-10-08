# Paper arbitrage observation

The **Paper arbitrage** tab observes one explicitly locked pair from the Manual desk. It reads the backend's full live books after each applicable WebSocket update and records what a capped, taker buy on each leg *would* have submitted. It has no exchange order transport. The Manual desk remains the only route that can create or cancel a real order.

## Use it

1. Start the desk as described in [the manual desk guide](one_contract_order_tool.md). The authenticated WebSocket feed needs credentials for live books, including when trading is disabled. The paper detector does not require the private order stream to be ready.
2. Load two exact tickers in the Manual desk, select the relevant YES or NO outcome on each, choose **Paired → Other market**, select **Same result** or **Opposite result**, and **Lock relationship**. That lock is your assertion about the payoff relationship. The desk cannot verify that the two claims together pay exactly $1 in every case, including ties, voids, postponements and early resolution.
3. Open **Paper arbitrage**, set the minimum net profit and safety margin in cents, and optionally change the modeled arrival delays. The first UI test is one contract per leg; the backend API accepts a two-decimal quantity from 0.01 to 100. Both thresholds default to zero and are total dollars for the pair.
4. Click **Use locked pair**, then **Start observing**. The relationship lock is your manual payoff assertion; the desk cannot prove settlement equivalence. **Stop** ends observation; it does not cancel any manual order.

The tab shows both complementary orientations. For a **Same result** lock, it evaluates primary YES plus other NO (or the opposite orientation). For **Opposite result**, it evaluates the two selected outcomes (and their opposites). These are payoff assertions, not automated market matching. The UI shows the actual ticker and outcome in each leg.

**Observed opportunity** requires a strictly positive net edge, full visible depth, synchronized books, valid market and fee metadata, and the relationship assertion. **Would submit** further requires the configured profit and margin and a profitable worst-case price cap. It is one paper IOC attempt per continuous opportunity episode. **Simulated result** estimates fills at each configured delay using the then-visible book and a separate shadow book per scenario. Partial or one-leg modeled fills show unmatched exposure, without a paired payout. These estimates do not establish exchange matching priority, actual fills, or realized profit.

The **Simulated P&L** view totals paired modeled net separately for each arrival delay. It also counts unmatched and missed attempts and shows the modeled outlay tied to unmatched fills. An unmatched outlay is exposure, not a loss or settled P&L. Totals cover the configured paper session even when older detail rows fall off the recent-results list; reconfiguring the pair resets them.

The calculation for quantity `q` is `q − ask cost A − ask cost B − taker fee bound A − taker fee bound B`. Each ask is derived from the same market's opposite-side bid, and each leg walks enough visible depth to fill `q`. App-owned resting size is excluded. Public book data cannot identify orders placed elsewhere by the same account. An empty or recovering book, lost sequence, stale metadata, invalid price grid, missing fee model, and evidence overflow block qualification. A quiet, synchronized book remains valid.

Fee modeling uses the current [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf), the [series fee type and multiplier](https://docs.kalshi.com/api-reference/market/get-series), and any [event override](https://docs.kalshi.com/api-reference/events/get-event). The UI uses conservative cent rounding plus an allowance for fractional fill fragmentation. It does not model any separate broker or FCM surcharge, so simulated net may be overstated for those accounts. Market and fee metadata are refreshed outside the update path at `METADATA_TTL_SECONDS` (30 seconds by default); a changed fee, status, grid, or ruleset resets paper episodes and reconfigures the model. Confirm the published fee schedule when running the detector after an exchange fee change.

Evidence is a bounded JSONL session in `data/paper_evidence/` by default, adjacent to `APP_DB_PATH`. It records configuration, per-decision depth and revisions, observed edges, caps, proposed intents, and delayed outcomes. Each file is capped at 25 MB. Queue or file overflow is displayed and blocks further qualification. The `GET /api/paper` response exposes the current state; only `/api/paper/*` POST routes configure or control paper observation. The paper code never calls the exchange's create, amend, or cancel methods.

## Replay and measure

With the desk dependencies installed, run the deterministic synthetic replay and local benchmark:

```powershell
python -m pytest tests/test_one_contract_tool.py tests/test_paper_arbitrage.py -q
python -m scripts.bench_paper_arbitrage
node tests/test_manual_quote_ui.cjs
```

The paper tests cover both relationship mappings and YES/NO conversions, a qualifying edge, fee-erased and exact-zero edges, fractional and sub-cent depth, price grids, own liquidity, quiet books, gaps and reconnection, changed configuration, episode deduplication, disappearance, delayed one-leg fills, and zero exchange mutations even when manual trading is enabled.

On this Windows development workspace, a 100-level-per-side synthetic replay ran 5,000 book updates followed by 400 alternating qualifying updates. The run produced 201 intent latency samples and no evidence overflow:

| Local interval | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| Receive → book (5,000-update phase) | 0.010 ms | 0.015 ms | 0.022 ms |
| Book → decision (5,000-update phase) | 0.062 ms | 0.090 ms | 0.122 ms |
| Decision → paper intent (qualifying phase) | 0.015 ms | 0.032 ms | 0.071 ms |

The benchmark starts its receive clock immediately before applying a synthetic delta. Production's receive clock starts when a WebSocket frame arrives, so it also includes JSON parsing and dispatch. These figures exclude network delay, exchange matching, browser rendering, and real order submission. They are sample measurements, not latency guarantees. The book update currently copies both side maps and checks the best YES/NO bids for integrity; the detector then walks only the required ask levels and queues evidence for a background writer. The benchmark makes these local costs reproducible before considering a larger architecture change.

## Source rules

- [Kalshi orderbook response semantics](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Fixed-point prices and quantities](https://docs.kalshi.com/getting_started/fixed_point_migration)
- [Fee rounding and account precision](https://docs.kalshi.com/getting_started/fee_rounding)
- [Current published fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Order V2 time-in-force fields](https://docs.kalshi.com/api-reference/orders/create-order-v2)
