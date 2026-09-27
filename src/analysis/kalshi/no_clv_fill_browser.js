// Persist the no-CLV 3D camera across Streamlit fragment/slider redraws.
(() => {
  const prior = window.__noClvPresentation;
  if (prior) prior.dispose();
  const state = {camera: prior?.camera, revision: prior?.revision, graph: null, restoring: false, disposed: false};
  const copy = value => JSON.parse(JSON.stringify(value));
  const restore = () => {
    const graph = state.graph;
    if (!graph?._fullLayout?.scene || !window.Plotly || state.restoring) return;
    const revision = graph.layout?.scene?.uirevision;
    if (state.revision !== revision) { state.revision = revision; state.camera = null; }
    if (state.camera && JSON.stringify(graph._fullLayout.scene.camera) !== JSON.stringify(state.camera)) {
      state.restoring = true;
      Promise.resolve(window.Plotly.relayout(graph, {'scene.camera': copy(state.camera)}))
        .finally(() => { state.restoring = false; });
    }
  };
  const remember = event => {
    if (state.restoring || !Object.keys(event).some(key => key.startsWith('scene.camera'))) return;
    state.camera = copy(state.graph._fullLayout.scene.camera);
    state.revision = state.graph.layout?.scene?.uirevision;
  };
  const detach = () => {
    state.graph?.removeListener?.('plotly_relayout', remember);
    state.graph?.removeListener?.('plotly_afterplot', restore);
  };
  const scan = () => {
    if (state.disposed) return;
    const graph = document.querySelector('.st-key-no_clv_time_surface .js-plotly-plot');
    if (graph !== state.graph) {
      detach(); state.graph = graph;
      if (graph?.on) { graph.on('plotly_relayout', remember); graph.on('plotly_afterplot', restore); restore(); }
    }
  };
  const resize = () => {
    document.querySelectorAll('[class*="st-key-no_clv_"] .js-plotly-plot')
      .forEach(graph => window.Plotly?.Plots.resize(graph));
  };
  const observer = new MutationObserver(scan);
  state.dispose = () => { state.disposed = true; observer.disconnect(); window.removeEventListener('resize', resize); detach(); };
  window.__noClvPresentation = state;
  observer.observe(document.body, {childList: true, subtree: true});
  window.addEventListener('resize', resize);
  scan();
})();
