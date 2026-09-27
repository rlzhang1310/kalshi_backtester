"""Shared scenario controls and full-page Plotly model inspection."""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .fill_probability import estimate_fill_probability
from .fill_surface import (
    MODEL_VERSION,
    curvature,
    fit_surface,
    reference_data,
    surface_grid,
)

BOUNDARY = "Assumed boundary: bid = 0, probability = 1 − current price"


def update_scenario(prefix, name):
    st.session_state["scenario"][name] = st.session_state[f"{prefix}_{name}"]


def scenario_controls(prefix, num_columns=2):
    defaults = dict(clv=60, p=60, t=0.5, b=55)
    state = st.session_state.setdefault("scenario", defaults)
    columns = st.columns(num_columns)
    definitions = [
        ("clv", "Pregame probability (CLV) (%)", 0, 100, 1, 0),
        ("p", "Current YES price x (¢)", 1, 100, 1, 1),
        ("t", "Normalized time t", 0.0, 1.0, 0.01, 0),
        ("b", "Your YES bid y (¢)", 0, 99, 1, 1),
    ]
    for name, label, low, high, step, col in definitions:
        key = f"{prefix}_{name}"
        if key not in st.session_state or st.session_state[key] != state[name]:
            st.session_state[key] = state[name]
        columns[col % num_columns].slider(
            label,
            low,
            high,
            step=step,
            key=key,
            on_change=update_scenario,
            args=(prefix, name),
        )
    return state["clv"], state["p"], state["t"], state["b"]


@st.cache_resource(
    max_entries=6,
    show_spinner="Fitting the four-input model and checking held-out games…",
)
def cached_model(
    path,
    modified,
    size,
    family,
    ticker,
    as_of,
    outcome,
    smoothing,
    boundary,
    coordinate="log_odds",
    version=MODEL_VERSION,
):
    frame = reference_data(pd.read_parquet(path), family, ticker, as_of)
    return fit_surface(frame, outcome, smoothing, boundary, coordinate)


@st.cache_data(max_entries=12, show_spinner=False)
def cached_grid(signature, clv, time, tolerances, minimum, _model):
    return surface_grid(_model, clv, time, tolerances, minimum)


def chart_figures(
    model,
    grid,
    clv,
    time,
    price,
    bid,
    tolerances,
    minimum,
    raw,
    mode,
    camera_revision,
    raw_selected=None,
):
    selected = model.evaluate([[clv, price, time, bid]], tolerances, minimum)
    value = selected["probability"][0]
    section_bids = np.unique(np.r_[np.linspace(0, 0.99, 101), bid])
    points = np.column_stack(
        [
            np.full(len(section_bids), clv),
            np.full(len(section_bids), price),
            np.full(len(section_bids), time),
            section_bids,
        ]
    )
    section = model.evaluate(points, tolerances, minimum)
    boundary = grid["probability"][0]
    assumed = model.boundary == "assumed"
    boundary_label = (
        BOUNDARY if assumed else "Fitted bid-zero probability · learned from data"
    )
    boundary_hover = (
        "Synthetic assumption; no execution count<extra></extra>"
        if assumed
        else "Bid-zero fitted probability learned from observations<extra></extra>"
    )
    support = grid["n_events"].astype(float)
    # Synthetic anchors have no empirical sample count; nearby coverage remains
    # available in diagnostics, never represented as trials for the assumption.
    if assumed:
        support[0] = np.nan
    custom = np.stack(
        [support, np.full(support.shape, clv), np.full(support.shape, time)], axis=-1
    )
    hover = (
        "Current price %{x:.1f}¢<br>Bid %{y:.1f}¢<br>Fitted %{z:.1%}"
        "<br>CLV %{customdata[1]:.0%}<br>Time %{customdata[2]:.2f}"
        "<br>Nearby matches %{customdata[0]}<extra></extra>"
    )
    main = go.Figure()
    if mode == "Surface":
        main.add_trace(
            go.Surface(
                x=grid["prices"] * 100,
                y=grid["bids"] * 100,
                z=grid["probability"],
                cmin=0,
                cmax=1,
                colorscale="Viridis",
                customdata=custom,
                hovertemplate=hover,
                colorbar=dict(tickformat=".0%", title="Fill"),
                connectgaps=False,
                name="Fitted probability",
            )
        )
        main.add_trace(
            go.Scatter3d(
                x=np.full(len(section_bids), price * 100),
                y=section_bids * 100,
                z=section["probability"],
                mode="lines",
                line=dict(color="#ef4444", width=5),
                name="Selected price section",
                connectgaps=False,
            )
        )
        main.add_trace(
            go.Scatter3d(
                x=grid["prices"] * 100,
                y=np.zeros(len(boundary)),
                z=boundary,
                mode="lines",
                line=dict(color="#f59e0b", width=7),
                name=boundary_label,
                hovertemplate=boundary_hover,
                connectgaps=False,
            )
        )
        main.add_trace(
            go.Scatter3d(
                x=[price * 100],
                y=[bid * 100],
                z=[value],
                mode="markers",
                marker=dict(size=6, color="#ef4444"),
                name="Selected bid · fitted",
            )
        )
        main.update_layout(
            scene=dict(
                xaxis_title="Current price (¢)",
                yaxis_title="Absolute bid (¢)",
                zaxis=dict(title="Fitted probability", range=[0, 1], tickformat=".0%"),
                camera=dict(eye=dict(x=1.65, y=1.65, z=1.25)),
                aspectmode="manual",
                aspectratio=dict(x=1.2, y=1.2, z=0.8),
                uirevision=f"camera-v2-{camera_revision}",
            )
        )
    else:
        main.add_trace(
            go.Heatmap(
                x=grid["prices"] * 100,
                y=grid["bids"] * 100,
                z=grid["probability"],
                zmin=0,
                zmax=1,
                colorscale="Viridis",
                customdata=custom,
                colorbar=dict(tickformat=".0%", title="Fill"),
                hovertemplate=hover,
                hoverongaps=False,
            )
        )
        main.add_trace(
            go.Scatter(
                x=grid["prices"] * 100,
                y=np.where(np.isfinite(boundary), 0, np.nan),
                mode="lines",
                line=dict(color="#f59e0b", width=4),
                name=boundary_label,
                hovertemplate=boundary_hover,
            )
        )
        main.add_vline(x=price * 100, line_color="#ef4444", line_dash="dot")
        main.add_trace(
            go.Scatter(
                x=[price * 100],
                y=[bid * 100],
                mode="markers",
                marker=dict(color="#ef4444", size=11),
                name="Selected bid · fitted",
            )
        )
        main.update_layout(
            xaxis_title="Current price (¢)", yaxis_title="Absolute bid (¢)"
        )
    main.update_layout(
        height=640,
        margin=dict(l=45, r=70, t=55, b=85),
        uirevision=f"view-{camera_revision}",
        legend=dict(orientation="h", y=1.04, yanchor="bottom", x=0),
    )
    cross = go.Figure(
        go.Scatter(
            x=section_bids * 100,
            y=section["probability"],
            mode="lines",
            name="Fitted cross-section",
            connectgaps=False,
            line_color="#2563eb",
        )
    )
    if raw is not None and len(raw):
        cross.add_trace(
            go.Scatter(
                x=raw.bid * 100,
                y=raw.probability,
                mode="markers",
                name="Raw neighboring-state estimates",
                marker=dict(
                    size=np.clip(4 + np.log1p(raw.n_events) * 1.5, 5, 16),
                    color="#64748b",
                ),
                customdata=raw[["n_events", "n_observations"]],
                hovertemplate="Bid %{x:.1f}¢<br>Observed %{y:.1%}<br>Matches %{customdata[0]}<br>States %{customdata[1]}<extra></extra>",
            )
        )
    anchor = model.evaluate([[clv, price, time, 0]], tolerances, minimum)[
        "probability"
    ][0]
    cross.add_trace(
        go.Scatter(
            x=[0],
            y=[anchor],
            mode="markers",
            marker=dict(color="#f59e0b", symbol="diamond", size=12),
            name="Synthetic bid-zero anchor" if assumed else "Learned bid-zero value",
            hovertemplate=(
                "Assumption: %{y:.1%}; no sample count<extra></extra>"
                if assumed
                else "Fitted bid-zero probability: %{y:.1%}<extra></extra>"
            ),
        )
    )
    if raw_selected is not None:
        cross.add_trace(
            go.Scatter(
                x=[bid * 100],
                y=[raw_selected],
                mode="markers",
                name="Selected bid · raw",
                marker=dict(color="#111827", symbol="x", size=12),
            )
        )
    cross.add_trace(
        go.Scatter(
            x=[bid * 100],
            y=[value],
            mode="markers",
            marker=dict(color="#ef4444", size=11),
            name="Selected bid · fitted",
        )
    )
    cross.update_layout(
        height=320,
        xaxis=dict(title="Absolute bid (¢)", range=[0, price * 100]),
        yaxis=dict(title="Fill probability", range=[0, 1], tickformat=".0%"),
        margin=dict(t=15, b=10),
        legend=dict(orientation="h", y=-0.3),
    )
    return main, cross, selected


def raw_section(model, family, clv, price, time, tolerances):
    rows = []
    # The same production empirical lookup, with support=1 for inspecting sparse
    # observations. It is explicitly labeled as neighboring states, not exact data.
    # Generate exact cent ticks before checking the domain. A linspace value
    # just below 0.50 could pass the check, then round to an invalid 0.50 bid.
    for bid_cents in range(0, 100, 5):
        bid = bid_cents / 100
        if bid >= price:
            continue
        result = estimate_fill_probability(
            model.source,
            family=family,
            clv=clv,
            current_price=price,
            normalized_time=time,
            bid_price=bid,
            clv_tolerance=tolerances[0],
            price_tolerance=tolerances[1],
            time_tolerance=tolerances[2],
            min_events=1,
        )
        if result["n_events"]:
            rows.append(
                dict(
                    bid=bid,
                    probability=result[f"p_{model.outcome}"],
                    n_events=result["n_events"],
                    n_observations=result["n_observations"],
                )
            )
    return pd.DataFrame(rows)


def surface_explorer(path, family, ticker, as_of, tolerances, minimum):
    st.subheader("CLV Visualizer")
    with st.sidebar:
        st.subheader("Your scenario")
        clv, price, time, bid = scenario_controls("explorer", num_columns=1)
    clv, price, bid = clv / 100, price / 100, bid / 100
    a, b, c = st.columns(3)
    mode = a.radio("View", ["Surface", "Heatmap"], horizontal=True)
    outcome = b.selectbox(
        "Estimate", ["optimistic", "conservative"], format_func=str.capitalize
    )
    smoothing = c.selectbox(
        "Smoothing",
        ["auto", 0.0, 1e-7, 1e-5, 1e-3],
        format_func=lambda x: "Automatic · whole-game validation"
        if x == "auto"
        else str(x),
    )
    show_raw = st.checkbox("Show raw data", value=True)
    coordinate = st.selectbox(
        "Model scale",
        ["log_odds", "probability"],
        format_func=lambda value: "Log-odds"
        if value == "log_odds"
        else "Probability (previous model)",
        key="surface_coordinate",
        help="Changes how the model smooths across prices. Compare held-out scores in Model details; neither scale guarantees better accuracy.",
    )
    boundary_mode = st.selectbox(
        "Zero-bid boundary",
        ["empirical", "assumed"],
        format_func=lambda value: "Learn from historical data"
        if value == "empirical"
        else "Impose 1 − current price (legacy assumption)",
    )
    if st.button("Reset camera"):
        st.session_state["camera_revision"] = (
            st.session_state.get("camera_revision", 0) + 1
        )
    st.caption(
        "Retrospective start-to-settlement time. YES price-reaching probabilities before trading closes; queue position and order size are not modeled. Blank areas lack support or have bid ≥ current price."
    )
    if boundary_mode == "assumed":
        st.warning(
            BOUNDARY
            + ". This imposes a floor on all higher bids and can conflict with the observations."
        )
    else:
        st.caption(
            "The zero-bid value is fitted from historical data. No 1 − current price floor is imposed."
        )
    stat = Path(path).stat()
    signature = (
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        family,
        ticker,
        as_of,
        outcome,
        smoothing,
        boundary_mode,
        coordinate,
        MODEL_VERSION,
    )
    try:
        model = cached_model(*signature)
        grid = cached_grid(signature, clv, time, tolerances, minimum, model)
        raw = (
            raw_section(model, family, clv, price, time, tolerances)
            if show_raw
            else None
        )
        raw_result = None
        if bid < price:
            raw_result = estimate_fill_probability(
                model.source,
                family=family,
                clv=clv,
                current_price=price,
                normalized_time=time,
                bid_price=bid,
                clv_tolerance=tolerances[0],
                price_tolerance=tolerances[1],
                time_tolerance=tolerances[2],
                min_events=minimum,
            )
        raw_value = raw_result[f"p_{outcome}"] if raw_result else None
        main, cross, selected = chart_figures(
            model,
            grid,
            clv,
            time,
            price,
            bid,
            tolerances,
            minimum,
            raw,
            mode,
            st.session_state.get("camera_revision", 0),
            raw_value if show_raw else None,
        )
    except (ValueError, OSError) as exc:
        st.error(f"Cannot build this surface: {exc}")
        return
    value = selected["probability"][0]
    label = f"{value:.2%}" if np.isfinite(value) else "Unsupported"
    st.write(
        f"CLV {clv:.0%} · Current {price * 100:.0f}¢ · Time {time:.2f} · Bid {bid * 100:.0f}¢"
    )
    fitted_column, raw_column = st.columns(2)
    fitted_column.metric("Fitted probability at selected bid", label)
    raw_column.metric(
        "Raw probability at selected bid",
        f"{raw_value:.2%}" if raw_value is not None else "Insufficient support",
    )
    if not np.isfinite(grid["probability"]).any():
        st.info(
            "No supported surface at this CLV/time slice. Adjust the slice or the estimator's similarity/support settings."
        )
    st.plotly_chart(main, width="stretch", key="surface_main")
    st.plotly_chart(cross, width="stretch", key="surface_section")
    # Trusted repository-owned code only. Streamlit rebuilds 3D charts on
    # reruns, so retain Plotly camera state separately from scenario inputs.
    browser_code = (
        Path(__file__).with_name("fill_surface_browser.js").read_text(encoding="utf-8")
    )
    st.html(f"<script>{browser_code}</script>", unsafe_allow_javascript=True)
    st.caption(
        f"Raw points pool neighboring states: CLV ±{tolerances[0]:.1%}, price ±{tolerances[1] * 100:g}¢, time ±{tolerances[2]:g}. Their size reflects match count; no confidence bands are inferred."
    )
    with st.expander("Model details, conflicts, and curvature"):
        st.write(
            f"Model scale: {model.coordinate}. Local B-splines across CLV, price and time; monotone integrated bid splines. Curvature regularization: {model.strength:g}."
        )
        if model.coordinate == "log_odds":
            st.caption(
                "CLV and price use finite log-odds with half-cent endpoint regularization. "
                "Bid uses the log-odds gap, normalized between zero bid and current price. "
                "Time, historical hit rates, support tolerances, and chart units are unchanged."
            )
        st.json(model.validation)
        conflicts = model.validation["boundary_conflict_bins"]
        if boundary_mode == "assumed":
            st.write(
                f"{conflicts:,} empirical bins lie below the imposed floor. This boundary prevents the fit from matching them."
            )
        if not model.validation["optimizer_converged"]:
            st.warning(
                "The optimizer reached its iteration limit. Treat this fit as provisional."
            )
        if model.validation["status"] == "unvalidated":
            st.warning(
                "Smoothing has not been validated on held-out games for this dataset."
            )
        st.json(curvature(grid))
        st.caption(
            "Average squared second derivative in [0,1] price units, using only supported three-cell stencils. Lower curvature means less bending, not better predictive accuracy."
        )
        st.download_button(
            "Download original empirical bins",
            model.raw.to_csv(index=False),
            f"{family}_surface_raw.csv",
            "text/csv",
        )
