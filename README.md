# sportsbetting

## Conditional bid fill probability

Run `python estimate_fill.py` for a menu to prepare a ticker family and enter
CLV, current price `x`, normalized time `t`, and bid price `y < x`.
It reports an optimistic estimate (future touch), a conservative estimate
(future trade-through), and their touch-only probability gap. Preparation
normalizes every eligible ticker from game start to actual settlement and
saves exclusions in a per-ticker audit.

See [the estimator design and usage](docs/fill_probability.md) for the formulas,
data sources, conditioning method, and the retrospective-time limitation.

## Sports win rate by price

Run the calibration analysis and choose a sport followed by a market category
in the terminal:

```powershell
python analyze.py win_rate_by_price
```

The category menu includes:

- All
- Moneyline
- Spread
- Score Total
- Player Props
- Team Props
- Futures

Choosing All applies only the sport filter and includes every finalized yes/no
market for that sport, including mixed or not-yet-classified event families.

You can also skip the prompts, which is useful for repeatable runs:

```powershell
python analyze.py win_rate_by_price --sport nfl --category all
python analyze.py win_rate_by_price --sport nfl --category spread
python analyze.py win_rate_by_price --sport ncaa_football --category score_total
python analyze.py win_rate_by_price --sport esports --category moneyline
```

`score-total`, `player props`, and similar spelling variants are normalized to
their canonical underscore names. Filtered files include both selections, such
as `output/win_rate_by_price_nfl_spread.csv`, so one run does not overwrite
another category.

Market categories are inferred from Kalshi's event-family ticker. Unknown and
mixed multi-leg families are intentionally excluded from the narrower filters
rather than being forced into a misleading category. Non-player game props
such as both-teams-to-score and exact-score markets are grouped under Team
Props.

Esports is available alongside the traditional sports. Its recognized event
families include League of Legends, Counter-Strike, Dota 2, Valorant, Call of
Duty, Rainbow Six, and Overwatch. Mixed esports families remain available
through the All category even when they cannot be assigned safely to a narrower
market category.

The win-rate estimate remains weighted by traded maker/taker positions. Its
95% confidence band treats each distinct traded market ticker as one cluster,
uses a market-clustered standard error, and uses Student-t degrees of freedom
equal to `unique_markets - 1` at each price. The CSV includes
`unique_markets`, `degrees_of_freedom`, `clustered_standard_error_pct`, and
`confidence_margin_pct`. The additive `confidence_status` column explains when
an interval is unavailable because fewer than two markets contribute, the
estimate is at a 0%/100% boundary, or the empirical cluster variance is zero.
Intervals are pointwise; the reported margin is unclipped even though displayed
bounds are clipped to 0–100%.

## Single-game odds visualizer

Open `single_game_odds_time_series_visualizer.ipynb` and run its cells for a
game picker with sport/search filters, game/outcome selections, a ticker
override, and time-window controls. Install the normal `requirements.txt` in
your Python environment; the notebook additionally needs
`pip install ipywidgets ipykernel` and a Jupyter-capable editor.

To choose a game without putting its ticker in command-line parameters, open
the terminal menu:

```powershell
python visualize_game.py
```

Choose **1** to paste a game/market ticker, then select local data or the
Kalshi API. Choose **2** to type a team or game name (for example,
`Cincinnati Cleveland`) and select a matching game and outcome. Press Enter
at the search prompt to browse all games. The results accept plain-text
searches, `n`/`p` for pages, `/` to clear the search, and `q` to quit.

Optional filters and catalog listing are also available:

```powershell
python visualize_game.py --sport nfl --search Cleveland
python visualize_game.py --list-games --sport mlb --search 26JUN03
```

The game-name search uses the local Parquet catalog. You can
also provide an event or child market ticker directly:

```powershell
python visualize_game.py KXNFLGAME-25SEP07CINCLE --source local --target CIN --start 2025-09-07T17:00:00Z
python visualize_game.py KXNFLGAME-25SEP07CINCLE-CIN --source api
```

A supplied ticker defaults to the existing API mode. A child ticker selects
its outcome automatically; `--target` accepts an outcome code, ticker, or
exact YES subtitle. API mode queries both live and historical Kalshi feeds
and de-duplicates overlap. Games absent from the local archive can be loaded
through this mode, subject to Kalshi's data availability.

Local mode reads `data/kalshi/markets/*.parquet` and
`data/kalshi/trades_global_staging/*.parquet`. It filters to the selected
markets while scanning bounded batches of files, normalizes cents to
probabilities, repairs prices using the backtester's taker-side rule, and
de-duplicates trade IDs. Catalog discovery reads market metadata only and
lists recognized game-winner events. A listed event may have no locally
collected trades; use API mode in that case. Large archives can take a minute
for the first catalog scan and longer for trade scans. The notebook reuses
its catalog for subsequent searches.

Local metadata has no scheduled game start. Without `--start`, local charts
begin at the first stored trade and include pregame trading. API mode uses
Kalshi's linked game milestone start; enter `--start` if unavailable. All
times are UTC, including timestamps supplied without an offset.

Two-outcome events combine the selected market's YES price with the
complement of the other market. One-outcome and three-plus-outcome events
use only the selected market's YES trades. The maker/taker lines retain the
reference notebook's approximate fee formula, not exact charged fees.

Additional options:

```text
--data-dir D:/archive/data       use another data root
--end 2025-09-07T21:00:00Z       stop at a specific time
--frequency 10s                  change the sampling interval (CLI default: 1s)
--no-resample                    graph raw trades as a step line
--no-fees                        hide maker/taker fee lines
--output-html output/game.html   choose the standalone chart location
--no-show                        save without opening a browser
```

The CLI and notebook save standalone interactive HTML under `output/`, with
Plotly bundled for offline viewing. The CLI opens a browser unless `--no-show`
is given. Zoom, pan, hover, legend toggles, and PNG export remain available.

### Optional sportsbook and play-by-play data

Neither overlay is required. Drop CSVs into these paths and rerun the chart:

```text
data/sportsbook/<EVENT_TICKER>.csv
data/playbyplay/<EVENT_TICKER>.csv
```

Or pass `--sportsbook path.csv` / `--playbyplay path.parquet` (CSV and Parquet
are supported). Absent default files and empty files are skipped. An explicit
missing path or malformed nonempty data produces an actionable error.
Header-only templates are in `examples/game_overlays/`.

Canonical sportsbook columns are `timestamp,book,target_team,implied_prob_raw`
with probability in `[0, 1]`. Alternatively use `raw_odds` for American odds.
`book_last_update` and `book_key` are accepted timestamp/book aliases. Team
matching uses the Kalshi outcome code, YES subtitle, or full market ticker;
use `--sportsbook-team "Provider team name"` to map a different name. The
chosen team's direct odds are used, without removing vig or inverting the
opponent's odds. The reference notebook's SportsDataIO format
(`Sportsbook,Updated,HomeMoneyLine,AwayMoneyLine`) is supported with the
explicit mapping `--sportsbook-side home` or `--sportsbook-side away`.

Canonical play-by-play requires `timestamp`. Optional columns:

| Column | Purpose |
| --- | --- |
| `event_type` | `All plays` (default), `Scoring play`, `Quarter end`, `Period end`, or `2-minute warning` |
| `event_label` | Short label |
| `event_detail` | Play description |
| `score_text` | Score at this event |
| `game_clock_text` | Period and game clock |
| `down_distance_text` | Optional sport-specific context |

The SportsDataIO NFL format from the reference notebook also works using
`Created`, `Description`, `IsScoringPlay`, and any available score/clock fields.
Other sports can provide canonical events. Use timestamp offsets from the
provider; offset-free timestamps are interpreted as UTC. Files containing
multiple games can include `event_ticker` to filter rows to the chosen game.

The event track appears only when events fall within the chart's trade window.
Scoring markers use the latest trade at or before the event. Period events
draw vertical guides. No play-by-play download or provider credentials are
needed; populate the files when that data becomes available.

### Python API

```python
from src.analysis.kalshi.local_game_data import list_local_games
from src.analysis.kalshi.single_game_odds_time_series import visualize_game_odds

games = list_local_games(sport="nfl", search="Cleveland")
figure = visualize_game_odds(
    "KXNFLGAME-25SEP07CINCLE",
    source="local",
    target="CIN",
    start_time="2025-09-07T17:00:00Z",
    resample_frequency="10s",
    show=False,
)
figure.show()
```
