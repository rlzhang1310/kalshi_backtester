"""Local dashboard: python -m streamlit run fill_app.py."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.analysis.kalshi.fill_probability import (
    estimate_fill_probability,
    normalize_family,
)
from src.analysis.kalshi.fill_probability_data import prepare_fill_data
from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR
from src.analysis.kalshi.fill_surface_ui import scenario_controls, surface_explorer
from src.analysis.kalshi.no_clv_fill_ui import no_clv_page
from src.analysis.kalshi.odds_moneyness_ui import odds_moneyness_page


ROOT = Path(__file__).resolve().parent


def prepared_families(output_dir: Path) -> dict:
    """Ignore partial runs; expose only matching manifests with saved states."""
    result = {}
    for path in sorted(output_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            family = normalize_family(path.parent.name)
            if (
                manifest.get("family") == family
                and (path.parent / "snapshots.parquet").is_file()
            ):
                result[family] = manifest
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


@st.cache_resource
def preparation_lock():
    # Shared across browser sessions: avoid simultaneous archive scans/writes.
    return threading.Lock()


@st.cache_data(max_entries=4, show_spinner="Loading prepared observations…")
def read_snapshots(path: str, modified: int, size: int):
    return pd.read_parquet(path)


def preparation_panel(output_dir):
    st.subheader("Prepare a ticker family")
    st.caption(
        "Uses your locally collected trades and fetches event timing metadata. Large families can take several minutes. Keep this session open until it finishes."
    )
    with st.form("prepare"):
        family = st.text_input(
            "Ticker family", placeholder="KXNFLGAME", key="new_family"
        )
        with st.expander("Preparation settings"):
            data_dir = st.text_input("Local data directory", str(DEFAULT_DATA_DIR))
            timings = st.text_input(
                "Optional timing CSV or Parquet path",
                help="Leave empty to fetch timing metadata automatically.",
            )
            step = st.number_input(
                "Time sampling interval",
                min_value=0.01,
                max_value=0.5,
                value=0.05,
                step=0.01,
            )
            refresh = st.checkbox(
                "Refresh cached trades and timing metadata",
                help="Enable after collecting more data. Otherwise existing input caches are reused.",
            )
        submitted = st.form_submit_button("Run family", type="primary")
    if submitted:
        try:
            family = normalize_family(family)
        except ValueError as exc:
            st.error(str(exc))
            return
        lock = preparation_lock()
        if not lock.acquire(blocking=False):
            st.warning(
                "Another preparation is running. Wait for it to finish and try again."
            )
            return
        try:
            with st.status(f"Preparing {family}…", expanded=True) as status:
                prepare_fill_data(
                    family=family,
                    output_dir=output_dir,
                    data_dir=Path(data_dir),
                    timings_path=Path(timings) if timings.strip() else None,
                    time_step=step,
                    refresh=refresh,
                    progress=st.write,
                )
                status.update(
                    label=f"{family} prepared", state="complete", expanded=False
                )
            read_snapshots.clear()
            st.session_state["prepared_message"] = (
                f"{family} is ready. Select it below to explore the results."
            )
            # The selector is created later, so this is safe before rerunning.
            st.session_state["family"] = family
        except Exception as exc:
            st.error(f"Preparation failed: {exc}")
            st.info(
                "Check the family and local data paths, then retry. Completed input caches are retained."
            )
            return
        finally:
            lock.release()
        st.rerun()


def game_panel(family, folder, manifest, show_clv=True, compact=False):
    """Entry-point widgets shared by both routes, including downloads/exclusions."""
    path = folder / "snapshots.parquet"
    try:
        stat = path.stat()
        snapshots = read_snapshots(str(path), stat.st_mtime_ns, stat.st_size)
    except Exception as exc:
        st.error(f"Could not load this family's observations: {exc}")
        return
    if compact:
        st.caption(
            f"{snapshots.event_ticker.nunique():,} matches · {len(snapshots):,} historical states"
        )
    else:
        a, b, c = st.columns(3)
        a.metric(
            "Eligible tickers",
            f"{manifest.get('audit_counts', {}).get('included', 0):,}",
        )
        b.metric("Sampled states", f"{len(snapshots):,}")
        c.metric("Matches with observations", f"{snapshots.event_ticker.nunique():,}")
    st.caption(f"Archive coverage through {manifest.get('coverage_end', 'unknown')}")
    ticker_status = {}
    with st.expander("Browse processed tickers and exclusions"):
        summary = folder / "ticker_summary.csv"
        if summary.exists():
            frame = pd.read_csv(summary)
            ticker_status = dict(zip(frame.ticker, frame.status))
            st.dataframe(frame, hide_index=True)
            st.download_button(
                "Download ticker summary",
                frame.to_csv(index=False),
                f"{family}_tickers.csv",
                "text/csv",
            )
    if snapshots.empty:
        st.info(
            "Preparation completed, but no tickers had eligible observations. Review the exclusions above."
        )
        return

    ticker = st.selectbox(
        "Specific ticker (optional)",
        [None, *sorted(set(snapshots.ticker) | set(ticker_status))],
        format_func=lambda value: (
            "Whole family / custom scenario"
            if value is None
            else f"{value} ({ticker_status[value]})"
            if ticker_status.get(value, "included") != "included"
            else value
        ),
        key=f"ticker_{family}",
        help="Selecting a ticker excludes its entire match from the historical reference set. Enter the scenario inputs below independently.",
    )
    if ticker:
        stored = snapshots.loc[snapshots.ticker.eq(ticker), "clv"]
        if stored.empty:
            st.info(
                f"This ticker was excluded: {ticker_status.get(ticker, 'no eligible observations')}. You can still enter a scenario and estimate it using other matches."
            )
        elif show_clv:
            st.caption(
                f"Stored pregame price: {stored.iloc[0]:.1%}. This ticker's match is excluded from estimates."
            )
        else:
            st.caption("This ticker's whole match is excluded from estimates.")
    return str(path), snapshots, ticker


def query_panel(family, folder, manifest):
    context = game_panel(family, folder, manifest)
    if context is not None:
        clv_query_panel(family, *context)


def clv_query_panel(family, path, snapshots, ticker):
    st.subheader("Your scenario")
    clv, x, t, y = scenario_controls("estimator")
    with st.expander("Similarity and support settings"):
        a, b, c = st.columns(3)
        clv_tol = a.number_input("CLV tolerance (percentage points)", 0.0, 100.0, 5.0)
        price_tol = b.number_input("Price tolerance (¢)", 0.0, 100.0, 5.0)
        time_tol = c.number_input(
            "Time tolerance", 0.0, 1.0, 0.025, 0.005, format="%.3f"
        )
        minimum = st.number_input(
            "Minimum reference matches", min_value=1, value=20, step=1
        )
        as_of = st.text_input(
            "Only use matches settled before (optional)",
            placeholder="2026-07-01T00:00:00Z",
        )
    if st.button("Visualize", key="visualize", type="primary"):
        st.session_state["clv_visualizer_settings"] = dict(
            as_of=as_of.strip() or None,
            tolerances=(clv_tol / 100, price_tol / 100, time_tol),
            minimum=int(minimum),
        )
        st.switch_page(st.session_state["clv_visualizer_page"])
    if y >= x:
        st.warning("Set your bid y below the current price x.")
        return
    options = dict(
        family=family,
        ticker=ticker,
        clv=clv / 100,
        current_price=x / 100,
        normalized_time=t,
        clv_tolerance=clv_tol / 100,
        price_tolerance=price_tol / 100,
        time_tolerance=time_tol,
        min_events=int(minimum),
        as_of=as_of.strip() or None,
    )
    try:
        result = estimate_fill_probability(snapshots, bid_price=y / 100, **options)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.subheader("Historical fill estimates")
    if result["status"] == "insufficient_support":
        st.warning(
            f"Only {result['n_events']} reference matches; {minimum} required. Broaden the similarity settings or choose another scenario."
        )
    else:
        a, b, c = st.columns(3)
        a.metric("Conservative · trade below bid", f"{result['p_conservative']:.2%}")
        b.metric("Optimistic · trade at/below bid", f"{result['p_optimistic']:.2%}")
        c.metric("Touch-only gap", f"{100 * result['fill_probability_gap']:.2f} pp")
        if result["status"] == "horizon_ended":
            st.info(
                "At settlement the order's fill horizon has ended, so both estimates are zero."
            )
    st.caption(
        f"Support: {result['n_events']:,} matches · {result['n_tickers']:,} tickers · {result['n_observations']:,} observations. Each matching event gets equal weight."
    )
    st.download_button(
        "Download estimate",
        json.dumps(result, indent=2),
        f"{family}_estimate.json",
        "application/json",
    )

    # Restrict to the relevant family and scenario once before the bid sweep.
    peers = snapshots.loc[
        snapshots.clv.sub(options["clv"]).abs().le(options["clv_tolerance"] + 1e-12)
        & snapshots.current_price.sub(options["current_price"])
        .abs()
        .le(options["price_tolerance"] + 1e-12)
        & snapshots.normalized_time.sub(t).abs().le(time_tol + 1e-12)
    ]
    rows = [
        estimate_fill_probability(peers, bid_price=bid / 100, **options)
        for bid in range(x)
    ]
    curve = pd.DataFrame(rows)
    fig = go.Figure()
    for column, label, color in [
        ("p_conservative", "Conservative", "#2563eb"),
        ("p_optimistic", "Optimistic", "#10b981"),
    ]:
        fig.add_trace(
            go.Scatter(
                x=curve.bid_price * 100,
                y=curve[column],
                name=label,
                line_color=color,
                connectgaps=False,
            )
        )
    fig.add_vline(x=y, line_dash="dot", annotation_text="Your bid")
    fig.update_layout(
        xaxis_title="Bid price (¢)",
        yaxis_title="Historical fill rate",
        yaxis_tickformat=".0%",
        yaxis_range=[0, 1],
        height=350,
        margin=dict(t=30, b=30),
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Move the sliders to compare bids. Gaps in the curves mean insufficient support; the reference set can change with bid price."
    )


def main():
    st.set_page_config(page_title="Bid fill explorer", page_icon="📊", layout="wide")
    st.title("Bid fill explorer")
    st.write(
        "Explore how often similar historical situations reached your bid before trading closed."
    )
    context = None

    def estimates_route():
        if context is not None:
            clv_query_panel(family, *context)

    def clv_route():
        if context is not None:
            path, _, ticker = context
            settings = st.session_state.get(
                "clv_visualizer_settings",
                dict(as_of=None, tolerances=(0.05, 0.05, 0.025), minimum=20),
            )
            with st.sidebar.expander("Similarity and support settings"):
                tolerances = (
                    st.number_input(
                        "CLV tolerance (percentage points)",
                        0.0,
                        100.0,
                        settings["tolerances"][0] * 100,
                        key="clv_page_clv_tol",
                    )
                    / 100,
                    st.number_input(
                        "Price tolerance (¢)",
                        0.0,
                        100.0,
                        settings["tolerances"][1] * 100,
                        key="clv_page_price_tol",
                    )
                    / 100,
                    st.number_input(
                        "Time tolerance",
                        0.0,
                        1.0,
                        settings["tolerances"][2],
                        0.005,
                        format="%.3f",
                        key="clv_page_time_tol",
                    ),
                )
                minimum = int(
                    st.number_input(
                        "Minimum reference matches",
                        min_value=1,
                        value=settings["minimum"],
                        key="clv_page_minimum",
                    )
                )
                cutoff = (
                    st.text_input(
                        "Only use matches settled before (optional)",
                        value=settings["as_of"] or "",
                        key="clv_page_cutoff",
                    ).strip()
                    or None
                )
            st.session_state["clv_visualizer_settings"] = dict(
                as_of=cutoff, tolerances=tolerances, minimum=minimum
            )
            surface_explorer(path, family, ticker, cutoff, tolerances, minimum)

    def no_clv_route():
        if context is not None:
            path, _, ticker = context
            no_clv_page(path, family, ticker)

    def odds_route():
        if context is not None:
            path, _, ticker = context
            odds_moneyness_page(path, family, ticker)

    clv_page = st.Page(clv_route, title="CLV Visualizer", url_path="clv")
    st.session_state["clv_visualizer_page"] = clv_page
    page = st.navigation(
        [
            st.Page(estimates_route, title="Historical estimates", default=True),
            clv_page,
            st.Page(no_clv_route, title="No-CLV Visualizer", url_path="no-clv"),
            st.Page(odds_route, title="Oddness Visualizer", url_path="odds-moneyness"),
        ],
        position="top",
    )
    is_no_clv = page.url_path in ("no-clv", "odds-moneyness")
    with st.sidebar:
        with st.expander("Saved data", expanded=page.url_path == ""):
            output_dir = Path(
                st.text_input(
                    "Results directory", str(ROOT / "output/fill_probability")
                )
            )
            st.button("Reload prepared families")
    with st.expander("Run a new family or refresh existing data"):
        preparation_panel(output_dir)
    if "prepared_message" in st.session_state:
        st.success(st.session_state.pop("prepared_message"))
    if preparation_lock().locked():
        st.info(
            "A family is being prepared in another session. Reload when it finishes."
        )
        return
    families = prepared_families(output_dir)
    if not families:
        st.info(
            "No prepared families yet. Open the preparation panel above and run your first family."
        )
        return
    if st.session_state.get("family") not in families:
        st.session_state["family"] = next(iter(families))
    family = st.selectbox("Prepared ticker family", list(families), key="family")
    context = game_panel(
        family,
        output_dir / family,
        families[family],
        show_clv=not is_no_clv,
        compact=page.url_path != "",
    )
    page.run()


if __name__ == "__main__":
    main()
