# Single-game odds visualizer

`visualize_game.py` builds a standalone interactive odds chart from local
Parquet or the Kalshi API. The companion
`single_game_odds_time_series_visualizer.ipynb` has a game picker with
sport/search filters, game and outcome selection, ticker override, and time
controls. Install `requirements.txt`; the notebook also needs `ipywidgets`,
`ipykernel`, and a Jupyter-capable editor.

## Choose a game

```powershell
python visualize_game.py
```

Choose **1** to paste an event or market ticker, then select local data or
Kalshi API. Choose **2** to search by team or game name, such as `Cincinnati
Cleveland`, and select an outcome. Press Enter at the search prompt to browse
all games. Results accept plain-text searches, `n`/`p` for pages, `/` to clear
the search, and `q` to quit.

You can filter or list the local catalog from the CLI:

```powershell
python visualize_game.py --sport nfl --search Cleveland
python visualize_game.py --list-games --sport mlb --search 26JUN03
```

Or provide a ticker directly:

```powershell
python visualize_game.py KXNFLGAME-25SEP07CINCLE --source local --target CIN --start 2025-09-07T17:00:00Z
python visualize_game.py KXNFLGAME-25SEP07CINCLE-CIN --source api
```

A supplied ticker defaults to API mode. A child market ticker selects its
outcome automatically. `--target` accepts an outcome code, market ticker, or
exact YES subtitle. API mode queries live and historical Kalshi feeds and
deduplicates overlap. It can load games missing from the local archive when
Kalshi still serves their history.

## Local data and price conventions

Local mode reads `data/kalshi/markets/*.parquet` and
`data/kalshi/trades_global_staging/*.parquet`. It filters selected markets
while scanning bounded batches, repairs prices with the backtester's taker-
side rule, and deduplicates trade IDs. Catalog discovery reads market metadata
only and lists recognized game-winner events. A listed event may have no local
trades. The first catalog scan may take a minute; scanning large trade
archives can take longer. The notebook reuses its catalog on later searches.

Local metadata lacks scheduled game start. Without `--start`, local charts
begin at the first stored trade and may include pregame trading. API mode
uses Kalshi's linked game milestone start when available; enter `--start`
otherwise. All times are UTC, including offset-free input timestamps.

For two-outcome events, the selected market's YES price is combined with the
complement of the opposing market's YES price. One-outcome and events with
three or more outcomes use only the selected market's YES trades. Maker/taker
lines retain the reference notebook's approximate fee formula, not exact
charged fees.

## Chart and CLI options

```text
--data-dir D:/archive/data       use another data root
--end 2025-09-07T21:00:00Z       stop at a specific time
--frequency 10s                  change price-line sampling (CLI default: 1s)
--no-resample                    graph raw trades as a step line
--no-fees                        hide maker/taker fee lines
--output-html output/game.html   choose the standalone chart location
--no-show                        save without opening a browser
```

The CLI and notebook save standalone Plotly HTML under `output/`, bundled for
offline viewing. The CLI opens a browser unless `--no-show` is set. The chart
supports zoom, pan, hover, legend toggles, PNG export, and contract volume
bars rebucketed as you zoom. The time axis has a bounded left edge and some
extra room on the right. For an unsettled API game, the browser polls for new
trades every 10 seconds by default; use `--refresh-seconds N` to change that
or `--no-show` for a static snapshot. Keep the terminal running for updates.

For a single local UI across historical, API polling, and WebSocket modes,
see [game_chart_ui.md](game_chart_ui.md).

## Optional sportsbook and play-by-play overlays

Neither overlay is required. Put files at the default paths and rerun:

```text
data/sportsbook/<EVENT_TICKER>.csv
data/playbyplay/<EVENT_TICKER>.csv
```

Alternatively pass `--sportsbook path.csv` or `--playbyplay path.parquet`;
CSV and Parquet are supported. Missing default files and empty files are
skipped. An explicitly supplied missing path or malformed nonempty file
produces an error. Header-only templates are in `examples/game_overlays/`.

Canonical sportsbook columns are `timestamp,book,target_team,implied_prob_raw`
with probability in `[0, 1]`. `raw_odds` may be used for American odds.
`book_last_update` and `book_key` are timestamp/book aliases. Team matching
accepts the Kalshi outcome code, YES subtitle, or full market ticker. Use
`--sportsbook-team "Provider team name"` if provider naming differs. The
chosen team's direct odds are used without vig removal or opponent inversion.
The notebook's SportsDataIO format
(`Sportsbook,Updated,HomeMoneyLine,AwayMoneyLine`) requires
`--sportsbook-side home` or `--sportsbook-side away`.

Canonical play-by-play requires `timestamp`. Optional columns:

| Column | Purpose |
| --- | --- |
| `event_type` | `All plays` (default), `Scoring play`, `Quarter end`, `Period end`, or `2-minute warning` |
| `event_label` | Short label |
| `event_detail` | Play description |
| `score_text` | Score at this event |
| `game_clock_text` | Period and game clock |
| `down_distance_text` | Sport-specific context |

The SportsDataIO NFL format also accepts `Created`, `Description`,
`IsScoringPlay`, and available score/clock fields. Other sports can provide
canonical events. Provider timestamps should include offsets; offset-free
values are UTC. Multi-game files may include `event_ticker` for filtering.
The event track appears only for events inside the trade window. Scoring
markers use the latest trade at or before the event; period events draw
vertical guides. No overlay download or provider credentials are required.

## Python API

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
