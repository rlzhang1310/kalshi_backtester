# Fill dashboard

Launch from the repository root:

```powershell
python -m pip install -r requirements-ui.txt
python -m streamlit run fill_app.py
```

The dashboard uses locally collected trades and prepared ticker families.
Choose a prepared family, optionally select an individual ticker, and adjust
pregame probability (called CLV here), current YES price, elapsed time, and
bid price. Results and the bid comparison chart update automatically.
**Run a new family or refresh existing data** prepares another family with
visible progress; completed families appear in the dropdown. The ticker table
includes excluded contracts and reasons. Selecting a ticker excludes its
whole match from the reference data; scenario inputs remain yours to set.
After collecting new trades or market metadata, check **Refresh cached trades
and timing metadata** when running an existing family. Otherwise the dashboard
continues using its saved family observations. The [loading guide](fill_probability.md#what-data-the-fill-models-load)
explains which archive records become model data and how to check cache freshness.

| Route | View | Detailed guide |
| --- | --- | --- |
| `/` | Historical estimates and family controls | [fill_probability.md](fill_probability.md) |
| `/clv` | CLV Visualizer: 3D surface, heatmap, linked bid curve | [fill_surface.md](fill_surface.md) |
| `/no-clv` | No-CLV Visualizer: pooled heatmap, time surface, series, snapshots and playback | [no_clv_fill.md](no_clv_fill.md) |
| `/odds-moneyness` | Oddness Visualizer: log-odds distance and elapsed time | [odds_moneyness.md](odds_moneyness.md) |

The top navigation changes pages. Scenario and playback controls stay in the
sidebar. No-CLV playback uses cached browser frames and updates the charts
when you release the time slider; Play/Pause is also in the sidebar. The CLV
surface learns its zero-bid boundary from data, while the No-CLV page uses an
explicit `1 - current price` zero-bid assumption. Oddness uses
`logit(current price) - logit(bid)` and normalized elapsed time; changing a
scenario moves the selected point without rebuilding its chart grids.

These are historical price-reaching estimates, not guaranteed order fills.
Elapsed time uses realized settlement and is retrospective. Read the linked
model guides before treating results as live predictions.

For terminal use, `python estimate_fill.py` opens a menu to prepare a ticker
family and query CLV, current price `x`, normalized time `t`, and bid `y < x`.
It reports optimistic future touch, conservative future trade-through, and
their gap. See [fill_probability.md](fill_probability.md) for CLI flags and
formulas.
