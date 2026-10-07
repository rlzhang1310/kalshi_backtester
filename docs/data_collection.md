# Historical data collection

The loader is inspired by [Jon Becker's prediction-market-analysis project](https://github.com/Jon-Becker/prediction-market-analysis).
Run from the repository root with Python 3.11 or newer:

```powershell
python -m pip install -r requirements.txt
python collect_data.py
```

Choose **Kalshi Global Trades** to download trade history. Trades stream into
Parquet files under `data/kalshi/trades_global_staging/` until both the
historical and live feeds have been read. Press `Ctrl+C` to pause; run the same
menu option again to resume from the saved checkpoint. Completed feeds are
skipped.

After the initial global backfill finishes, choose **Kalshi Global Trades
Refresh** from the same menu to append trades added since the last run. The
original `checkpoint.json` and its Parquet batches remain unchanged. The
refresh records its own state in
`data/kalshi/trades_global_staging/refresh_checkpoint.json` and writes new
`refresh_*.parquet` batches in that folder. It starts one week before the
earliest original feed completion (or one week before the last successful
refresh), fetches both live and historical feeds in a fixed UTC time range,
and checks `trade_id` against the overlapping archive before appending.
The overlap covers the handoff between Kalshi's live and historical tiers;
it is not a guarantee against older gaps in the original backfill.

If interrupted, choose the refresh option again. A saved cursor advances
only after the corresponding batch reaches disk. Keep just one refresh
process running at a time. If a process is forcibly killed and a
`refresh.lock` file remains, verify no refresh is still running before
removing that lock file. Do not change the original backfill's `completed`
flags to refresh data: its finished cursors are empty, so that restarts the
full feed. A successful refresh prints the number of pages and new trades
for each source. Rerunning it is safe; duplicate trade IDs are not appended.
The initial refresh may take time and temporary disk space while it checks
IDs in the existing archive; it does not rewrite those Parquet files.

Run `python collect_data.py` again and choose **Kalshi Markets** to download
market metadata into `data/kalshi/markets/`. To prepare data for the fill
visualizers, start `fill_app.py`, open **Run a new family or refresh existing
data**, choose a sports ticker family, and click **Run family**. Prepared family
outputs live under `output/fill_probability/` and can be reused on later runs.

Collecting or refreshing global trades only changes files in `data/`; it does
not update an already prepared fill family. After collecting more trades or
markets, run that family's preparation again with **Refresh cached trades and
timing metadata** checked (or use `python estimate_fill.py prepare --family
FAMILY --refresh`). This rescans the current local market and global trade
Parquet files, fetches timing metadata again, and rebuilds the family's saved
observations. Without this option, preparation reuses its cached market list,
trade extract, and timing metadata even when new archive files exist. Each fill
dashboard page reads the saved observations, not the global trade files directly.
See [fill_probability.md](fill_probability.md#what-data-the-fill-models-load)
for the full inclusion rules and a cache freshness check.

The game chart UI's Kalshi API mode does not import trades into this local
archive. A game may be listed in the local market catalog but lack collected
trades; use API mode if Kalshi still serves its history. See
[game_visualizer.md](game_visualizer.md) for bounded local scans and catalog
behavior, and [fill_probability.md](fill_probability.md) for family preparation
and the estimator's data requirements.

For a separate, copy-only organization of a captured global-trade snapshot
into family partitions, see [trade_reorganization.md](trade_reorganization.md).
It does not change the collector, current readers, or prepared fill data, and
new refresh batches do not automatically enter that organized snapshot.
