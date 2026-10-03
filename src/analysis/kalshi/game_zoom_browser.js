(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;

  function timestamp(value) {
    if (typeof value === 'number') return value;
    const text = String(value).replace(' ', 'T');
    return Date.parse(/(?:[Zz]|[+-]\d\d:?\d\d)$/.test(text) ? text : text + 'Z');
  }

  function bounds() {
    const axis = graph._fullLayout?.xaxis;
    if (!axis?.range) return null;
    const [left, right] = axis.range.map(timestamp);
    const floor = timestamp(axis.minallowed);
    const ceiling = timestamp(axis.maxallowed);
    return [left, right, floor, ceiling].every(Number.isFinite)
      ? {left, right, floor, ceiling} : null;
  }

  function setRange(left, right) {
    const range = [new Date(left).toISOString(), new Date(right).toISOString()];
    const update = {};
    for (const key of ['xaxis', 'xaxis2', 'xaxis3']) {
      if (graph.layout[key]) update[key + '.range'] = range;
    }
    Plotly.relayout(graph, update);
  }

  function zoomIn() {
    const view = bounds();
    if (!view) return;
    const inset = (view.right - view.left) * 0.125;
    setRange(view.left + inset, view.right - inset);
  }

  function zoomOut() {
    const view = bounds();
    if (!view) return;
    const span = view.right - view.left;
    const nextSpan = Math.min(view.ceiling - view.floor, span * 1.25);
    let left = Math.max(view.floor, view.left - (nextSpan - span) * 0.25);
    let right = left + nextSpan;
    if (right > view.ceiling) {
      right = view.ceiling;
      left = Math.max(view.floor, right - nextSpan);
    }
    setRange(left, right);
  }

  function fitTrades() {
    const initial = graph.layout.meta?.time_bounds;
    if (!initial) return;
    setRange(timestamp(initial.left), timestamp(initial.right));
  }

  const controls = document.createElement('div');
  controls.style.cssText = 'position:fixed;top:56px;right:12px;z-index:25;display:flex;gap:5px';
  const dark = graph.layout.paper_bgcolor === '#111827';
  for (const [label, title, action] of [
    ['+', 'Zoom in on time', zoomIn],
    ['−', 'Zoom out on time', zoomOut],
    ['Fit', 'Fit all trades', fitTrades]
  ]) {
    const button = document.createElement('button');
    button.className = 'game-zoom-control';
    button.textContent = label;
    button.title = title;
    button.style.cssText = 'min-width:34px;padding:6px 9px;border-radius:5px;cursor:pointer;font:13px sans-serif;box-shadow:0 1px 4px #0003';
    button.style.background = dark ? '#1e293b' : '#ffffff';
    button.style.color = dark ? '#f1f5f9' : '#172033';
    button.style.border = '1px solid ' + (dark ? '#64748b' : '#ccd3df');
    button.addEventListener('click', action);
    controls.appendChild(button);
  }
  document.body.appendChild(controls);
})();
