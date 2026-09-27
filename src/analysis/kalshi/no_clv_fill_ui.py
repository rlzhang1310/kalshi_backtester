"""Full-page no-CLV workspace with one shared state and exact model calls."""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .fill_surface import reference_data
from .fill_surface_ui import BOUNDARY
from .fill_playback import playback_controls
from .no_clv_fill import MODEL_VERSION, fit_model, heatmap_grid, time_grid

SNAPSHOTS = (0.1, 0.3, 0.5, 0.7, 0.9)


@st.cache_resource(
    max_entries=6, show_spinner="Fitting the no-CLV model and checking held-out games…"
)
def cached_model(
    path,
    modified,
    size,
    family,
    ticker,
    cutoff,
    outcome,
    smoothing,
    version=MODEL_VERSION,
):
    source = reference_data(pd.read_parquet(path), family, ticker, cutoff)
    return fit_model(source, outcome, smoothing)


@st.cache_data(max_entries=32, show_spinner=False)
def cached_heatmap(signature, time, tolerances, minimum, resolution, _model):
    return heatmap_grid(_model, time, tolerances, minimum, resolution)


@st.cache_data(max_entries=12, show_spinner=False)
def cached_time_grid(signature, price, tolerances, minimum, resolution, _model):
    return time_grid(_model, price, tolerances, minimum, resolution)


@st.cache_data(max_entries=12, show_spinner=False)
def cached_raw_curve(signature, price, bid, tolerances, _model):
    times = np.linspace(0, 1, 21)
    return _model.empirical(
        np.column_stack([np.full(len(times), price), times, np.full(len(times), bid)]),
        tolerances,
    )


def update_scenario(name):
    st.session_state["no_clv_scenario"][name] = st.session_state[f"no_clv_{name}"]


def scenario_controls():
    state = st.session_state.setdefault("no_clv_scenario", dict(p=60, t=0.5, b=55))
    for name, label, low, high, step in [
        ("p", "Current price (¢)", 1, 100, 1),
        ("b", "Bid (¢)", 0, 99, 1),
    ]:
        key = f"no_clv_{name}"
        st.session_state[key] = state[name]
        st.slider(
            label,
            low,
            high,
            step=step,
            key=key,
            on_change=update_scenario,
            args=(name,),
        )
    return state["p"] / 100, state["t"], state["b"] / 100


def custom_data(grid, time=None, price=None):
    count = np.where(grid["synthetic"], np.nan, grid["n_events"].astype(float))
    observations = np.where(
        grid["synthetic"], np.nan, grid["n_observations"].astype(float)
    )
    fixed = np.full(count.shape, time if time is not None else price)
    return np.stack(
        [fixed, count, observations, grid["synthetic"], grid["status"]], axis=-1
    )


def heatmap_figure(grid, price, time, bid, selected, title=None):
    figure = go.Figure(
        go.Heatmap(
            x=grid["prices"] * 100,
            y=grid["bids"] * 100,
            z=grid["probability"],
            zmin=0,
            zmax=1,
            colorscale="Viridis",
            hoverongaps=False,
            customdata=custom_data(grid, time=time),
            hovertemplate="Price %{x:.1f}¢<br>Bid %{y:.1f}¢<br>Time %{customdata[0]:.2f}<br>Fitted %{z:.1%}<br>Matches %{customdata[1]}<br>States %{customdata[2]}<br>Synthetic %{customdata[3]}<br>%{customdata[4]}<extra></extra>",
            colorbar=dict(title="Fill", tickformat=".0%"),
        )
    )
    figure.add_trace(
        go.Scatter(
            x=[price * 100],
            y=[bid * 100],
            mode="markers",
            marker=dict(size=12, color="#ef4444", line=dict(color="white", width=2)),
            name="Selected point",
            customdata=[[time, selected]],
            hovertemplate="Selected price %{x:.1f}¢<br>Bid %{y:.1f}¢<br>Time %{customdata[0]:.2f}<br>Exact fitted %{customdata[1]:.1%}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=grid["prices"] * 100,
            y=np.zeros(len(grid["prices"])),
            mode="lines",
            line=dict(color="#f59e0b", width=4),
            name="Assumed bid-zero boundary",
            hovertemplate=BOUNDARY + "<br>Synthetic; no sample count<extra></extra>",
        )
    )
    figure.update_layout(
        title=title,
        height=510,
        xaxis=dict(title="Current price (¢)", range=[1, 100]),
        yaxis=dict(title="Absolute bid (¢)", range=[0, 99]),
        margin=dict(t=35, b=35),
        legend=dict(orientation="h", y=-0.2),
        uirevision="no-clv-heatmap",
    )
    return figure


def time_surface_figure(
    grid, price, time, bid, selected, camera_revision, slice_probability=None
):
    figure = go.Figure(
        go.Surface(
            x=grid["times"],
            y=grid["bids"] * 100,
            z=grid["probability"],
            cmin=0,
            cmax=1,
            colorscale="Viridis",
            connectgaps=False,
            customdata=custom_data(grid, price=price),
            colorbar=dict(title="Fill", tickformat=".0%"),
            hovertemplate="Time %{x:.2f}<br>Bid %{y:.1f}¢<br>Price %{customdata[0]:.0%}<br>Fitted %{z:.1%}<br>Matches %{customdata[1]}<br>States %{customdata[2]}<br>Synthetic %{customdata[3]}<br>%{customdata[4]}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter3d(
            x=grid["times"],
            y=np.zeros(len(grid["times"])),
            z=np.full(len(grid["times"]), 1 - price),
            mode="lines",
            line=dict(color="#f59e0b", width=7),
            name="Assumed bid-zero boundary",
            hovertemplate=BOUNDARY + "<br>Synthetic; no sample count<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter3d(
            x=[time] * len(grid["bids"]),
            y=grid["bids"] * 100,
            z=(
                slice_probability
                if slice_probability is not None
                else grid["probability"][:, np.argmin(abs(grid["times"] - time))]
            ),
            mode="lines",
            line=dict(color="#ef4444", width=7),
            connectgaps=False,
            name="Current time slice",
        )
    )
    figure.add_trace(
        go.Scatter3d(
            x=[time],
            y=[bid * 100],
            z=[selected],
            mode="markers",
            marker=dict(color="#ef4444", size=6),
            name="Selected point",
        )
    )
    figure.update_layout(
        height=680,
        scene=dict(
            xaxis_title="Normalized time",
            yaxis_title="Absolute bid (¢)",
            zaxis=dict(title="Fitted probability", range=[0, 1], tickformat=".0%"),
            camera=dict(
                eye=dict(x=1.7, y=1.7, z=1.3), projection=dict(type="orthographic")
            ),
            aspectmode="manual",
            aspectratio=dict(x=1.3, y=1.2, z=0.8),
            uirevision=f"no-clv-camera-v2-{camera_revision}",
        ),
        uirevision=f"no-clv-surface-{camera_revision}",
        margin=dict(l=55, r=85, t=65, b=110),
        legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, font=dict(size=11)),
    )
    return figure


def time_series_figure(model, price, time, bid, tolerances, minimum, raw=None):
    times = np.unique(np.r_[np.linspace(0, 1, 101), time])
    figure = go.Figure()
    for candidate, label, color in [
        (bid, "Selected bid", "#2563eb"),
        (round(bid - 0.01, 8), "Bid − 1¢", "#94a3b8"),
        (round(bid + 0.01, 8), "Bid + 1¢", "#10b981"),
    ]:
        if not 0 <= candidate < price:
            continue
        points = np.column_stack(
            [np.full(len(times), price), times, np.full(len(times), candidate)]
        )
        result = model.evaluate(points, tolerances, minimum)
        figure.add_trace(
            go.Scatter(
                x=times,
                y=result["probability"],
                mode="lines",
                name=label,
                line=dict(color=color),
                connectgaps=False,
                customdata=np.column_stack([result["n_events"], result["synthetic"]]),
                hovertemplate="Time %{x:.2f}<br>Fitted %{y:.1%}<br>Nearby matches %{customdata[0]}<br>Synthetic %{customdata[1]}<extra></extra>",
            )
        )
    if raw is not None:
        figure.add_trace(
            go.Scatter(
                x=raw.normalized_time,
                y=raw.probability,
                mode="markers",
                name="Raw neighboring-state rate",
                marker=dict(color="#64748b", size=7),
                customdata=raw[["n_events", "n_observations"]],
                hovertemplate="Time %{x:.2f}<br>Observed %{y:.1%}<br>Matches %{customdata[0]}<br>States %{customdata[1]}<extra></extra>",
            )
        )
    selected = model.evaluate([[price, time, bid]], tolerances, minimum)["probability"][
        0
    ]
    figure.add_trace(
        go.Scatter(
            x=[time],
            y=[selected],
            mode="markers",
            marker=dict(color="#ef4444", size=11),
            name="Exact selected point",
        )
    )
    figure.add_vline(x=time, line_dash="dot", line_color="#ef4444")
    figure.update_layout(
        height=440,
        xaxis=dict(title="Normalized time", range=[0, 1]),
        yaxis=dict(title="Fitted probability", range=[0, 1], tickformat=".0%"),
        legend=dict(orientation="h", y=-0.25),
        margin=dict(t=20, b=40),
    )
    return figure


def snapshots_figure(grids, price, time, bid, model, tolerances, minimum):
    closest = min(SNAPSHOTS, key=lambda value: abs(value - time))
    titles = [
        f"t = {value:.2f}" + (" · closest to selection" if value == closest else "")
        for value in SNAPSHOTS
    ]
    figures = []
    for snapshot, grid, title in zip(SNAPSHOTS, grids, titles):
        selected = model.evaluate([[price, snapshot, bid]], tolerances, minimum)[
            "probability"
        ][0]
        figure = heatmap_figure(grid, price, snapshot, bid, selected, title=title)
        figure.update_layout(
            height=420,
            showlegend=False,
            margin=dict(l=65, r=65, t=65, b=65),
            title=dict(text=title, x=0.02),
        )
        figures.append(figure)
    return figures


def workspace(path, family, ticker):
    st.subheader("No-CLV Visualizer")
    st.caption(
        "Pools all pregame prices. Retrospective normalized time uses realized settlement. These are YES price-reaching estimates; queue position, size and partial executions are not modeled."
    )
    with st.sidebar:
        st.subheader("Your scenario")
        price, time, bid = scenario_controls()
        playback_slot = st.container()
    a, b, c = st.columns(3)
    outcome = a.selectbox(
        "Estimate",
        ["optimistic", "conservative"],
        format_func=str.capitalize,
        key="no_clv_outcome",
    )
    mode = b.selectbox(
        "Smoothing",
        ["Automatic · whole-game validation", "Manual by axis"],
        key="no_clv_smoothing_mode",
    )
    show_raw = c.checkbox("Show raw data", value=True, key="no_clv_show_raw")
    smoothing = "auto"
    with st.sidebar.expander("Similarity, support and smoothing settings"):
        a, b = st.columns(2)
        price_tol = (
            a.number_input(
                "Price tolerance (¢)", 0.0, 100.0, 5.0, key="no_clv_price_tol"
            )
            / 100
        )
        time_tol = b.number_input(
            "Time tolerance",
            0.0,
            1.0,
            0.025,
            0.005,
            format="%.3f",
            key="no_clv_time_tol",
        )
        minimum = int(
            st.number_input(
                "Minimum reference matches", min_value=1, value=20, key="no_clv_minimum"
            )
        )
        cutoff = (
            st.text_input(
                "Only use matches settled before (optional)",
                key="no_clv_cutoff",
                placeholder="2026-07-01T00:00:00Z",
            ).strip()
            or None
        )
        resolution = st.select_slider(
            "Display grid resolution", [21, 51, 81], value=51, key="no_clv_resolution"
        )
        if mode == "Manual by axis":
            defaults = st.session_state.get(
                "no_clv_validated_strengths", (1e-5, 1e-5, 1e-5)
            )
            columns = st.columns(3)
            smoothing = tuple(
                10
                ** column.slider(
                    f"{axis} smoothing · log10",
                    -9.0,
                    -1.0,
                    value=float(np.log10(max(default, 1e-9))),
                    step=0.5,
                    key=f"no_clv_smooth_{axis}",
                )
                for column, axis, default in zip(
                    columns, ["Price", "Time", "Bid"], defaults
                )
            )
    tolerances = (price_tol, time_tol)
    if bid >= price:
        st.warning("Set your bid below the current price.")
    try:
        stat = Path(path).stat()
        signature = (
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
            family,
            ticker,
            cutoff,
            outcome,
            smoothing,
            MODEL_VERSION,
        )
        model = cached_model(*signature)
        if smoothing == "auto":
            st.session_state["no_clv_validated_strengths"] = model.strengths
        selected = model.evaluate([[price, time, bid]], tolerances, minimum)
        raw_selected = model.empirical([[price, time, bid]], tolerances).iloc[0]
        grid = cached_heatmap(signature, time, tolerances, minimum, resolution, model)
        tgrid = cached_time_grid(
            signature, price, tolerances, minimum, resolution, model
        )
    except (ValueError, OSError, KeyError) as exc:
        st.error(f"Cannot build the no-CLV visualizer: {exc}")
        return
    value = float(selected["probability"][0])
    with st.container(key="no_clv_readouts"):
        a, b, c = st.columns(3)
        a.metric(
            "Fitted probability at selected bid",
            f"{value:.2%}" if np.isfinite(value) else "Unsupported",
        )
        raw_enough = raw_selected.n_events >= minimum
        b.metric(
            "Local empirical probability",
            f"{raw_selected.probability:.2%}" if raw_enough else "Insufficient support",
        )
        c.metric("Data support", str(selected["status"][0]).capitalize())
    with st.container(key="no_clv_coverage"):
        st.caption(
            f"Local coverage: {raw_selected.n_events:,} matches · {raw_selected.n_observations:,} states. Rates pool all CLVs."
        )
    if time == 1:
        st.info(
            "At settlement the historical fill horizon has ended. Positive-bid fitted values are masked; the displayed bid-zero boundary is only an assumption."
        )
    if st.button("Reset camera", key="no_clv_reset_camera"):
        st.session_state["no_clv_camera_revision"] = (
            st.session_state.get("no_clv_camera_revision", 0) + 1
        )

    def remember_view():
        st.session_state["no_clv_active_view"] = st.session_state["no_clv_view"]

    active_view = st.session_state.get("no_clv_active_view", "Price/Bid Heatmap")
    st.session_state["no_clv_view"] = active_view
    heat, surface, series, snapshots = st.tabs(
        [
            "Price/Bid Heatmap",
            "Time Surface",
            "Selected-Point Time Series",
            "Time Snapshots",
        ],
        key="no_clv_view",
        on_change=remember_view,
        default=active_view,
    )
    with heat:
        st.plotly_chart(
            heatmap_figure(grid, price, time, bid, value),
            width="stretch",
            key="no_clv_heatmap",
        )
    with surface:
        st.html(
            "<style>.fill-surface-playback{display:flex;gap:.5rem;margin-bottom:.5rem}"
            ".fill-surface-playback button{font:inherit;color:inherit;background:transparent;"
            "border:1px solid #94a3b8;border-radius:.5rem;padding:.5rem 1rem;cursor:pointer}"
            ".fill-surface-playback button:hover{border-color:#ef4444}"
            '</style><div class="fill-surface-playback">'
            '<button type="button" data-fill-playback="play">▶ Play time</button>'
            '<button type="button" data-fill-playback="pause">Pause</button></div>'
        )
        st.plotly_chart(
            time_surface_figure(
                tgrid,
                price,
                time,
                bid,
                value,
                st.session_state.get("no_clv_camera_revision", 0),
                model.evaluate(
                    np.column_stack(
                        [
                            np.full(len(tgrid["bids"]), price),
                            np.full(len(tgrid["bids"]), time),
                            tgrid["bids"],
                        ]
                    ),
                    tolerances,
                    minimum,
                )["probability"],
            ),
            width="stretch",
            key="no_clv_time_surface",
        )
    with series:
        raw = (
            cached_raw_curve(signature, price, bid, tolerances, model)
            if show_raw
            else None
        )
        st.plotly_chart(
            time_series_figure(model, price, time, bid, tolerances, minimum, raw),
            width="stretch",
            key="no_clv_time_series",
        )
        if show_raw:
            st.caption(
                "Raw markers show neighboring-state rates with at least one match; inspect hover counts. The selected empirical readout uses your minimum-support threshold."
            )
    with snapshots:
        grids = [
            cached_heatmap(signature, snapshot, tolerances, minimum, resolution, model)
            for snapshot in SNAPSHOTS
        ]
        st.caption("Each snapshot uses the same 0–100% probability scale.")
        for index, figure in enumerate(
            snapshots_figure(grids, price, time, bid, model, tolerances, minimum)
        ):
            with st.container(border=True):
                st.plotly_chart(figure, width="stretch", key=f"no_clv_snapshot_{index}")
    browser_code = (
        Path(__file__).with_name("no_clv_fill_browser.js").read_text(encoding="utf-8")
    )
    st.html(f"<script>{browser_code}</script>", unsafe_allow_javascript=True)
    playback_controls(
        signature,
        model,
        price,
        time,
        bid,
        tolerances,
        minimum,
        resolution,
        family,
        ticker,
        slot=playback_slot,
    )
    if show_raw:
        with st.expander("Raw historical observations"):
            st.dataframe(model.raw, hide_index=True)
            st.download_button(
                "Download no-CLV empirical bins",
                model.raw.to_csv(index=False),
                f"{family}_no_clv_raw.csv",
                "text/csv",
                key="no_clv_download_raw",
            )
    with st.expander("Model validation and assumptions"):
        st.warning(
            BOUNDARY
            + ". With a nondecreasing bid curve, this also imposes a floor at higher bids. The amber boundary is synthetic and has no sample count; raw rates can lie below it."
        )
        st.caption(
            f"Raw estimates give each match equal weight across all CLVs; price ±{price_tol * 100:g}¢, time ±{time_tol:g}. Sparse means fewer than {minimum} matches; no nearby matches is unsupported. Blank regions include bids at/above price or an ended horizon."
        )
        st.write(
            "Independent log-odds price/time B-splines with monotone integrated bid splines. Fitted with state-level binomial hits/trials. Price, time and bid smoothing are chosen separately using whole games; synthetic boundary rows do not enter fitting or scores."
        )
        st.json(model.validation)
        if not model.validation["optimizer_converged"]:
            st.warning("The optimizer did not converge. Treat this fit as provisional.")
        if model.validation["status"] == "unvalidated":
            st.warning(
                "This dataset is too small to validate smoothing on held-out games."
            )
        st.download_button(
            "Download validation metrics",
            pd.Series(model.validation).to_json(indent=2),
            f"{family}_no_clv_validation.json",
            "application/json",
            key="no_clv_download_validation",
        )
    if st.query_params.get("debug") == "1":
        with st.expander("No-CLV development diagnostics"):
            row = int(np.argmin(abs(grid["bids"] - bid)))
            col = int(np.argmin(abs(grid["prices"] - price)))
            st.json(
                dict(
                    slider_values=st.session_state["no_clv_scenario"],
                    feature_order=["current_price", "normalized_time", "bid"],
                    model_inputs=[price, time, bid],
                    exact_prediction=float(model.predict([[price, time, bid]])[0]),
                    displayed_prediction=value,
                    nearest_grid_inputs=[
                        float(grid["prices"][col]),
                        time,
                        float(grid["bids"][row]),
                    ],
                    nearest_grid_value=float(grid["probability"][row, col]),
                    empirical_neighborhood=raw_selected.to_dict(),
                    support=str(selected["status"][0]),
                    model_version=MODEL_VERSION,
                    smoothing=model.strengths,
                )
            )


def no_clv_page(path, family, ticker):
    workspace(path, family, ticker)
