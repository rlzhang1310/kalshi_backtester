// The browser owns playback. Only pause, seeking and completion notify Python.
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  const root = parentElement.querySelector('.playback');
  // A new render cancels old timers and queued chart updates before they can run.
  root.__playback?.dispose();
  const slider = root.querySelector('#fill-time');
  const output = root.querySelector('#fill-time-value');
  const play = root.querySelector('#fill-play');
  const pause = root.querySelector('#fill-pause');
  const status = root.querySelector('#fill-playback-status');
  let disposed = false, playing = false, request = null, started = 0, startTime = 0;
  const timeState=retainedTime('fill',data.token,data.initial_time);
  let current = Math.round(timeState.time * 100), pending = null, busy = false, lastClosest = null;
  const graph = key => document.querySelector(`.st-key-${key} .js-plotly-plot`);
  const format = value => value == null ? 'Unsupported' : `${(value * 100).toFixed(2)}%`;
  const sync = () => {
    if (disposed) return;
    timeState.pending=current/100;
    setStateValue('selection', {time:current / 100, token:data.token,
      price:data.price, bid:data.bid, family:data.family, ticker:data.ticker});
  };
  const updateReadouts = index => {
    const values = document.querySelectorAll('.st-key-no_clv_readouts [data-testid="stMetricValue"]');
    const labels = [format(data.selected[index]), data.events[index] >= data.minimum ? format(data.raw[index]) : 'Insufficient support', data.status[index]];
    values.forEach((value, i) => {
      const text = value.querySelector('div') || value;
      if (i < labels.length) text.textContent = labels[i];
    });
    const coverage = document.querySelector('.st-key-no_clv_coverage p');
    if (coverage) coverage.textContent = `Local coverage: ${data.events[index]} matches · ${data.observations[index]} states. Rates pool all CLVs.`;
  };
  const apply = async index => {
    if (disposed || !globalThis.Plotly) return;
    const time = index / 100, selected = data.selected[index], frame = data.frames[index];
    const heat = graph('no_clv_heatmap'), surface = graph('no_clv_time_surface'), series = graph('no_clv_time_series');
    const jobs = [];
    if (heat?.data) {
      const custom = data.bids.map((bid, row) => data.prices.map((price, col) => {
        const synthetic = bid === 0, count = frame.events[row][col];
        const supported = time < 1 && bid < price && count >= data.minimum;
        const label = synthetic ? 'assumed boundary' : time === 1 || bid >= price ? 'outside horizon/domain' : supported ? 'supported' : count > 0 ? 'sparse' : 'unsupported';
        return [time, synthetic ? null : count, synthetic ? null : frame.observations[row][col], synthetic, label];
      }));
      jobs.push((async () => {
        await Plotly.restyle(heat, {z:[frame.z], customdata:[custom]}, [0]);
        if (!disposed) await Plotly.restyle(heat, {customdata:[[[time,selected]]]}, [1]);
      })());
    }
    if (surface?.data) {
      jobs.push(Plotly.restyle(surface, {x:[[time]], z:[[selected]]}, [surface.data.length - 1]));
      const slice = surface.data.findIndex(trace => trace.name === 'Current time slice');
      if (slice >= 0) jobs.push(Plotly.restyle(surface, {x:[data.bids.map(() => time)], y:[data.bids], z:[data.slices[index]]}, [slice]));
    }
    if (series?.data) {
      jobs.push((async () => {
        await Plotly.restyle(series, {x:[[time]], y:[[selected]]}, [series.data.length - 1]);
        if (!disposed) await Plotly.relayout(series, {'shapes[0].x0':time, 'shapes[0].x1':time});
      })());
    }
    updateReadouts(index);
    {
      const times = [0.1,0.3,0.5,0.7,0.9];
      const closest = times.reduce((a,b) => Math.abs(a-time) <= Math.abs(b-time) ? a : b);
      if (closest !== lastClosest) {
        lastClosest = closest;
        times.forEach((value, i) => {
          const snapshot = graph(`no_clv_snapshot_${i}`);
          if (snapshot) jobs.push(Plotly.relayout(snapshot, {'title.text':`t = ${value.toFixed(2)}${value === closest ? ' · closest to selection' : ''}`}));
        });
      }
    }
    await Promise.all(jobs);
  };
  // Serial updates, retaining only the most recent requested frame. Slow WebGL
  // draws never create a backlog, and camera/layout are never recreated.
  const drain = async () => {
    if (busy || disposed) return;
    busy = true;
    try {
      while (pending != null && !disposed) {
        const index = pending; pending = null;
        await apply(index);
      }
    } catch (error) {
      playing = false;
      play.disabled = false; pause.disabled = true;
      status.textContent = 'Playback stopped. Select a time or reload the view.';
    } finally { busy = false; }
  };
  const select = (index, draw=true) => {
    current = Math.max(0, Math.min(100, index));
    timeState.time=current/100;
    slider.value = String(current); output.value = (current / 100).toFixed(2);
    if(draw){pending = current; void drain();}
  };
  const stop = commit => {
    playing = false;
    if (request != null) cancelAnimationFrame(request);
    request = null; play.disabled = false; pause.disabled = true;
    if (commit) sync();
  };
  const tick = now => {
    if (!playing || disposed) return;
    const index = Math.min(100, Math.round(startTime + (now - started) / 125));
    if (index !== current) select(index);
    if (current === 100) { stop(true); return; }
    request = requestAnimationFrame(tick);
  };
  play.onclick = () => {
    if (playing || disposed) return;
    if (current === 100) select(0);
    playing = true; started = performance.now(); startTime = current;
    play.disabled = true; pause.disabled = false;
    status.textContent = 'Playing · no page reloads';
    request = requestAnimationFrame(tick);
  };
  pause.onclick = () => { stop(true); status.textContent = 'Paused'; };
  const onControl = event => {
    const button = event.target.closest?.('[data-fill-playback]');
    if (!button || disposed) return;
    if (button.dataset.fillPlayback === 'play') play.onclick();
    if (button.dataset.fillPlayback === 'pause') pause.onclick();
  };
  document.addEventListener('click', onControl);
  const disposeSeek=bindTimeSeek(slider, {
    interaction:timeState,
    onStart:()=>{stop(false);pending=null;status.textContent='Paused';},
    onDrag:index=>{select(index,false);status.textContent='Selecting time…';},
    onPreview:index=>select(index), onCommit:()=>{status.textContent='Paused';sync();},
  });
  const onVisibility = () => { if (document.hidden && playing) stop(true); };
  document.addEventListener('visibilitychange', onVisibility);
  const state = {dispose:() => {
    if(disposed)return;
    disposed = true; pending = null; stop(false);
    disposeSeek();
    play.onclick = null; pause.onclick = null; slider.oninput = null; slider.onchange = null;
    document.removeEventListener('visibilitychange', onVisibility);
    document.removeEventListener('click', onControl);
  }};
  root.__playback = state;
  slider.value = String(current); output.value = (current / 100).toFixed(2);
  if(!timeState.dirty&&timeState.time!==data.initial_time){pending=current;void drain();}
  // Streamlit runs this cleanup before remounting and on page navigation.
  return () => { state.dispose(); if (root.__playback === state) delete root.__playback; };
}
