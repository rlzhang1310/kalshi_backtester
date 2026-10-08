(() => {
  const get = id => document.getElementById(id);
  let visible = false, configured = false, busy = false, selectionVersion = 0;
  let resetPending = Promise.resolve();

  const node = (tag, value = "", className = "") => {
    const item = document.createElement(tag);
    item.textContent = String(value);
    if (className) item.className = className;
    return item;
  };
  const dollarsText = value => {
    if (value === null || value === undefined) return "—";
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "—";
    const digits = Math.abs(amount).toFixed(4).replace(/(\.\d{2})0+$/, "$1");
    return `${amount < 0 ? "−" : ""}$${digits}`;
  };
  const pairText = pair => pair ?
    `${pair.primary_ticker} ${pair.primary_outcome.toUpperCase()} · ${pair.relationship === "same_outcome" ? "same result" : "opposite result"} · ${pair.other_ticker} ${pair.other_outcome.toUpperCase()}` :
    "Lock a relationship on the Manual desk first.";
  const localPair = () => draft.mode === "paired" && draft.secondTarget === "other_market" &&
    secondSelection() && draft.locked ? draft.locked : null;

  async function paperApi(path, body) {
    const options = {credentials: "same-origin"};
    if (body !== undefined) {
      if (!status?.request_token) throw new Error("Local session is not ready. Reload the page.");
      options.method = "POST";
      options.headers = {"Content-Type": "application/json", "X-Request-Token": status.request_token};
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || `Paper request failed (${response.status})`);
    return result;
  }

  function list(container, records, render) {
    container.replaceChildren();
    if (!records.length) { container.appendChild(node("p", "None yet.", "paper-note")); return; }
    for (const record of records.slice(0, 12)) {
      const row = node("article");
      const [title, detail] = render(record);
      row.append(node("strong", title), node("span", detail));
      container.appendChild(row);
    }
  }

  function render(view) {
    configured = view.configured;
    get("paper-state").textContent = view.observing ? "Observing" : view.configured ? "Ready · stopped" : "Stopped";
    get("paper-start").disabled = busy || !view.configured || view.observing;
    get("paper-stop").disabled = busy || !view.observing;
    const locked = localPair();
    get("paper-pair").textContent = view.pair ? pairText(view.pair) : locked ?
      pairText({primary_ticker: locked.primaryTicker, primary_outcome: locked.primaryOutcome,
        other_ticker: locked.otherTicker, other_outcome: locked.otherOutcome, relationship: locked.relationship}) :
      pairText(null);
    const current = get("paper-current");
    current.replaceChildren();
    if (!view.current.length) current.appendChild(node("p", view.configured ? "Waiting for synchronized live books." : "No pair configured.", "paper-note"));
    for (const record of view.current) {
      const item = node("article", "", "paper-assessment");
      const legs = record.legs.map(leg => `${leg.ticker} ${leg.outcome.toUpperCase()}`).join(" + ");
      item.appendChild(node("strong", legs));
      item.appendChild(node("p", `Ask costs ${record.legs.map(leg => dollarsText(leg.ask_cost)).join(" + ")} · fee bound ${dollarsText(record.fee_bound)}`));
      item.appendChild(node("p", `Net edge ${dollarsText(record.net_edge)} · required ${dollarsText(record.threshold)}`, "paper-edge"));
      item.appendChild(node("p", record.blocked.length ? record.blocked.join(" · ") :
        record.net_edge !== null && Number(record.net_edge) > 0 ? "Observed opportunity" : "No positive net edge"));
      current.appendChild(item);
    }
    const timing = view.latency_ms || {};
    const fmt = key => timing[key] ? `${timing[key].p50}/${timing[key].p95}/${timing[key].p99} ms` : "—";
    get("paper-latency").textContent = `p50/p95/p99: receive→book ${fmt("receive_to_book")} · book→decision ${fmt("book_to_decision")} · decision→intent ${fmt("decision_to_intent")}`;
    list(get("paper-episodes"), view.episodes, episode => [
      `Orientation ${episode.orientation + 1} · peak ${dollarsText(episode.peak_edge)}`,
      `${episode.started_at} · ${episode.ended_at ? `ended · ${Math.round(episode.duration_ms)} ms` : "open"}`]);
    list(get("paper-intents"), view.would_submit, intent => [
      intent.legs.map(leg => `${leg.ticker} ${leg.outcome.toUpperCase()} ≤ ${dollarsText(leg.cap)}`).join(" + "),
      `Observed edge ${dollarsText(intent.observed_net_edge)} · cap bound ${dollarsText(intent.cap_net_edge_bound)} · IOC paper intent`]);
    list(get("paper-results"), view.simulated_results, result => [
      `${result.delay_ms} ms · ${result.paired_fill ? "two modeled fills" : result.unmatched_exposure ? "unmatched exposure" : "no paired fill"}`,
      result.legs.map(leg => `${leg.ticker} ${leg.outcome.toUpperCase()} ${leg.filled}`).join(" · ") +
      (result.modeled_net === null ? result.unmatched_exposure && result.modeled_outlay !== null ?
        ` · outlay ${dollarsText(result.modeled_outlay)}` : "" : ` · paired net ${dollarsText(result.modeled_net)}`)]);
    const pnlRows = get("paper-pnl-rows");
    pnlRows.replaceChildren();
    if (!view.simulated_pnl?.length) {
      const row = node("tr"); const cell = node("td", "Configure a pair to see scenario P&L.");
      cell.colSpan = 7; row.appendChild(cell); pnlRows.appendChild(row);
    }
    for (const scenario of view.simulated_pnl || []) {
      const row = node("tr");
      const exposureText = scenario.unpriced_exposure ?
        `${dollarsText(scenario.unmatched_outlay)} + ${scenario.unpriced_exposure} unpriced` :
        dollarsText(scenario.unmatched_outlay);
      for (const value of [`${scenario.delay_ms} ms`, scenario.attempts, scenario.paired,
        scenario.unmatched, scenario.missed, dollarsText(scenario.paired_net),
        exposureText]) row.appendChild(node("td", value));
      pnlRows.appendChild(row);
    }
    get("paper-evidence").textContent = `Evidence: ${view.evidence_file || "not started"} · queue overflow ${view.evidence_overflow}. Depth estimates do not prove exchange fills or realized profit.`;
  }

  async function refresh() {
    if (!visible || busy) return;
    try { render(await paperApi("/api/paper")); }
    catch (error) { get("paper-error").textContent = error.message; }
  }

  async function run(action, body) {
    busy = true;
    get("paper-error").textContent = "";
    try { render(await paperApi(`/api/paper/${action}`, body)); }
    catch (error) { get("paper-error").textContent = error.message; }
    finally { busy = false; await refresh(); }
  }

  get("tab-manual").addEventListener("click", () => {
    visible = false; get("manual-tab").hidden = false; get("paper-tab").hidden = true;
    get("tab-manual").setAttribute("aria-current", "page"); get("tab-paper").removeAttribute("aria-current");
  });
  get("tab-paper").addEventListener("click", () => {
    visible = true; get("manual-tab").hidden = true; get("paper-tab").hidden = false;
    get("tab-paper").setAttribute("aria-current", "page"); get("tab-manual").removeAttribute("aria-current");
    refresh();
  });
  const showPaperView = pnl => {
    get("paper-signals-panel").hidden = pnl;
    get("paper-pnl-panel").hidden = !pnl;
    if (pnl) {
      get("paper-view-signals").removeAttribute("aria-current");
      get("paper-view-pnl").setAttribute("aria-current", "page");
    } else {
      get("paper-view-pnl").removeAttribute("aria-current");
      get("paper-view-signals").setAttribute("aria-current", "page");
    }
  };
  get("paper-view-signals").addEventListener("click", () => showPaperView(false));
  get("paper-view-pnl").addEventListener("click", () => showPaperView(true));
  get("paper-configure").addEventListener("click", async () => {
    await resetPending;
    const version = selectionVersion;
    const locked = localPair();
    if (!locked) { get("paper-error").textContent = "Lock two distinct markets on the Manual desk first."; return; }
    const min = spreadUnits(get("paper-min-profit").value), margin = spreadUnits(get("paper-safety").value);
    if (min === null || margin === null) { get("paper-error").textContent = "Enter nonnegative thresholds in cents, up to two decimals."; return; }
    const delaysText = get("paper-delays").value.trim();
    const delays = delaysText.split(",").map(value => value.trim());
    if (!delays.length || delays.length > 8 || delays.some(value => !/^\d{1,4}$/.test(value)) ||
      !delays.includes("0") || new Set(delays).size !== delays.length || delays.some(value => Number(value) > 5000)) {
      get("paper-error").textContent = "Enter unique delays from 0 to 5000 ms, including 0."; return;
    }
    const asDollars = units => `${units / 10000n}.${String(units % 10000n).padStart(4, "0")}`;
    await run("configure", {primary_ticker: locked.primaryTicker, primary_outcome: locked.primaryOutcome,
      other_ticker: locked.otherTicker, other_outcome: locked.otherOutcome, relationship: locked.relationship,
      quantity: "1", min_profit: asDollars(min), safety_margin: asDollars(margin),
      direct_account: false, settlement_asserted: true,
      delays_ms: delays.map(Number)});
    if (version !== selectionVersion && configured) await run("reset", {});
  });
  get("paper-start").addEventListener("click", () => run("start", {}));
  get("paper-stop").addEventListener("click", () => run("stop", {}));
  window.addEventListener("paper-desk-selection-changed", () => {
    selectionVersion += 1;
    resetPending = resetPending.then(async () => {
      if (!configured) return;
      try { render(await paperApi("/api/paper/reset", {})); }
      catch (error) { get("paper-error").textContent = error.message; }
    });
  });
  setInterval(refresh, 500);
})();
