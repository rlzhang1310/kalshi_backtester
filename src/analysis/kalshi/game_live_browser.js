(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;
  const interval = {refresh_ms};
  let revision = 0;
  let live = true;
  let refreshable = false;

  const status = document.createElement('div');
  status.id = 'game-live-status';
  const dark = graph.layout.paper_bgcolor === '#111827';
  status.style.cssText = 'position:fixed;top:12px;right:12px;z-index:20;padding:6px 10px;border-radius:6px;font:14px sans-serif;box-shadow:0 1px 4px #0004';
  status.style.background = dark ? '#1e293b' : '#ffffff';
  status.style.color = dark ? '#f1f5f9' : '#172033';
  status.style.border = '1px solid ' + (dark ? '#64748b' : '#ccd3df');
  status.textContent = 'Checking chart status';
  document.body.appendChild(status);

  function utcTime(value) {
    if (typeof value === 'number') return value;
    const timestamp = String(value).replace(' ', 'T');
    return Date.parse(/(?:[Zz]|[+-]\d\d:?\d\d)$/.test(timestamp) ? timestamp : timestamp + 'Z');
  }

  function keepViewport(layout) {
    const oldAxis = graph._fullLayout && graph._fullLayout.xaxis;
    const nextAxis = layout.xaxis;
    if (!oldAxis || !oldAxis.range || !nextAxis) return;
    const oldStart = utcTime(oldAxis.minallowed);
    const oldEnd = utcTime(graph.layout.meta?.time_bounds?.right || oldAxis.range[1]);
    const nextStart = utcTime(nextAxis.minallowed);
    const nextEnd = utcTime(layout.meta?.time_bounds?.right || nextAxis.range[1]);
    const nextSoftEnd = utcTime(nextAxis.maxallowed);
    const viewStart = utcTime(oldAxis.range[0]);
    const viewEnd = utcTime(oldAxis.range[1]);
    if (![oldStart, oldEnd, nextStart, nextEnd, nextSoftEnd, viewStart, viewEnd].every(Number.isFinite)) return;
    const fullSpan = oldEnd - oldStart;
    const viewSpan = viewEnd - viewStart;
    if (fullSpan <= 0 || viewSpan <= 0) return;
    const atFullRange = Math.abs(viewStart - oldStart) < fullSpan * 0.01 &&
      Math.abs(viewEnd - oldEnd) < fullSpan * 0.01;
    if (atFullRange) return;

    // A viewport at the right edge follows incoming trades; other zooms stay put.
    let left = viewStart;
    if (Math.abs(viewEnd - oldEnd) < fullSpan * 0.01) left += nextEnd - oldEnd;
    left = Math.max(nextStart, Math.min(left, nextSoftEnd - viewSpan));
    const range = [new Date(left).toISOString(), new Date(left + viewSpan).toISOString()];
    for (const key of ['xaxis', 'xaxis2', 'xaxis3']) {
      if (layout[key]) layout[key].range = range;
    }
  }

  async function poll() {
    try {
      const response = await fetch('/snapshot?since=' + revision, {cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const snapshot = await response.json();
      if (snapshot.figure) {
        // Plotly.react otherwise restores the server's default visibility on every update.
        const visibility = new Map(graph.data.map(trace => [trace.name, trace.visible]));
        for (const trace of snapshot.figure.data) {
          if (visibility.has(trace.name)) trace.visible = visibility.get(trace.name);
        }
        snapshot.figure.layout.uirevision = graph.layout.uirevision || 'game-chart';
        snapshot.figure.layout.legend = snapshot.figure.layout.legend || {};
        snapshot.figure.layout.legend.uirevision = snapshot.figure.layout.uirevision;
        keepViewport(snapshot.figure.layout);
        if (window.gameThemeFigure) window.gameThemeFigure(snapshot.figure);
        if (window.gameFitToViewport) window.gameFitToViewport(snapshot.figure.layout);
        await Plotly.react(graph, snapshot.figure.data, snapshot.figure.layout, graph._context);
        if (window.gameUpdateVolatility) window.gameUpdateVolatility();
      }
      revision = snapshot.revision;
      live = snapshot.live;
      refreshable = Boolean(snapshot.refreshable);
      if (snapshot.connection === 'authentication failed') {
        status.textContent = 'Kalshi WebSocket authentication failed';
      } else if (snapshot.connection === 'subscription failed') {
        status.textContent = 'Kalshi trade subscription failed';
      } else if (snapshot.connection === 'reconnecting') {
        status.textContent = 'Kalshi WebSocket reconnecting';
      } else if (snapshot.connection === 'connecting') {
        status.textContent = 'Connecting to Kalshi WebSocket';
      } else if (snapshot.connection === 'paused') {
        status.textContent = 'API chart - updates paused';
      } else if (snapshot.connection === 'static') {
        status.textContent = 'API chart - fixed time range';
      } else if (!live) {
        status.textContent = 'Settled · live updates stopped';
      } else if (snapshot.connection === 'connected') {
        status.textContent = 'Live stream · ' + new Date().toLocaleTimeString();
      } else {
        status.textContent = 'Live · checked ' + new Date().toLocaleTimeString();
      }
    } catch (error) {
      status.textContent = 'Live · refresh failed; retrying';
    }
    if (live || refreshable) setTimeout(poll, interval);
  }

  setTimeout(poll, interval);
})();
