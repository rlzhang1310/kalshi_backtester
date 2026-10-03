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

Run `python collect_data.py` again and choose **Kalshi Markets** to download
market metadata into `data/kalshi/markets/`. To prepare data for the fill
visualizers, start `fill_app.py`, open **Run a new family or refresh existing
data**, choose a sports ticker family, and click **Run family**. Prepared family
outputs live under `output/fill_probability/` and can be reused on later runs.

The game chart UI's Kalshi API mode does not import trades into this local
archive. A game may be listed in the local market catalog but lack collected
trades; use API mode if Kalshi still serves its history. See
[game_visualizer.md](game_visualizer.md) for bounded local scans and catalog
behavior, and [fill_probability.md](fill_probability.md) for family preparation
and the estimator's data requirements.
