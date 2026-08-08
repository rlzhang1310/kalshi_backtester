# sportsbetting

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
