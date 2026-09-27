"""Browser-side time playback, with exact cached predictions and pause/seek sync."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np
from plotly.offline import get_plotlyjs
import streamlit as st

from .no_clv_fill import heatmap_grid


def nullable(values):
    values = np.asarray(values)
    return np.where(np.isfinite(values), values, None).tolist()


@st.cache_data(max_entries=3, show_spinner="Preparing smooth time playback…")
def cached_frames(signature, tolerances, minimum, resolution, _model):
    frames = []
    for index in range(101):
        time = index / 100
        grid = heatmap_grid(_model, float(time), tolerances, minimum, resolution)
        frames.append(
            dict(
                z=nullable(grid["probability"]),
                events=grid["n_events"].tolist(),
                observations=grid["n_observations"].tolist(),
            )
        )
    return dict(
        frames=frames,
        prices=(grid["prices"] * 100).tolist(),
        bids=(grid["bids"] * 100).tolist(),
    )


def playback_data(
    model, frames, price, bid, time, tolerances, minimum, token, family, ticker
):
    times = np.arange(101) / 100
    points = np.column_stack(
        [np.full(len(times), price), times, np.full(len(times), bid)]
    )
    selected = model.evaluate(points, tolerances, minimum)
    raw = model.empirical(points, tolerances)
    slice_times, slice_bids = np.meshgrid(
        times, np.asarray(frames["bids"]) / 100, indexing="ij"
    )
    slice_points = np.column_stack(
        [np.full(slice_times.size, price), slice_times.ravel(), slice_bids.ravel()]
    )
    slices = model.evaluate(slice_points, tolerances, minimum)["probability"].reshape(
        slice_times.shape
    )
    return dict(
        **frames,
        slices=nullable(slices),
        initial_time=time,
        price=price,
        bid=bid,
        token=token,
        family=family,
        ticker=ticker,
        minimum=minimum,
        selected=nullable(selected["probability"]),
        status=selected["status"].tolist(),
        raw=nullable(raw.probability),
        events=raw.n_events.tolist(),
        observations=raw.n_observations.tolist(),
    )


def commit_playback_time():
    selection = st.session_state.get("no_clv_playback", {}).get("selection")
    state = st.session_state.get("no_clv_scenario")
    if (
        not selection
        or not state
        or selection.get("token") != st.session_state.get("no_clv_playback_token")
    ):
        return
    if selection.get("family") != st.session_state.get("family"):
        return
    if selection.get("ticker") != st.session_state.get(f"ticker_{selection['family']}"):
        return
    if (
        selection.get("price") != state["p"] / 100
        or selection.get("bid") != state["b"] / 100
    ):
        return
    time = selection.get("time")
    if isinstance(time, (int, float)) and np.isfinite(time) and 0 <= time <= 1:
        state["t"] = round(float(time), 2)


@lru_cache(maxsize=1)
def component_source():
    # Ship the installed Plotly bundle locally; no CDN or network dependency.
    javascript = (
        Path(__file__).with_name("fill_playback.js").read_text(encoding="utf-8")
    )
    seek = Path(__file__).with_name("time_seek.js").read_text(encoding="utf-8")
    return (
        "if (!globalThis.Plotly) {\n"
        + get_plotlyjs()
        + "\n}\n"
        + seek
        + "\n"
        + javascript
    )


def playback_controls(
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
    slot=None,
):
    token = hashlib.sha256(
        json.dumps(
            [signature, price, bid, tolerances, minimum, resolution], default=str
        ).encode()
    ).hexdigest()
    st.session_state["no_clv_playback_token"] = token
    frames = cached_frames(signature, tolerances, minimum, resolution, model)
    data = playback_data(
        model, frames, price, bid, time, tolerances, minimum, token, family, ticker
    )
    component = st.components.v2.component(
        "fill_time_playback",
        html="""<div class="playback">
          <label for="fill-time">Normalized time <output id="fill-time-value"></output></label>
          <input id="fill-time" type="range" min="0" max="100" step="1" aria-label="Normalized time" />
          <div class="buttons"><button id="fill-play" type="button">▶ Play time</button>
          <button id="fill-pause" type="button" disabled>Ⅱ Pause</button></div>
          <p id="fill-playback-status" role="status">Retrospective · start to settlement</p>
        </div>""",
        css=""".playback {font:inherit;color:var(--st-text-color);padding:6px 0}
          label {display:flex;justify-content:space-between;font-size:14px;margin-bottom:12px}
          output {font-variant-numeric:tabular-nums} input {width:100%;accent-color:#ef4444;cursor:pointer}
          .buttons {display:flex;gap:8px;margin-top:12px}
          button {flex:1;padding:8px;border:1px solid #94a3b866;border-radius:8px;background:transparent;color:inherit;cursor:pointer}
          button:focus-visible,input:focus-visible {outline:2px solid #ef4444;outline-offset:3px}
          button:disabled {opacity:.45;cursor:default} p {font-size:12px;opacity:.7;margin:10px 0 0}
        """,
        js=component_source(),
    )
    with slot if slot is not None else st.sidebar:
        component(
            data=data, key="no_clv_playback", on_selection_change=commit_playback_time
        )
