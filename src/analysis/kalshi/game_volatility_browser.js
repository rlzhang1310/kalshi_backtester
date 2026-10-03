(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;

  const panel = document.createElement('section');
  panel.id = 'game-volatility-panel';
  panel.setAttribute('aria-label', 'Rolling realized volatility');
  panel.style.cssText = 'box-sizing:border-box;margin:0 16px;padding:9px 12px;border:1px solid;border-radius:7px;font:13px sans-serif;min-height:82px';
  const heading = document.createElement('div');
  heading.style.cssText = 'font-weight:600;margin-bottom:5px';
  const row = document.createElement('div');
  row.style.cssText = 'display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px';
  panel.append(heading, row);
  graph.insertAdjacentElement('afterend', panel);
  const labels = ['1m', '5m', '10m', '30m'];
  const cells = labels.map(label => {
    const cell = document.createElement('div');
    cell.style.cssText = 'white-space:pre-line;overflow:hidden';
    row.appendChild(cell);
    return cell;
  });

  let cursor = null;
  function utcTime(value) {
    if (value instanceof Date) return value.getTime();
    if (typeof value === 'number') return value;
    const timestamp = String(value).replace(' ', 'T');
    return Date.parse(/(?:[Zz]|[+-]\d\d:?\d\d)$/.test(timestamp) ? timestamp : timestamp + 'Z');
  }

  function render() {
    const payload = graph.layout.meta?.rolling_volatility;
    const samples = payload?.samples || [];
    const dark = graph.layout.paper_bgcolor === '#111827';
    panel.style.background = dark ? '#1e293b' : '#ffffff';
    panel.style.color = dark ? '#f1f5f9' : '#172033';
    panel.style.borderColor = dark ? '#475569' : '#ccd3df';
    const asOf = cursor === null ? (samples.at(-1)?.t ?? null) : cursor;
    heading.textContent = 'Realized volatility' + (cursor === null ? '' : ' · as of chart cursor');
    let left = 0;
    let right = samples.length;
    while (left < right) {
      const middle = (left + right) >> 1;
      if (samples[middle].t <= asOf) left = middle + 1;
      else right = middle;
    }
    const sample = left ? samples[left - 1] : null;
    const fresh = sample && asOf - sample.t <= payload.cadence_seconds * 1000;
    const formula = '100 × √Σ(Δp)², in percentage points; observed last trade per '
      + (payload?.cadence_seconds ? payload.cadence_seconds + 's' : 'selected time')
      + ' bucket (no interpolation). '
      + (payload?.source || 'Trade-implied probability') + '.';
    cells.forEach((cell, index) => {
      const value = fresh ? sample.values[index] : null;
      const normalized = fresh ? sample.per_minute[index] : null;
      const reason = !sample ? 'no observed prices' : !fresh ? 'stale price / unsupported resolution'
        : sample.reasons[index];
      cell.textContent = labels[index] + ': ' + (value === null ? 'N/A' : value.toFixed(2) + ' pp')
        + '\n1m equivalent: ' + (normalized === null ? 'N/A' : normalized.toFixed(2) + ' pp');
      cell.title = formula + ' ' + labels[index] + ' requires start coverage within '
        + payload.start_tolerance_seconds[index] + 's and no gap over '
        + payload.max_gap_seconds[index] + 's.'
        + ' 1m equivalent = accumulated volatility / sqrt(window minutes);'
        + ' this is a time-normalized comparison, not a forecast.'
        + (reason ? ' N/A: ' + reason + '.' : '');
    });
  }

  graph.on('plotly_hover', event => {
    const timestamp = utcTime(event.points?.[0]?.x);
    if (Number.isFinite(timestamp)) {
      cursor = timestamp;
      render();
    }
  });
  graph.on('plotly_unhover', () => { cursor = null; render(); });
  graph.on('plotly_afterplot', render);
  window.gameUpdateVolatility = render;
  render();
})();
