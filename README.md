# sportsbetting

## Single-game odds visualizer

Open an interactive Plotly chart from either a Kalshi event ticker or one of
its child market tickers:

```powershell
python visualize_game.py KXNFLGAME-25SEP07CINCLE
```

Supplying a child ticker selects that outcome automatically. With an event
ticker, the target can also be selected explicitly:

```powershell
python visualize_game.py KXNFLGAME-25SEP07CINCLE --target CIN
```

Useful options:

```text
--start 2025-09-07T17:00:00Z   override the start returned by Kalshi
--end 2025-09-07T21:00:00Z     stop at a specific time
--no-resample                  graph raw trades as a step line
--no-fees                      hide maker/taker fee lines
--output-html output/game.html choose the standalone chart location
--no-show                      build/save without opening a browser
```

When `--start` is omitted, the visualizer queries Kalshi's milestone API and
starts the graph at the linked sports game's `start_date`.
Every run saves the interactive chart under `output/`. With the default browser
renderer, it also prints and opens a link such as
`http://127.0.0.1:50446/` in the terminal.

The same flow is available as a function:

```python
from src.analysis.kalshi.single_game_odds_time_series import visualize_game_odds

figure = visualize_game_odds(
    "KXNFLGAME-25SEP07CINCLE",
)
```

The loader queries both the live and historical Kalshi feeds and de-duplicates
their overlap. This first version intentionally supports two-outcome game
events only; sportsbook and play-by-play overlays are not included.
