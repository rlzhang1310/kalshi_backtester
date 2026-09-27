"""Oddness Visualizer; shared preparation/game UI stays in fill_app."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from scipy.special import logit
import streamlit as st

from .fill_surface import reference_data
from .fill_playback import nullable
from .odds_moneyness import (
    MODEL_VERSION,
    distance,
    equivalent_bid,
    fit_model,
    surface_grid,
)


@st.cache_resource(max_entries=6, show_spinner="Fitting odds-moneyness surface…")
def cached_model(signature, outcome, smoothing):
    path, _, _, family, ticker, cutoff, _ = signature
    return fit_model(
        reference_data(pd.read_parquet(path), family, ticker, cutoff),
        outcome,
        smoothing,
    )


@st.cache_data(max_entries=12, show_spinner="Evaluating odds-moneyness grid…")
def cached_grid(signature, outcome, strengths, tolerances, minimum, resolution, _model):
    """Cache the fourth-power distance grid shared by the heatmap and surface."""
    return surface_grid(_model, tolerances, minimum, resolution)


def chart_figures(
    model,
    grid,
    d,
    time,
    selected,
    show_raw,
    revision,
    tolerances=(0.2, 0.05),
    minimum=20,
):
    custom = np.stack(
        [
            grid["empirical"],
            grid["n_events"],
            grid["n_trials"],
            grid["status"],
            np.broadcast_to(grid["distances"], grid["probability"].shape),
        ],
        axis=-1,
    )
    axis_distances = np.log1p(grid["distances"] / 0.1)
    axis_d = float(np.sign(d) * np.log1p(abs(d) / 0.1))
    ticks = np.array([0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 4, 6, 8, 10])
    ticks = ticks[(ticks >= model.bounds[0]) & (ticks <= model.bounds[1])]
    distance_axis = dict(
        title="Log-odds distance (expanded near zero)",
        range=np.log1p(np.array(model.bounds) / 0.1).tolist(),
        tickvals=np.log1p(ticks / 0.1),
        ticktext=[f"{value:g}" for value in ticks],
    )
    hover = "Time %{x:.2f}<br>Distance %{customdata[4]:.4f}<br>Estimated %{z:.1%}<br>Empirical %{customdata[0]:.1%}<br>Games %{customdata[1]}<br>State-bid trials %{customdata[2]}<br>%{customdata[3]}<extra></extra>"
    heat = go.Figure(
        go.Heatmap(
            x=grid["times"],
            y=axis_distances,
            z=grid["probability"].T,
            zmin=0,
            zmax=1,
            colorscale="Viridis",
            hoverongaps=False,
            customdata=custom.transpose(1, 0, 2),
            colorbar=dict(title="Probability", tickformat=".0%"),
            hovertemplate=hover,
        )
    )
    surface = go.Figure(
        go.Surface(
            x=grid["times"],
            y=axis_distances,
            z=grid["probability"].T,
            cmin=0,
            cmax=1,
            colorscale="Viridis",
            connectgaps=False,
            customdata=custom.transpose(1, 0, 2),
            colorbar=dict(title="Probability", tickformat=".0%"),
            hovertemplate=hover,
        )
    )
    if show_raw:
        raw = model.raw
        raw_custom = raw[
            [
                "probability",
                "n_events",
                "n_trials",
                "current_price",
                "bid",
                "log_odds_distance",
            ]
        ].to_numpy()
        size = np.clip(3 + np.log1p(raw.n_trials), 4, 13)
        raw_hover = "Time %{x:.2f}<br>Distance %{customdata[5]:.4f}<br>Observed %{customdata[0]:.1%}<br>Games %{customdata[1]}<br>State-bid trials %{customdata[2]}<br>Mean price %{customdata[3]:.1%}<br>Mean bid %{customdata[4]:.1%}<extra>Raw bin</extra>"
        heat.add_trace(
            go.Scatter(
                x=raw.normalized_time,
                y=np.log1p(raw.log_odds_distance / 0.1),
                mode="markers",
                name="Raw estimates",
                marker=dict(
                    size=size,
                    color=raw.probability,
                    cmin=0,
                    cmax=1,
                    colorscale="Viridis",
                    opacity=0.6,
                    line=dict(color="white", width=0.5),
                ),
                customdata=raw_custom,
                hovertemplate=raw_hover,
            )
        )
        surface.add_trace(
            go.Scatter3d(
                x=raw.normalized_time,
                y=np.log1p(raw.log_odds_distance / 0.1),
                z=raw.probability,
                mode="markers",
                name="Raw estimates",
                marker=dict(
                    size=size / 2,
                    color=raw.probability,
                    cmin=0,
                    cmax=1,
                    colorscale="Viridis",
                    opacity=0.55,
                ),
                customdata=raw_custom,
                hovertemplate=raw_hover,
            )
        )
    heat.add_trace(
        go.Scatter(
            x=[time],
            y=[axis_d],
            mode="markers",
            name="Selected point",
            marker=dict(size=12, color="#ef4444"),
            customdata=[[selected, d]],
            hovertemplate="Time %{x:.2f}<br>Distance %{customdata[1]:.4f}<br>Exact estimate %{customdata[0]:.2%}<extra></extra>",
        )
    )
    surface.add_trace(
        go.Scatter3d(
            x=[time],
            y=[axis_d],
            z=[selected],
            mode="markers",
            name="Selected point",
            marker=dict(size=6, color="#ef4444"),
            customdata=[[selected, d]],
            hovertemplate="Time %{x:.2f}<br>Distance %{customdata[1]:.4f}<br>Exact estimate %{z:.2%}<extra></extra>",
        )
    )
    heat.update_layout(
        height=600,
        xaxis=dict(title="Normalized elapsed time", range=[0, 1]),
        yaxis=distance_axis,
        margin=dict(l=65, r=75, t=45, b=95),
        legend=dict(orientation="h", y=1.02, yanchor="bottom"),
        uirevision="odds-heatmap-time-x-expanded",
    )
    surface.update_layout(
        height=700,
        scene=dict(
            xaxis=dict(title="Normalized elapsed time", range=[0, 1]),
            yaxis=distance_axis,
            zaxis=dict(
                title="Price-reaching probability", range=[0, 1], tickformat=".0%"
            ),
            camera=dict(
                eye=dict(x=1.7, y=1.7, z=1.3), projection=dict(type="orthographic")
            ),
            aspectmode="manual",
            aspectratio=dict(x=1.6, y=1.8, z=0.8),
            uirevision=f"odds-camera-time-x-expanded-{revision}",
        ),
        uirevision=f"odds-surface-time-x-expanded-{revision}",
        margin=dict(l=55, r=90, t=70, b=120),
        legend=dict(orientation="h", y=1.02, yanchor="bottom"),
    )
    return heat, surface


def commit_selection():
    selection = st.session_state.get("odds_playback", {}).get("selection")
    state = st.session_state.get("odds_scenario")
    if (
        not selection
        or not state
        or selection.get("token") != st.session_state.get("odds_token")
    ):
        return
    if (
        selection.get("price") != state["p"] / 100
        or selection.get("bid") != state["b"] / 100
    ):
        return
    if selection.get("view") in ("Heatmap", "3D Surface"):
        st.session_state["odds_active_view"] = selection["view"]
    time = selection.get("time")
    if isinstance(time, (int, float)) and np.isfinite(time) and 0 <= time <= 1:
        state["t"] = float(time)
    d = selection.get("distance")
    if isinstance(d, (int, float)) and np.isfinite(d):
        state["b"] = float(
            np.clip(equivalent_bid(state["p"] / 100, d), 0.01, 0.99) * 100
        )
        st.session_state["odds_b"] = state["b"]


@lru_cache(maxsize=1)
def playback_source():
    return (
        "if (!globalThis.Plotly) {\n"
        + get_plotlyjs()
        + "\n}\n"
        + Path(__file__).with_name("time_seek.js").read_text(encoding="utf-8")
        + "\n"
        + Path(__file__)
        .with_name("odds_moneyness_browser.js")
        .read_text(encoding="utf-8")
    )


def playback_controls(slot, model, p, b, d, time, tolerances, minimum, token):
    times = np.arange(101) / 100
    selected = model.evaluate(
        np.column_stack([np.full(101, d), times]), tolerances, minimum
    )
    exact = model.evaluate([[d, time]], tolerances, minimum)
    data = dict(
        token=token,
        price=p,
        bid=b,
        distance=d,
        initial_time=time,
        initial_probability=nullable(exact["probability"])[0],
        **{
            key: nullable(value) if value.dtype.kind == "f" else value.tolist()
            for key, value in selected.items()
        },
    )
    component = st.components.v2.component(
        "odds_moneyness_playback",
        html="""<div class="odds-playback">
      <label for="odds-time">Normalized time <output id="odds-time-value"></output></label>
      <input id="odds-time" aria-label="Normalized time" type="range" min="0" max="100" step="1" />
      <div class="buttons"><button id="odds-play" type="button">▶ Play time</button><button id="odds-pause" type="button" disabled>Pause</button></div>
      <p id="odds-status" role="status">Retrospective · start to settlement</p></div>""",
        css=""".odds-playback{font:inherit;color:var(--st-text-color);padding:6px 0}label{display:flex;justify-content:space-between;font-size:14px;margin-bottom:12px}
      input{width:100%;accent-color:#ef4444;cursor:pointer}.buttons{display:flex;gap:8px;margin-top:12px}button{flex:1;padding:8px;border:1px solid #94a3b866;
      border-radius:8px;background:transparent;color:inherit;cursor:pointer}button:disabled{opacity:.45}button:focus-visible,input:focus-visible{outline:2px solid #ef4444;outline-offset:3px}p{font-size:12px;opacity:.7}""",
        js=playback_source(),
    )
    with slot:
        component(data=data, key="odds_playback", on_selection_change=commit_selection)


@st.fragment
def scenario_panel(
    model,
    grid,
    tolerances,
    minimum,
    token_base,
    caption_slot,
    readout_slot,
    coverage_slot,
    warning_slot,
    debug_slot,
):
    """Rerun only scenario controls/readouts; existing plots stay mounted."""
    state = st.session_state.setdefault("odds_scenario", dict(p=60.0, b=55.0, t=0.5))

    def save_scenario(field, key):
        value = float(st.session_state[key])
        state[field] = value
        st.session_state[f"odds_{field}"] = value
        st.session_state[f"odds_{field}_input"] = value

    st.subheader("Your scenario")
    for field, label in (("p", "Current price (¢)"), ("b", "Absolute bid (¢)")):
        slider_key, input_key = f"odds_{field}", f"odds_{field}_input"
        for key in (slider_key, input_key):
            if key not in st.session_state or st.session_state[key] != state[field]:
                st.session_state[key] = state[field]
        st.slider(
            label,
            1.0,
            99.0,
            step=0.01,
            key=slider_key,
            on_change=save_scenario,
            args=(field, slider_key),
        )
        st.number_input(
            f"Enter {label.lower()}",
            1.0,
            99.0,
            step=0.01,
            format="%.2f",
            key=input_key,
            label_visibility="collapsed",
            on_change=save_scenario,
            args=(field, input_key),
        )
    playback_slot = st.container()
    p, b, time = state["p"] / 100, state["b"] / 100, state["t"]
    d = float(distance(p, b))
    selected = model.evaluate([[d, time]], tolerances, minimum)
    probability = float(selected["probability"][0])
    caption_slot.caption(
        f"Current price {p:.2%} · bid {b:.2%} · log-odds distance {d:.4f}"
    )
    readout_slot.empty()
    with readout_slot.container():
        columns = st.columns(4)
        columns[0].metric(
            "Estimated price-reaching probability",
            f"{probability:.2%}" if np.isfinite(probability) else "Unsupported",
        )
        empirical = selected["empirical"][0]
        columns[1].metric(
            "Local empirical rate",
            f"{empirical:.2%}" if np.isfinite(empirical) else "Unavailable",
        )
        columns[2].metric("Data support", str(selected["status"][0]).capitalize())
        columns[3].metric("Normalized time", f"{time:.2f}")
    coverage_slot.caption(
        f"Local coverage: {selected['n_events'][0]:,} games · {selected['n_trials'][0]:,} state-bid trials. Blank regions are masked; observed 0% remains visible."
    )
    warning_slot.empty()
    with warning_slot.container():
        if b >= p:
            st.warning(
                "Choose a bid below the current price; at/above-market bids are outside this model's eligible training domain."
            )
        if time == 1:
            st.info(
                "At settlement the price-reaching horizon has ended; fitted probabilities are masked."
            )
    token = hashlib.sha256(json.dumps([token_base, p, b]).encode()).hexdigest()
    st.session_state["odds_token"] = token
    playback_controls(playback_slot, model, p, b, d, time, tolerances, minimum, token)
    if debug_slot is not None:
        di, ti = (
            np.argmin(abs(grid["distances"] - d)),
            np.argmin(abs(grid["times"] - time)),
        )
        nearest = model.raw.iloc[
            np.argmin(
                np.sum(
                    (
                        model.raw[["log_odds_distance", "normalized_time"]].to_numpy()
                        - [d, time]
                    )
                    ** 2,
                    axis=1,
                )
            )
        ]
        debug_slot.json(
            dict(
                sliders=state,
                probabilities=dict(p=p, b=b),
                logits=dict(p=float(logit(p)), b=float(logit(b))),
                distance=d,
                feature_order=["log_odds_distance", "normalized_time"],
                exact=probability,
                nearest_grid=dict(
                    distance=float(grid["distances"][di]),
                    time=float(grid["times"][ti]),
                    probability=float(grid["probability"][ti, di]),
                ),
                nearest_empirical=nearest.to_dict(),
                support=str(selected["status"][0]),
                model_version=MODEL_VERSION,
                smoothing=model.strengths,
            )
        )


def odds_moneyness_page(path, family, ticker):
    st.subheader("Oddness Visualizer")
    st.caption(
        "Estimated YES price-reaching probability from log-odds distance and retrospective normalized time. Queue position, size and actual order execution are not modeled."
    )
    st.caption(
        "The odds-moneyness surface uses finite positive prices; settlement at zero is outside the continuous log-odds surface."
    )
    state = st.session_state.setdefault("odds_scenario", dict(p=60.0, b=55.0, t=0.5))

    with st.sidebar:
        scenario_slot = st.container()
        with st.expander("Support, smoothing and display"):
            d_tol = st.number_input(
                "Distance tolerance",
                min_value=0.01,
                max_value=10.0,
                value=0.2,
                step=0.05,
                key="odds_d_tol",
            )
            t_tol = st.number_input(
                "Time tolerance",
                min_value=0.005,
                max_value=1.0,
                value=0.05,
                step=0.005,
                format="%.3f",
                key="odds_t_tol",
            )
            minimum = int(
                st.number_input(
                    "Minimum reference games", min_value=1, value=20, key="odds_minimum"
                )
            )
            cutoff = (
                st.text_input(
                    "Only use matches settled before (optional)", key="odds_cutoff"
                ).strip()
                or None
            )
            resolution = st.select_slider(
                "Display grid resolution",
                [21, 51, 81, 101, 151],
                value=101,
                key="odds_resolution",
            )
    p, b, time = state["p"] / 100, state["b"] / 100, state["t"]
    d = float(distance(p, b))
    a, c, e = st.columns(3)
    outcome = a.selectbox(
        "Estimate", ["optimistic", "conservative"], key="odds_outcome"
    )
    smoothing_mode = c.selectbox(
        "Smoothing",
        ["Automatic · whole-game validation", "Manual by axis"],
        key="odds_smoothing_mode",
    )
    show_raw = e.checkbox("Show raw data", value=False, key="odds_show_raw")
    smoothing = "auto"
    if smoothing_mode == "Manual by axis":
        with st.sidebar:
            defaults = st.session_state.get("odds_validated_strengths", (1e-4, 1e-4))
            smoothing = tuple(
                10
                ** st.slider(
                    f"{axis} smoothing · log10",
                    -8.0,
                    -1.0,
                    float(np.log10(max(value, 1e-8))),
                    0.5,
                    key=f"odds_smooth_{axis}",
                )
                for axis, value in zip(["Distance", "Time"], defaults)
            )
    tolerances = (d_tol, t_tol)
    st.session_state.update(odds_tolerances=tolerances, odds_support_minimum=minimum)
    try:
        stat = Path(path).stat()
        signature = (
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
            family,
            ticker,
            cutoff,
            MODEL_VERSION,
        )
        model = cached_model(signature, outcome, smoothing)
        grid = cached_grid(
            signature, outcome, model.strengths, tolerances, minimum, resolution, model
        )
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return
    st.session_state["odds_validated_strengths"] = model.strengths
    price_effect = model.validation.get("diagnostics", {}).get(
        "within_distance_time_price_effect", {}
    )
    interval = price_effect.get("interval_95")
    if (
        price_effect.get("status") == "estimated"
        and interval
        and (interval[0] > 0 or interval[1] < 0)
    ):
        st.info(
            "Held-out games retain a measurable absolute-price effect within comparable distance/time regions. Odds moneyness only partially standardizes price; inspect the price-band calibration below."
        )
    selected = model.evaluate([[d, time]], tolerances, minimum)
    probability = float(selected["probability"][0])
    caption_slot = st.empty()
    with st.container(key="odds_readouts"):
        readout_slot = st.empty()
    with st.container(key="odds_coverage"):
        coverage_slot = st.empty()
    warning_slot = st.empty()
    if st.button("Reset camera", key="odds_reset_camera"):
        st.session_state["odds_camera_revision"] = (
            st.session_state.get("odds_camera_revision", 0) + 1
        )

    def remember_view():
        st.session_state["odds_active_view"] = st.session_state["odds_view"]

    active = st.session_state.get("odds_active_view", "Heatmap")
    st.session_state["odds_view"] = active
    heat_tab, surface_tab = st.tabs(
        ["Heatmap", "3D Surface"],
        key="odds_view",
        default=active,
        on_change=remember_view,
    )
    heat, surface = chart_figures(
        model,
        grid,
        d,
        time,
        probability,
        show_raw,
        st.session_state.get("odds_camera_revision", 0),
        tolerances,
        minimum,
    )
    with heat_tab:
        st.plotly_chart(heat, width="stretch", key="odds_heatmap")
    with surface_tab:
        st.html(
            '<style>.odds-inline{display:flex;gap:.5rem}.odds-inline button{font:inherit;color:inherit;background:transparent;border:1px solid #94a3b8;border-radius:.5rem;padding:.5rem 1rem;cursor:pointer}</style><div class="odds-inline"><button type="button" data-odds-playback="play">▶ Play time</button><button type="button" data-odds-playback="pause">Pause</button></div>'
        )
        st.plotly_chart(surface, width="stretch", key="odds_surface")
    token_base = hashlib.sha256(
        json.dumps(
            [
                signature,
                outcome,
                model.strengths,
                tolerances,
                minimum,
                resolution,
            ],
            default=str,
        ).encode()
    ).hexdigest()
    with st.expander("Whole-game validation and standardization diagnostics"):
        st.write(
            "A monotone cubic tensor-product binomial spline uses only [log-odds distance, normalized time]. Independent distance/time penalties are selected on whole games; test games are untouched during selection."
        )
        test_metrics = model.validation.get("test_metrics")
        if test_metrics:
            score_columns = st.columns(3)
            score_columns[0].metric(
                "Held-out Brier score", f"{test_metrics['brier']:.4f}"
            )
            score_columns[1].metric(
                "Held-out log loss", f"{test_metrics['log_loss']:.4f}"
            )
            score_columns[2].metric(
                "Independent test games", model.validation["test_events"]
            )
        diagnostics = model.validation.get("diagnostics", {})
        if diagnostics.get("price_bands"):
            calibration = go.Figure()
            for row in diagnostics["price_bands"]:
                points = row["calibration"]
                label = ["1–20¢", "20–40¢", "40–60¢", "60–80¢", "80–99¢"][row["group"]]
                calibration.add_trace(
                    go.Scatter(
                        x=[point["predicted"] for point in points],
                        y=[point["observed"] for point in points],
                        mode="lines+markers",
                        name=label,
                        customdata=[[point["trials"]] for point in points],
                        hovertemplate="Predicted %{x:.1%}<br>Observed %{y:.1%}<br>Trials %{customdata[0]}<extra>%{fullData.name}</extra>",
                    )
                )
            calibration.add_trace(
                go.Scatter(
                    x=[0, 1],
                    y=[0, 1],
                    mode="lines",
                    name="Perfect calibration",
                    line=dict(color="#94a3b8", dash="dot"),
                )
            )
            calibration.update_layout(
                height=440,
                xaxis=dict(
                    title="Predicted probability", range=[0, 1], tickformat=".0%"
                ),
                yaxis=dict(
                    title="Observed price-reaching rate", range=[0, 1], tickformat=".0%"
                ),
                margin=dict(l=60, r=30, t=55, b=70),
                legend=dict(orientation="h", y=1.02, yanchor="bottom"),
            )
            with st.container(border=True):
                st.plotly_chart(calibration, width="stretch", key="odds_calibration")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "calibration"
                        }
                        for row in diagnostics["price_bands"]
                    ]
                ),
                hide_index=True,
            )
        st.caption(
            "Price bands 0–4 correspond to <20¢, 20–40¢, 40–60¢, 60–80¢, ≥80¢. Residuals are observed minus predicted. Nonzero within-bin price effects suggest the collapse is incomplete."
        )
        st.json(model.validation)
    with st.expander("Finite-price filtering and model metadata"):
        st.json(model.audit)
        st.caption(
            f"Distance domain {model.bounds[0]:.4f}<d≤{model.bounds[1]:.4f}; time 0≤t<1. Support requires at least {minimum} distinct games in empirical bins within ±{d_tol:g} distance and ±{t_tol:g} time. Sparse/extrapolated estimates are masked. No synthetic bid-zero boundary."
        )
        artifact = dict(
            model_version=MODEL_VERSION,
            feature_order=["log_odds_distance", "normalized_time"],
            family=family,
            outcome=outcome,
            bounds=model.bounds,
            strengths=model.strengths,
            coefficients=model.weights.tolist(),
            audit=model.audit,
            validation=model.validation,
        )
        st.download_button(
            "Download odds-moneyness model",
            json.dumps(artifact, indent=2),
            f"{family}_odds_moneyness_model.json",
            "application/json",
            key="odds_download_model",
        )
    if show_raw:
        with st.expander("Raw binned observations"):
            st.dataframe(model.raw, hide_index=True)
    debug_slot = st.empty() if st.query_params.get("debug") == "1" else None
    with scenario_slot:
        scenario_panel(
            model,
            grid,
            tolerances,
            minimum,
            token_base,
            caption_slot,
            readout_slot,
            coverage_slot,
            warning_slot,
            debug_slot,
        )
