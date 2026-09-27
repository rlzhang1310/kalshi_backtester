// Browser-only presentation state. No model inputs or market data live here.
(() => {
  const prior = window.__fillSurfacePresentation;
  if (prior) prior.dispose();
  const state = {
    camera: prior?.camera,
    revision: prior?.revision,
    graph: null,
    disposed: false,
    restoring: false,
    scheduled: false,
  };
  const copy = value => JSON.parse(JSON.stringify(value));
  const revision = () => state.graph?.layout?.scene?.uirevision;
  const restore = () => {
    const graph = state.graph;
    if (!graph?._fullLayout?.scene || !window.Plotly || state.restoring) return;
    const currentRevision = revision();
    if (state.revision !== currentRevision) {
      state.revision = currentRevision;
      state.camera = null;
    }
    if (state.camera && JSON.stringify(graph._fullLayout.scene.camera) !== JSON.stringify(state.camera)) {
      state.restoring = true;
      Promise.resolve(window.Plotly.relayout(graph, {"scene.camera": copy(state.camera)}))
        .finally(() => { state.restoring = false; });
    }
  };
  const remember = event => {
    if (state.restoring || !Object.keys(event).some(key => key.startsWith("scene.camera"))) return;
    state.camera = copy(state.graph._fullLayout.scene.camera);
    state.revision = revision();
  };
  const detach = () => {
    if (state.graph?.removeListener) {
      state.graph.removeListener("plotly_relayout", remember);
      state.graph.removeListener("plotly_afterplot", restore);
    }
  };
  const scan = () => {
    state.scheduled = false;
    if (state.disposed) return;
    const graph = document.querySelector('.st-key-surface_main .js-plotly-plot');
    if (graph !== state.graph && graph?.on) {
      detach();
      state.graph = graph;
      graph.on("plotly_relayout", remember);
      graph.on("plotly_afterplot", restore);
      restore();
    }
  };
  const schedule = () => {
    if (!state.scheduled && !state.disposed) {
      state.scheduled = true;
      requestAnimationFrame(scan);
    }
  };
  const resize = () => {
    document.querySelectorAll('.st-key-surface_main .js-plotly-plot, .st-key-surface_section .js-plotly-plot')
      .forEach(graph => window.Plotly?.Plots.resize(graph));
  };
  const observer = new MutationObserver(schedule);
  state.dispose = () => {
    state.disposed = true;
    observer.disconnect();
    window.removeEventListener("resize", resize);
    detach();
  };
  window.__fillSurfacePresentation = state;
  window.addEventListener("resize", resize);
  observer.observe(document.body, {childList: true, subtree: true});
  scan();
})();
