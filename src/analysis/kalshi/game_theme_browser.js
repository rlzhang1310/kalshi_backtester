(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;
  let dark = {initial_dark};

  function themedFigure(figure) {
    const layout = figure.layout;
    const paper = dark ? '#111827' : '#ffffff';
    const ink = dark ? '#e5e7eb' : '#172033';
    const grid = dark ? '#334155' : 'rgba(0,0,0,0.08)';
    delete layout.template;
    layout.paper_bgcolor = paper;
    layout.plot_bgcolor = paper;
    layout.font = {...layout.font, color: ink};
    layout.title = {...layout.title, font: {...layout.title?.font, color: ink}};
    layout.legend = {
      ...layout.legend,
      bgcolor: dark ? 'rgba(30,41,59,0.95)' : 'rgba(255,255,255,0.92)',
      bordercolor: dark ? '#64748b' : 'rgba(0,0,0,0.2)',
      font: {...layout.legend?.font, color: ink},
      title: {...layout.legend?.title, font: {...layout.legend?.title?.font, color: ink}}
    };
    layout.hoverlabel = {
      ...layout.hoverlabel,
      bgcolor: dark ? '#1e293b' : '#ffffff',
      font: {...layout.hoverlabel?.font, color: ink}
    };
    for (const [key, axis] of Object.entries(layout)) {
      if (!/^[xy]axis\d*$/.test(key) || !axis) continue;
      layout[key] = {
        ...axis,
        gridcolor: grid,
        zerolinecolor: grid,
        tickfont: {...axis.tickfont, color: ink},
        title: {...axis.title, font: {...axis.title?.font, color: ink}}
      };
    }
    for (const trace of figure.data) {
      const colors = {
        Kalshi: dark ? '#60a5fa' : '#1f4aff',
        'Kalshi taker': dark ? '#fb7185' : '#ff6b57',
        'Kalshi maker': dark ? '#34d399' : '#00b894'
      };
      if (colors[trace.name]) trace.line = {...trace.line, color: colors[trace.name]};
      if (trace.name === 'Volume') {
        trace.marker = {...trace.marker, color: dark ? '#94a3b8' : '#8294ba'};
      }
    }
    document.documentElement.style.background = paper;
    document.body.style.background = paper;
    document.body.style.color = ink;
    const status = document.getElementById('game-live-status');
    if (status) {
      status.style.background = dark ? '#1e293b' : '#ffffff';
      status.style.color = ink;
      status.style.borderColor = dark ? '#64748b' : '#ccd3df';
    }
    for (const button of document.querySelectorAll('.game-zoom-control')) {
      button.style.background = dark ? '#1e293b' : '#ffffff';
      button.style.color = ink;
      button.style.borderColor = dark ? '#64748b' : '#ccd3df';
    }
    return figure;
  }

  window.gameThemeFigure = themedFigure;
  window.addEventListener('message', async event => {
    if (event.source !== window.parent || event.data?.type !== 'game-chart-theme') return;
    const next = Boolean(event.data.dark);
    if (next === dark) return;
    dark = next;
    const figure = {
      data: graph.data.map(trace => ({...trace})),
      layout: {...graph.layout}
    };
    await Plotly.react(graph, themedFigure(figure).data, figure.layout, graph._context);
  });
})();
