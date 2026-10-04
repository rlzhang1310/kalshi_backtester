(function () {
  const graph = document.getElementById('{plot_id}');
  if (!graph || !graph.on || !window.Plotly) return;
  let index = graph.data.findIndex(trace => trace.name === 'Volume' && trace.type === 'bar');
  if (index < 0) return;
  let source = graph.data[index].meta;
  if (!source || !source.timestamps || !source.counts) return;
  let times = source.timestamps.map(value => Date.parse(value));
  let counts = source.counts;
  let minimum = source.minimum_ms;
  let currentBucket = source.bucket_ms;
  let pending = false;

  function utcTime(value) {
    if (typeof value === 'number') return value;
    const timestamp = String(value).replace(' ', 'T');
    return Date.parse(/(?:[Zz]|[+-]\d\d:?\d\d)$/.test(timestamp) ? timestamp : timestamp + 'Z');
  }

  function bucketSize(span) {
    const desired = Math.max(minimum, span / 120);
    const magnitude = Math.pow(10, Math.floor(Math.log10(desired)));
    for (const multiplier of [1, 2, 5, 10]) {
      const candidate = multiplier * magnitude;
      if (candidate >= desired) return candidate;
    }
    return desired;
  }

  function rebucket() {
    pending = false;
    const nextIndex = graph.data.findIndex(trace => trace.name === 'Volume' && trace.type === 'bar');
    if (nextIndex < 0) return;
    if (nextIndex !== index) {
      index = nextIndex;
      source = null;
    }
    if (graph.data[index].meta !== source) {
      source = graph.data[index].meta;
      if (!source || !source.timestamps || !source.counts) return;
      times = source.timestamps.map(value => Date.parse(value));
      counts = source.counts;
      minimum = source.minimum_ms;
      currentBucket = source.bucket_ms;
    }
    const range = graph._fullLayout && graph._fullLayout.xaxis.range;
    if (!range) return;
    const left = utcTime(range[0]);
    const right = utcTime(range[1]);
    if (!Number.isFinite(left) || !Number.isFinite(right) || right <= left) return;
    const size = bucketSize(right - left);
    if (size === currentBucket) return;
    currentBucket = size;
    const buckets = new Map();
    for (let i = 0; i < times.length; i++) {
      if (!Number.isFinite(times[i])) continue;
      const start = Math.floor(times[i] / size) * size;
      buckets.set(start, (buckets.get(start) || 0) + counts[i]);
    }
    const starts = Array.from(buckets.keys()).sort((a, b) => a - b);
    Plotly.restyle(graph, {
      x: [starts.map(start => new Date(start + size / 2).toISOString())],
      y: [starts.map(start => buckets.get(start))],
      width: [size]
    }, [index]);
  }

  function scheduleRebucket() {
    if (!pending) {
      pending = true;
      requestAnimationFrame(rebucket);
    }
  }
  graph.on('plotly_relayout', scheduleRebucket);
  graph.on('plotly_afterplot', scheduleRebucket);
  rebucket();
})();
