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

The older **Kalshi Trades** per-ticker menu option writes
`data/kalshi/trades/`; published family readers do not use that legacy output.
Use the scoped `trades` command below after migration for new local analysis data.

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

Global collection writes only to staging. Local analysis and fill preparation
read the verified family dataset after migration; global refresh batches do
not enter it automatically. Scoped `collect_data.py trades` updates append to
that dataset. Run `python estimate_fill.py prepare --family FAMILY` after a
scoped update. Preparation detects changed family files or checked ranges and
rebuilds that family's saved observations. Use `--refresh` after market
metadata changes without new family trades. Each fill dashboard page reads
the saved observations, not trade Parquet directly.
See [fill_probability.md](fill_probability.md#what-data-the-fill-models-load)
for the full inclusion rules and a cache freshness check.

The game chart UI's Kalshi API mode does not import trades into the local
dataset. Local mode reads the selected family partition only. A game may be
listed in the local market catalog but lack collected trades; use API mode if
Kalshi still serves its history. See
[game_visualizer.md](game_visualizer.md) for bounded local scans and catalog
behavior, and [fill_probability.md](fill_probability.md) for family preparation
and the estimator's data requirements.

## Selected family or market trade updates

The `trades` subcommand appends only a selected Kalshi series or full market
to a **verified, published** family dataset. The initial copy-only migration
must have finished and passed verification first; an in-progress migration's
private output is not usable. See [trade_reorganization.md](trade_reorganization.md)
for `status`, `resume`, and `verify`. The updater does not start a global
migration or scan the mixed archive for every run.

```powershell
# First adoption: choose a deliberate UTC boundary because the copied archive
# does not prove complete Kalshi history or a reliable captured-through time.
python collect_data.py trades --family KXNFLGAME --since 2026-09-01T00:00:00Z

# Subsequent update: discover newly listed markets and check each market's
# recent range with a seven-day overlap.
python collect_data.py trades --family KXNFLGAME

# One contract only, or a read-only preview.
python collect_data.py trades --ticker FULL_MARKET_TICKER --since 2026-09-01T00:00:00Z
python collect_data.py trades --family KXNFLGAME --dry-run

# Explicit historical repair; both endpoints are checked and duplicate IDs
# already in the family partition are skipped.
python collect_data.py trades --family KXNFLGAME --since 2026-09-01T00:00:00Z --until 2026-10-01T00:00:00Z
```

The first `--since` is retained as the adoption start for new markets later
discovered in that family. An exact market without prior coverage needs its
own `--since`. For fill preparation, choose a start before the earliest
relevant observed print and run through settlement for each desired market;
the copy-only snapshot alone has no proven checked range. Each successful
window, including an empty one, is checkpointed
per full ticker only after both live and historical trade feeds finish. The
run freezes its end time; reruns resume interrupted windows, and an expired
cursor replays its bounded window. An error leaves that market pending and
returns a nonzero exit status. Other markets can still finish. A full market
ticker is required after `--ticker`; bad selections never fall back to global
collection. `--min-free-gib` controls the disk reserve (25 GiB by default).

The authoritative files are
`data/kalshi/trades_by_series/series_ticker=<FAMILY>/part-*.parquet` from the
snapshot and `scoped_*.parquet` added by this command. A source dataset with
its own `series_ticker` column uses `ticker_family=<FAMILY>` instead. The
updater keeps its per-market ranges, discovery progress, and appended-file
manifest in `_metadata/scoped_checkpoint.json`; the original migration
manifest and validation report still describe only the copy-only snapshot.
The seven-day overlap can catch recent late trades, but an older omission
requires an explicit `--since`/`--until` repair. Existing duplicate rows in
the baseline remain untouched. Do not run a second importer against these
family partitions while the scoped updater is active.

Analysis code can call `create_family_trades_view(con, dataset_dir, family)`
from `src.analysis.kalshi.util.trades`, then query `analysis_trades`, for
example `SELECT * FROM analysis_trades WHERE ticker = ? ORDER BY
created_time, trade_id`. Game charts and fill preparation select one family;
sport calibration selects only the matching families; whole-market analyses
use the published dataset across families. These readers include both baseline
and appended files. Prepared fill observations remain separate, and the
copy-only baseline does not establish complete history. The fill audit uses
per-market checked ranges from `_metadata/scoped_checkpoint.json` to decide
whether a market's trade horizon is covered.
