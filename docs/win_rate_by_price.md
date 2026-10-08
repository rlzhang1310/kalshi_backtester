# Sports win rate by market price

Run the calibration analysis and choose a sport and market category:

```powershell
python analyze.py win_rate_by_price
```

The category menu includes All, Moneyline, Spread, Score Total, Player Props,
Team Props, and Futures. **All** applies only the sport filter and includes
every finalized YES/NO market for that sport, including mixed and unclassified
event families. Non-player game props such as both-teams-to-score and
exact-score markets are grouped under Team Props.

Trade inputs come from the verified published
`data/kalshi/trades_by_series/` dataset, including scoped `scoped_*.parquet`
appends. The selected sport/category identifies family partitions before the
trade read; an unfiltered run reads all published families. An unfinished
migration causes an actionable error instead of a scan of the mixed archive.

Prompts can be skipped for repeatable runs:

```powershell
python analyze.py win_rate_by_price --sport nfl --category all
python analyze.py win_rate_by_price --sport nfl --category spread
python analyze.py win_rate_by_price --sport ncaa_football --category score_total
python analyze.py win_rate_by_price --sport esports --category moneyline
```

Spelling variants such as `score-total` and `player props` normalize to
underscore names. Filtered outputs include both selections, for example
`output/win_rate_by_price_nfl_spread.csv`, so runs for different categories
do not overwrite each other.

Categories are inferred from Kalshi event-family tickers. Unknown and mixed
multi-leg families are excluded from narrow categories rather than forced
into one. Esports includes recognized League of Legends, Counter-Strike,
Dota 2, Valorant, Call of Duty, Rainbow Six, and Overwatch families; mixed
esports families remain available through All.

The win-rate estimate is weighted by traded maker/taker positions. Its 95%
confidence band treats each distinct traded market ticker as one cluster,
uses a market-clustered standard error, and Student-t degrees of freedom
`unique_markets - 1` at each price. Output includes `unique_markets`,
`degrees_of_freedom`, `clustered_standard_error_pct`,
`confidence_margin_pct`, and `confidence_status`. An interval is unavailable
with fewer than two markets, at a 0%/100% estimate boundary, or when empirical
cluster variance is zero. Intervals are pointwise; the margin is unclipped
even when displayed bounds are clipped to 0-100%.
