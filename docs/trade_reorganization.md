# Trade archive reorganization

`reorganize_trades.py` builds a **copy** of one frozen local Kalshi global-trade
archive snapshot, partitioned by ticker family. It reads existing local Parquet
files only; it makes no Kalshi API requests. The original files and collector
checkpoints remain untouched. Local game charts, Kalshi analyses, and fill
preparation require the verified published output; API-based live charts remain
independent. Stop the original collector and refresh process before starting a run.

## Why the files must be rewritten

Each source file in `data/kalshi/trades_global_staging/` can hold trades from
multiple families. Renaming a file cannot make it a family partition. The tool
reads all captured `*.parquet` files, including completed `refresh_*.parquet`
files, and writes family-specific data files. It preserves duplicate trade IDs
and duplicate rows. Later global-archive refreshes do **not** update this
published snapshot. After publication and verification, the selected-scope
`collect_data.py trades` command can append family-specific trades directly.

The default output is `data/kalshi/trades_by_series/series_ticker=<FAMILY>/part-*.parquet`,
plus `_metadata/dataset.json`, `family_catalog.parquet`, `validation.json`, and
`source_inventory.json`. If the source already has a `series_ticker` column,
the partition key becomes `ticker_family` so the original column remains
untouched. `UNRESOLVED` keeps rows whose ticker is missing or whose family
cannot be validated. Use a family-specific glob such as
`series_ticker=KXNFLGAME/*.parquet`; a recursive glob would also include the
catalog. Partitions are by family only. Each final file is sorted by full
market ticker, then chronological trade timestamp, then trade ID, ascending
with nulls last. SQL readers still need an explicit `ORDER BY` when order matters.

The current market metadata has `ticker` and `event_ticker`, with no explicit
series column in the inspected files. A prefix is accepted only when the local
market records for that prefix agree between market and event tickers. The
mapping rule is recorded as `market-event-prefix-agreement-v1`; unknown,
conflicting, and unsafe prefixes route to `UNRESOLVED`. Folder names are
uppercase alphanumeric and bounded in length. The source row's ticker value
is never rewritten. Original columns are preserved; absent columns in a
compatible schema variant become null. Incompatible schema types stop the run.

## Plan and disk space

From the repository root, with the project's Python dependencies installed:

```powershell
python reorganize_trades.py plan --source data/kalshi/trades_global_staging --destination data/kalshi/trades_by_series
```

The plan scans Parquet footers, reports source file and row counts, byte size,
schema variants, representative key-field null counts, mapping readiness,
destination and spill free space, and a peak additional-space estimate. It
does not create a published dataset. The user's approximately 250 GB free-space
figure is a planning context, not an assumed capacity. Check the **current**
plan before running: source files, compression, and available disk can change.
The estimate assumes final files and fragments each near the original
compressed size plus another half-source-size allowance for compaction and
spill, then adds the configured reserve. It is intentionally uncertain.
Original archive bytes already consume disk and are not added twice.

On **October 6, 2026**, a read-only preflight of this workspace found
113,056 trade files, 1,130,548,384 rows, and 57,959,126,395 compressed
source bytes (about 54.0 GiB). The destination volume had 274,815,766,528
bytes free (about 256.0 GiB). The approximate peak additional-space estimate,
including a 25 GiB reserve, was 171,741,361,587 bytes (about 160.0 GiB).
Those figures describe that snapshot only; rerun `plan` immediately before a
production migration.

The same preflight found one source schema variant: `trade_id` and `ticker`
strings; `count`, `yes_price`, and `no_price` int64; `taker_side` string; and
`created_time` and `_fetched_at` UTC nanosecond timestamps. Four representative
10,000-row files, including refresh files, had no nulls in `ticker`,
`trade_id`, or `created_time`. Those null counts are sample measurements, not
an archive-wide assertion. The metadata check validated 7,550 family prefixes;
unknown and conflicting mappings remain preserved in `UNRESOLVED` during a run.

The default reserve is 25 GiB per relevant volume, target file size is about
256 MiB compressed, memory limit 1024 MiB, one worker, and 64 source files
per transaction unit. Tune with `--min-free-gib`, `--target-mib`,
`--memory-mib`, `--workers`, `--files-per-unit`, and `--spill`. A smaller
`--files-per-unit` reduces peak in-memory Arrow data, while larger units
produce fewer temporary family fragments. The plan extrapolates a fragment
count from the first unit; it is a rough file-count warning. The tool checks free
space at unit and family boundaries. Another process can still fill a disk
between checks; resume handles incomplete private outputs.

## Run, pause, resume, and verify

```powershell
python reorganize_trades.py run --source data/kalshi/trades_global_staging --destination data/kalshi/trades_by_series --min-free-gib 25
python reorganize_trades.py status --run-id RUN_ID
python reorganize_trades.py resume --run-id RUN_ID
python reorganize_trades.py verify --run-id RUN_ID
```

`run` prints a `RUN_ID`. Stop it with `Ctrl+C`; committed source units and
families are retained. The SQLite manifest and temporary fragments live
under `data/kalshi/.trades_by_series_work/RUN_ID/`. Resume uses its frozen
file list, paths, schema, mapping, and settings. It rejects changed or missing
captured files and does not silently add newly collected files. It discards
only incomplete output owned by the current unit or family, then retries it.
On Windows, short-lived file sharing errors during a private directory rename
are retried before the run stops. If a rename still fails, resume uses the
manifest to discard that uncommitted unit and rebuild it; previously committed
units are skipped.
Single-writer OS locks prevent two migration processes from writing the same
run at once. A completed run reports that it is already complete. Existing
unrelated destinations are never overwritten.

Each distributed unit is checked against its source row count and typed,
order-independent row fingerprint. Each compacted family is checked for row
and ticker counts, fingerprint, folder placement, file sort order, and Parquet
readability. Up to three high-count tickers per family receive an exact row
multiset comparison between committed source fragments and compacted output.
Duplicate multiplicity contributes to both checks. The full-dataset
fingerprint is probabilistic validation, not a proof of exact equality.
`verify` rereads published files against the retained manifest and checks
that captured source files remain unchanged. Published metadata reports
unresolved row counts and validation results. A test fixture compares the
entire source and output as an exact row multiset.

The full production migration is a separate, potentially long operational
run; implementing this tool does not start it. The scoped updater refuses an
absent or unverified published destination. Finish the existing `RUN_ID` with
`resume` and `verify` before starting scoped updates; do not launch another
`run` against its destination. As of October 7, 2026, this workspace's family
dataset had **not** been published: the existing migration manifest was still
running. No production backfill is implied by the scoped updater code.

## Appended scoped trades

After the copy-only snapshot is published, `python collect_data.py trades
--family KXNFLGAME --since 2026-09-01T00:00:00Z` establishes an explicit
checked range. Use `--ticker FULL_MARKET_TICKER` to ingest exactly one market.
Routine runs omit `--since`; bounded repairs use both `--since` and `--until`.
See [data_collection.md](data_collection.md#selected-family-or-market-trade-updates)
for full commands and recovery behavior.

The updater puts immutable `scoped_*.parquet` batches beside that family's
`part-*.parquet`, with exactly the published schema. A temporary file is
renamed into place before the cursor/checkpoint moves. On rerun, a published
batch missing from the checkpoint is recovered and its IDs deduplicated. The
single `_metadata/scoped_checkpoint.json` tracks discovery, per-market checked
ranges, in-progress endpoints/cursors, and appended files. The OS lock is
shared by all scoped selections, so a family and one of its markets cannot
write concurrently. Source `dataset.json`, `family_catalog.parquet`, and
`validation.json` remain provenance for the original snapshot only; they do
not certify appended rows or assert complete Kalshi history. A copied row's
timestamp is not a coverage boundary. The first adoption therefore requires
an explicit start when none is known.

The shared reader in `src/analysis/kalshi/util/trades.py` includes base and
scoped files and validates the published metadata. Single-game and fill paths
open one family partition. Sport calibration opens only the families selected
by its market filter; all-market analyses scan all published partitions. It
never unions the raw staging archive with its copied snapshot and does not
remove duplicate rows already present in the base. Physical files are each
sorted, but the family as a whole has no guaranteed order; SQL callers should
specify `ORDER BY`. Fill preparation detects new files and successful empty
checked windows in the scoped checkpoint. Markets without sufficient checked
ranges remain in its audit as incomplete. Retire the old archive only after
the global collector and migration no longer depend on it.

| Consumer | Trade files read |
| --- | --- |
| Local game chart and notebook | Selected event's family partition, then exact market/time filters |
| Fill preparation and dashboard cache build | Selected family partition; prepared observations remain under `output/fill_probability/` |
| Sport/category calibration | Families present in the selected finalized markets |
| All-market Kalshi and Kalshi/Polymarket analyses | All published family partitions |
| Trade inspection script | Required `--family` or full `--ticker` selection |

Market metadata remains under `data/kalshi/markets/`; the family migration
organizes trades, not market records. Kalshi API and WebSocket live modes still
read the exchange directly. The older `data/kalshi/trades/` per-ticker collector
output and mixed global staging files are not read by these historical analyses.
