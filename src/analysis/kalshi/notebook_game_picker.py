"""Jupyter controls for selecting a game without editing plotting code."""

from pathlib import Path

from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR, list_local_games
from src.analysis.kalshi.single_game_odds_time_series import (
    figure_display_config,
    visualize_game_odds,
    volume_rebucket_script,
)
from src.analysis.kalshi.util.sports_markets import SPORT_LABELS


def game_picker(data_dir: str | Path = DEFAULT_DATA_DIR):
    """Build optional ipywidgets controls; no data scan or API call until clicked."""
    try:
        import ipywidgets as widgets
        from IPython.display import HTML, display
    except ImportError as exc:
        raise ImportError(
            "Notebook controls require: pip install ipywidgets ipykernel"
        ) from exc

    root = Path(data_dir)
    style = {"description_width": "145px"}
    wide = widgets.Layout(width="95%")
    sport = widgets.Dropdown(
        options=[("All sports", "")]
        + [(label, key) for key, label in SPORT_LABELS.items()],
        description="Sport",
        style=style,
    )
    search = widgets.Text(
        description="Search games",
        placeholder="Team, ticker, or date code such as 25SEP07",
        style=style,
        layout=wide,
    )
    find = widgets.Button(description="Find local games")
    games = widgets.Dropdown(options=[], description="Game", style=style, layout=wide)
    target = widgets.Dropdown(
        options=[("Automatic", None)], description="Outcome", style=style, layout=wide
    )
    ticker = widgets.Text(
        description="Ticker override",
        placeholder="Optional event or child ticker (also works without a local catalog)",
        style=style,
        layout=wide,
    )
    target_override = widgets.Text(
        description="Outcome override",
        placeholder="Optional outcome code or ticker, e.g. CIN",
        style=style,
        layout=wide,
    )
    source = widgets.Dropdown(
        options=[("Local Parquet", "local"), ("Kalshi API", "api")],
        description="Odds source",
        style=style,
    )
    start = widgets.Text(
        description="Start (UTC)",
        placeholder="Optional ISO time, e.g. 2025-09-07T17:00:00Z",
        style=style,
        layout=wide,
    )
    end = widgets.Text(
        description="End (UTC)",
        placeholder="Optional ISO time",
        style=style,
        layout=wide,
    )
    frequency = widgets.Text(
        value="10s",
        description="Sample interval",
        placeholder="Blank for raw trades",
        style=style,
    )
    fees = widgets.Checkbox(value=True, description="Show fee estimates")
    sportsbook = widgets.Text(
        description="Sportsbook file",
        placeholder="Optional; otherwise data/sportsbook/<event>.csv",
        style=style,
        layout=wide,
    )
    playbyplay = widgets.Text(
        description="Play-by-play file",
        placeholder="Optional; otherwise data/playbyplay/<event>.csv",
        style=style,
        layout=wide,
    )
    book_team = widgets.Text(
        description="Sportsbook team",
        placeholder="Optional name mapping for long-form odds",
        style=style,
        layout=wide,
    )
    book_side = widgets.Dropdown(
        options=[("Not needed (long-form)", None), ("Home", "home"), ("Away", "away")],
        description="SportsDataIO side",
        style=style,
    )
    render = widgets.Button(description="Visualize game", button_style="primary")
    status = widgets.Output()
    chart = widgets.Output()
    catalog = None

    def update_targets(change):
        del change
        options = [("Automatic", None)]
        if catalog is not None and games.value:
            row = catalog[catalog["event_ticker"].eq(games.value)].iloc[0]
            options += [(label, label) for label in row.outcomes.split(" / ") if label]
        target.options = options

    def find_games(_):
        nonlocal catalog
        with status:
            status.clear_output(wait=True)
            find.disabled = True
            try:
                if catalog is None:
                    print(
                        "Reading the local market catalog. Large archives may take a minute..."
                    )
                    catalog = list_local_games(root)
                filtered = catalog
                if sport.value:
                    filtered = filtered[filtered["sport"].eq(sport.value)]
                if search.value.strip():
                    text = (
                        filtered["event_ticker"]
                        + " "
                        + filtered["title"]
                        + " "
                        + filtered["outcomes"]
                    )
                    filtered = filtered[
                        text.str.contains(search.value.strip(), case=False, regex=False)
                    ]
                games.options = [
                    (
                        f"{row.event_ticker} | {row.outcomes or row.title}",
                        row.event_ticker,
                    )
                    for row in filtered.itertuples()
                ]
                print(
                    f"{len(filtered):,} games found. Select a game and outcome, then visualize."
                )
            except (ValueError, OSError) as exc:
                print(f"Could not load games: {exc}")
            finally:
                find.disabled = False

    def plot_game(_):
        with chart:
            chart.clear_output(wait=True)
            selected = ticker.value.strip() or games.value
            if not selected:
                print("Choose a game or enter a ticker first.")
                return
            render.disabled = True
            try:
                print(f"Loading {selected} from {source.value}...")
                figure = visualize_game_odds(
                    selected,
                    target=target_override.value.strip()
                    or (None if ticker.value.strip() else target.value),
                    source=source.value,
                    data_dir=root,
                    start_time=start.value.strip() or None,
                    end_time=end.value.strip() or None,
                    resample_frequency=frequency.value.strip() or None,
                    show_fee_lines=fees.value,
                    show=False,
                    width=1200,
                    height=800,
                    sportsbook_path=sportsbook.value.strip() or None,
                    playbyplay_path=playbyplay.value.strip() or None,
                    sportsbook_team=book_team.value.strip() or None,
                    sportsbook_side=book_side.value,
                )
                config = figure_display_config(
                    figure.layout.meta["event_ticker"],
                    figure.layout.meta["target_label"],
                    width=1200,
                    height=800,
                )
                output = Path("output") / (
                    config["toImageButtonOptions"]["filename"] + ".html"
                )
                output.parent.mkdir(parents=True, exist_ok=True)
                figure.write_html(
                    output,
                    include_plotlyjs=True,
                    config=config,
                    post_script=volume_rebucket_script(),
                )
                chart.clear_output(wait=True)
                print(f"Saved interactive chart: {output.resolve()}")
                display(
                    HTML(
                        figure.to_html(
                            full_html=False,
                            include_plotlyjs=True,
                            config=config,
                            post_script=volume_rebucket_script(),
                        )
                    )
                )
            except Exception as exc:
                # Keep widget errors visible in Jupyter rather than its hidden callback log.
                print(f"Could not plot game: {exc}")
            finally:
                render.disabled = False

    find.on_click(find_games)
    games.observe(update_targets, names="value")
    render.on_click(plot_game)
    return widgets.VBox(
        [
            widgets.HTML(
                "<b>Choose a game</b><p>Find games in your local catalog, or enter any Kalshi ticker and choose the API source.</p>"
            ),
            sport,
            search,
            find,
            status,
            games,
            target,
            ticker,
            target_override,
            source,
            start,
            end,
            frequency,
            fees,
            widgets.HTML(
                "<b>Optional overlays</b><p>Leave file paths empty to use matching event files when present. Missing files are optional.</p>"
            ),
            sportsbook,
            book_team,
            book_side,
            playbyplay,
            render,
            chart,
        ]
    )
