(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;
  const liveClock = {live_clock};

  const panel = document.createElement('section');
  panel.id = 'game-volatility-panel';
  panel.setAttribute('aria-label', 'Rolling price movement metrics');
  panel.style.cssText = 'box-sizing:border-box;margin:0 16px;padding:9px 12px;border:1px solid;border-radius:7px;font:13px sans-serif;min-height:205px';
  const gameHeading = document.createElement('div');
  gameHeading.style.cssText = 'font-weight:600;margin-bottom:4px';
  const gameRow = document.createElement('div');
  gameRow.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px';
  const gameCells = Array.from({length: 4}, () => {
    const cell = document.createElement('div');
    cell.style.cssText = 'white-space:pre-line;min-width:0';
    gameRow.appendChild(cell);
    return cell;
  });
  const divider = document.createElement('div');
  divider.style.cssText = 'border-top:1px solid;opacity:.35;margin:9px 0 7px';
  const heading = document.createElement('div');
  heading.style.cssText = 'font-weight:600;margin-bottom:3px';
  const status = document.createElement('div');
  status.style.cssText = 'font-size:11px;opacity:.78;margin-bottom:6px';
  const row = document.createElement('div');
  row.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px';
  panel.append(gameHeading, gameRow, divider, heading, status, row);
  graph.insertAdjacentElement('afterend', panel);
  const cells = Array.from({length: 4}, () => {
    const cell = document.createElement('div');
    cell.style.cssText = 'white-space:pre-line;min-width:0';
    row.appendChild(cell);
    return cell;
  });

  let cursor = null;
  let latest = graph.layout.meta?.rolling_volatility || null;
  let requestId = 0;
  let pending = null;
  let hoverTimer = null;
  let settledAt = null;

  function utcTime(value) {
    if (value instanceof Date) return value.getTime();
    if (typeof value === 'number') return value;
    const timestamp = String(value).replace(' ', 'T');
    return Date.parse(/(?:[Zz]|[+-]\d\d:?\d\d)$/.test(timestamp) ? timestamp : timestamp + 'Z');
  }
  function windowLabel(seconds) {
    return seconds < 60 ? seconds + 's' : seconds / 60 + 'm';
  }
  function metricLines(metrics, index) {
    const value = key => index === null ? metrics[key] : metrics[key][index];
    return [
      'Vol ' + value('values').toFixed(2) + ' pp · 1m eq ' + value('per_minute').toFixed(2) + ' pp',
      'Up ' + value('up').toFixed(2) + ' pp · 1m eq ' + value('up_per_minute').toFixed(2) + ' pp',
      'Down ' + value('down').toFixed(2) + ' pp · 1m eq ' + value('down_per_minute').toFixed(2) + ' pp',
      'Two-way ' + value('two_way').toFixed(2) + ' pp · 1m eq ' + value('two_way_per_minute').toFixed(2) + ' pp',
    ];
  }
  function metricTooltip(payload, coverage) {
    return 'Price: ' + payload.source + '. Last known price carried forward at '
      + payload.cadence_seconds + 's intervals; no earlier unknown price is filled. '
      + 'Underlying squared sums U and D use positive and negative probability changes. '
      + 'Vol = sqrt(U + D); Up = sqrt(U); Down = sqrt(D); Two-way = sqrt(2 × min(U, D)). '
      + 'All displayed values are in pp (100 × the decimal result), and all 1m equivalents '
      + 'scale by sqrt(60 / covered seconds). '
      + 'Two-way is zero for one-way movement and weights larger opposing moves more heavily; '
      + 'it does not prove uncertainty or fully remove trends. Coverage: ' + coverage + ' seconds.';
  }
  function render(payload) {
    latest = payload;
    const dark = graph.layout.paper_bgcolor === '#111827';
    panel.style.background = dark ? '#1e293b' : '#ffffff';
    panel.style.color = dark ? '#f1f5f9' : '#172033';
    panel.style.borderColor = dark ? '#475569' : '#ccd3df';
    divider.style.borderColor = dark ? '#94a3b8' : '#64748b';
    const game = payload?.game;
    gameHeading.textContent = 'Game to date' + (cursor === null ? '' : ' · as of chart cursor')
      + (game ? ' · ' + game.coverage_seconds + 's covered' : '');
    gameCells.forEach((cell, index) => {
      if (!game) {
        cell.textContent = ['Vol', 'Up', 'Down', 'Two-way'][index] + ': Loading';
        cell.title = 'Waiting for the first valid target-side price.';
        return;
      }
      cell.textContent = metricLines(game, null)[index];
      cell.title = metricTooltip(payload, game.coverage_seconds);
    });
    heading.textContent = 'Price movement' + (cursor === null ? '' : ' · as of chart cursor');
    const sample = payload?.samples?.[0];
    const windows = payload?.windows_seconds || [60, 300, 600, 1800];
    const connection = window.gameFeedConnection;
    const feed = connection ? ' · feed: ' + connection : '';
    if (!sample) {
      status.textContent = 'Loading — no valid price yet' + feed;
    } else {
      const age = Math.max(0, Math.floor((payload.as_of_ms - payload.last_trade_ms) / 1000));
      const stale = age > Math.max(30, 3 * payload.cadence_seconds);
      status.textContent = age
        ? (stale ? 'Stale price — ' : '') + 'carried forward for ' + age + 's (not a new observation)' + feed
        : 'Price from latest observed trade' + feed;
    }
    cells.forEach((cell, index) => {
      const label = windowLabel(windows[index]);
      if (!sample) {
        cell.textContent = label + ': Loading';
        cell.title = 'Waiting for the first valid target-side price.';
        return;
      }
      const coverage = sample.coverage_seconds[index];
      const partial = sample.partial[index];
      cell.textContent = label + (partial ? ' · partial ' + coverage + '/' + windows[index] + 's' : '')
        + '\n' + metricLines(sample, index).join('\n');
      cell.title = metricTooltip(payload, coverage);
    });
  }

  async function refresh() {
    const id = ++requestId;
    if (pending) pending.abort();
    pending = new AbortController();
    if (liveClock && window.gameFeedConnection === 'settled' && settledAt === null) {
      settledAt = Date.now();
    }
    const at = cursor === null ? (liveClock ? (settledAt ?? Date.now()) : null) : cursor;
    const url = '/metrics' + (at === null ? '' : '?at=' + encodeURIComponent(Math.trunc(at)));
    try {
      const response = await fetch(url, {cache: 'no-store', signal: pending.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const payload = await response.json();
      if (id === requestId) render(payload);
    } catch (error) {
      if (error.name !== 'AbortError' && id === requestId) {
        status.textContent = 'Metric refresh failed — last values may be stale';
      }
    }
  }

  graph.on('plotly_hover', event => {
    const at = utcTime(event.points?.[0]?.x);
    if (Number.isFinite(at)) {
      cursor = at;
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = setTimeout(refresh, 80);
    }
  });
  graph.on('plotly_unhover', () => {
    if (hoverTimer) clearTimeout(hoverTimer);
    cursor = null;
    refresh();
  });
  graph.on('plotly_afterplot', () => { if (latest) render(latest); });
  window.gameUpdateVolatility = refresh;
  if (latest) render(latest);
  refresh();
  if (liveClock) setInterval(refresh, 1000);
})();
