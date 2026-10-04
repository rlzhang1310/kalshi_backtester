(function () {
  if (new URLSearchParams(window.location.search).get('fullscreen') !== '1') return;
  const graph = document.getElementById('{plot_id}');
  if (!graph || !window.Plotly) return;

  document.documentElement.style.height = '100%';
  document.body.style.margin = '0';
  document.body.style.height = '100%';
  document.body.style.overflow = 'hidden';
  const panel = document.getElementById('game-volatility-panel');
  if (panel) {
    panel.style.position = 'fixed';
    panel.style.left = '0';
    panel.style.right = '0';
    panel.style.bottom = '0';
    panel.style.zIndex = '25';
    panel.style.margin = '0';
  }

  function viewportSize() {
    return {
      width: Math.max(320, window.innerWidth - 2),
      height: Math.max(240, window.innerHeight - (panel?.offsetHeight || 0) - 12),
      autosize: false
    };
  }

  // Live snapshots arrive with the embedded chart's height. Keep this tab
  // fitted to its own viewport instead of shrinking after the first update.
  window.gameFitToViewport = layout => Object.assign(layout, viewportSize());
  function resize() { Plotly.relayout(graph, viewportSize()); }
  window.addEventListener('resize', resize);
  document.addEventListener('fullscreenchange', resize);
  resize();

  const button = document.createElement('button');
  button.textContent = 'Enter browser fullscreen';
  button.style.cssText = 'position:fixed;right:12px;z-index:30;padding:7px 10px;border-radius:6px;cursor:pointer;background:#1e293b;color:#fff;border:1px solid #64748b';
  function positionButton() {
    button.style.bottom = ((document.getElementById('game-volatility-panel')?.offsetHeight || 0) + 12) + 'px';
  }
  window.addEventListener('resize', positionButton);
  positionButton();
  button.addEventListener('click', async () => {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
    button.textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Enter browser fullscreen';
  });
  document.body.appendChild(button);
  if (panel && window.ResizeObserver) {
    new ResizeObserver(() => { resize(); positionButton(); }).observe(panel);
  }
})();
