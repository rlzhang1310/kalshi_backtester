const $ = id => document.getElementById(id);
const cards = ["a", "b"].map(id => ({id, generation: 0, ticker: null, outcome: "yes", data: null, error: null}));
let status = null, selected = null, orders = [], submitting = false, pending = null, ticketError = "", ticketNotice = "";
let backendReachable = false, streamHealthy = false, lastStateAt = 0, eventSource = null;
let streamGeneration = 0, streamEpoch = null, streamRevision = 0, orderFingerprint = "";
let sessionEpoch = null, refreshingSession = false;
const draft = {mode: "single", secondTarget: "other_market", relationship: null, locked: null,
  pricing: "live", spreadTouched: false};
const metrics = {display: [], dispatch: [], exchange: []};
const sample = (name, value) => {
  if (!Number.isFinite(value) || value < 0) return;
  metrics[name].push(value);
  if (metrics[name].length > 100) metrics[name].shift();
  const fmt = values => {
    if (!values.length) return "—";
    const sorted = [...values].sort((a, b) => a - b);
    return `${sorted[Math.floor((sorted.length - 1) / 2)].toFixed(1)}/${sorted[Math.floor((sorted.length - 1) * .95)].toFixed(1)}`;
  };
  $("latency").textContent = `Latency median/p95 ms · server-to-display ${fmt(metrics.display)} · local dispatch ${fmt(metrics.dispatch)} · exchange ack ${fmt(metrics.exchange)}`;
};
try { pending = JSON.parse(localStorage.getItem("pendingOneContract") || "null"); } catch { pending = null; }

const el = (tag, text = "", className = "") => {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
};
const append = (parent, ...children) => children.forEach(child => parent.appendChild(child));
const setPending = value => {
  pending = value;
  if (value) localStorage.setItem("pendingOneContract", JSON.stringify(value));
  else localStorage.removeItem("pendingOneContract");
  renderTicket();
};
const centsUnits = value => {
  const match = /^\s*(\d{1,2})(?:\.(\d{1,2}))?\s*$/.exec(value);
  if (!match) return null;
  const units = BigInt(match[1]) * 100n + BigInt((match[2] || "").padEnd(2, "0"));
  return units > 0n && units < 10000n ? units : null;
};
const dollarUnits = value => {
  if (typeof value !== "string") return null;
  const match = /^(\d+)(?:\.(\d{1,4}))?$/.exec(value);
  return match ? BigInt(match[1]) * 10000n + BigInt((match[2] || "").padEnd(4, "0")) : null;
};
const dollars = units => `0.${String(units).padStart(4, "0")}`.replace(/0+$/, "");
const centsText = units => units === null ? "" : `${units / 100n}${units % 100n ? `.${String(units % 100n).padStart(2, "0").replace(/0+$/, "")}` : ""}`;
const spreadUnits = value => {
  const match = /^\s*(\d{1,3})(?:\.(\d{1,2}))?\s*$/.exec(value);
  if (!match) return null;
  const units = BigInt(match[1]) * 100n + BigInt((match[2] || "").padEnd(2, "0"));
  return units <= 10000n ? units : null;
};
const cents = value => {
  const units = dollarUnits(value);
  return units === null ? "—" : `${String(units / 100n)}${units % 100n ? `.${String(units % 100n).padStart(2, "0").replace(/0+$/, "")}` : ""}¢`;
};
const onGrid = (units, ranges) => ranges.some(range => {
  const start = dollarUnits(range.start), end = dollarUnits(range.end), step = dollarUnits(range.step);
  return start !== null && end !== null && step !== null && step > 0n && units >= start && units <= end && (units - start) % step === 0n;
});
const fresh = card => streamHealthy && card?.data?.book_state === "live" && !card.data.stale;
const active = order => !["filled", "canceled", "expired", "rejected"].includes(order.status);
const notifyPaperSelectionChange = () => {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("paper-desk-selection-changed"));
};
const selectedCard = () => selected === null ? null : cards[selected];
const otherCard = () => selected === null ? null : cards[1 - selected];
const opposite = outcome => outcome === "yes" ? "no" : "yes";
const gridFor = (card, units) => !!card?.data?.price_ranges && units > 0n && units < 10000n &&
  onGrid(units, card.data.price_ranges) && onGrid(10000n - units, card.data.price_ranges);
function clearDraft() {
  draft.locked = null; draft.pricing = "live"; draft.spreadTouched = false;
  $("spread").value = ""; $("price").value = "";
  ticketError = ""; ticketNotice = "";
  notifyPaperSelectionChange();
}
function otherMarketCandidate() {
  const primary = selectedCard(), other = otherCard();
  if (!primary?.data || !other?.data || primary.ticker === other.ticker) return null;
  return {card: other, selectedOutcome: other.outcome,
    outcome: draft.relationship === "same_outcome" ? opposite(other.outcome) :
      draft.relationship === "opposite_outcomes" ? other.outcome : null};
}
function secondSelection() {
  const primary = selectedCard();
  if (!primary || draft.mode !== "paired") return null;
  if (draft.secondTarget === "same_market") return {card: primary, outcome: opposite(primary.outcome)};
  const candidate = otherMarketCandidate();
  if (!candidate?.outcome || !draft.locked || draft.locked.primaryTicker !== primary.ticker ||
      draft.locked.primaryOutcome !== primary.outcome || draft.locked.otherTicker !== candidate.card.ticker ||
      draft.locked.otherOutcome !== candidate.selectedOutcome || draft.locked.relationship !== draft.relationship) return null;
  return {card: candidate.card, outcome: candidate.outcome};
}
function frozenLegs() {
  if (!pending) return null;
  if (pending.kind !== "pair") return [{ticker: pending.ticker, outcome: pending.outcome, price: dollarUnits(pending.price)}];
  const secondOutcome = pending.second_target === "same_market" ? opposite(pending.primary_outcome) :
    pending.relationship === "same_outcome" ? opposite(pending.other_outcome) : pending.other_outcome;
  return [{ticker: pending.primary_ticker, outcome: pending.primary_outcome, price: dollarUnits(pending.primary_price)},
    {ticker: pending.second_target === "same_market" ? pending.primary_ticker : pending.other_ticker,
     outcome: secondOutcome, price: dollarUnits(pending.second_price)}];
}
function primaryUnits() {
  const card = selectedCard();
  if (draft.pricing === "fixed") return centsUnits($("price").value);
  const bid = card?.data?.quotes?.[card.outcome]?.bid_price;
  return bid ? dollarUnits(bid) : null;
}
function secondUnits(primary) {
  const spread = spreadUnits($("spread").value);
  return primary === null || spread === null ? null : 10000n - primary - spread;
}
function defaultSpread() {
  if (draft.mode !== "paired" || draft.spreadTouched || $("spread").value) return;
  const first = selectedCard(), second = secondSelection();
  const a = first?.data?.quotes?.[first.outcome]?.bid_price;
  const b = second?.card?.data?.quotes?.[second.outcome]?.bid_price;
  const aa = a ? dollarUnits(a) : null, bb = b ? dollarUnits(b) : null;
  if (aa === null || bb === null) return;
  const spread = 10000n - aa - bb;
  if (spread >= 0n && spread <= 10000n && gridFor(first, aa) && gridFor(second.card, bb)) $("spread").value = centsText(spread);
}

async function api(path, options = {}) {
  const {onResponse, ...fetchOptions} = options;
  const response = await fetch(path, {credentials: "same-origin", ...fetchOptions});
  if (onResponse) onResponse(response);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function selectOutcome(card, outcome) {
  if (selected === null) selected = cards.indexOf(card);
  const changed = card.outcome !== outcome;
  card.outcome = outcome;
  if (changed) clearDraft();
  cards.forEach(renderCard);
  renderTicket();
}

function makePrimary(card) {
  if (!card?.data || selectedCard() === card) return;
  selected = cards.indexOf(card);
  clearDraft();
  cards.forEach(renderCard);
  renderTicket();
}

function buildCardView(card, body) {
  const title = el("div", "", "market-title"), tickerLine = el("div", "", "ticker");
  const subtitle = el("p", "", "market-subtitle");
  const sideOptions = el("div", "", "side-options"), sides = {};
  for (const outcome of ["yes", "no"]) {
    const button = el("button", "", "side-option");
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    const name = el("div", "", "side-name");
    const action = el("span", "Select");
    append(name, el("span", outcome.toUpperCase()), action);
    const pair = el("div", "", "quote-pair");
    const bidBox = el("div"), askBox = el("div");
    const bid = el("strong", "—"), ask = el("strong", "—");
    const bidSize = el("small", "Size —"), askSize = el("small", "Size —");
    append(bidBox, el("span", "BID"), bid, bidSize);
    append(askBox, el("span", "ASK"), ask, askSize);
    append(pair, bidBox, askBox);
    append(button, name, pair);
    button.addEventListener("click", () => selectOutcome(card, outcome));
    sideOptions.appendChild(button);
    sides[outcome] = {button, bid, ask, bidSize, askSize, action};
  }
  const book = el("details", "", "book-details"), bookLabel = el("summary");
  book.open = false;
  const bookGrid = el("div", "", "book-grid"), depth = {};
  for (const side of ["bids", "asks"]) {
    const column = el("div", "", "book-column");
    const levels = el("div");
    append(column, el("h4", side === "bids" ? "Bids · high to low" : "Asks · low to high"), levels);
    bookGrid.appendChild(column);
    depth[side] = levels;
  }
  const ownOrder = el("p", "", "own-order"), bookNote = el("p", "Visible size is not a guaranteed fill or queue position.", "book-footnote");
  append(book, bookLabel, bookGrid, ownOrder, bookNote);
  book.addEventListener("toggle", () => { if (book.open) renderCard(card); });
  const rules = el("details", "", "rules-details"), primary = el("p"), secondary = el("p");
  append(rules, el("summary", "Market rules"), primary, secondary);
  const error = el("p", "", "market-error");
  body.replaceChildren(title, tickerLine, subtitle, sideOptions, book, rules, error);
  card.view = {title, tickerLine, subtitle, sides, book, bookLabel, depth, ownOrder, primary, secondary, error, depthFingerprint: null};
}

function renderCard(card) {
  const body = $(`market-${card.id}`), badge = $(`state-${card.id}`);
  $(`card-${card.id}`).classList.toggle("selected", selectedCard() === card);
  $(`primary-${card.id}`).textContent = selectedCard() === card ? "Primary leg" : "Make primary";
  $(`primary-${card.id}`).disabled = !card.data || selectedCard() === card;
  if (!card.data) {
    card.view = null;
    const message = card.error || (card.ticker ? "Loading market…" : "Enter a ticker to see YES and NO prices.");
    if (body.textContent !== message) body.replaceChildren(el("p", message, "empty-market"));
    badge.textContent = card.error ? "Unavailable" : card.ticker ? "Loading" : "Waiting for ticker";
    badge.dataset.tone = card.error ? "warn" : "neutral";
    return;
  }
  const data = card.data;
  if (!card.view) buildCardView(card, body);
  const view = card.view;
  const live = fresh(card) && data.tradable;
  badge.textContent = !streamHealthy ? "Feed connecting" : data.status === "unavailable" ? "Unavailable" :
    !["active", "open"].includes(data.status) ? "Closed or paused" : live ? "Live book" :
    data.book_state === "live" ? "Checking market" : data.book_state === "connecting" ? "Connecting book" : "Recovering book";
  badge.dataset.tone = live ? "good" : "warn";
  view.title.textContent = data.title || data.ticker;
  view.tickerLine.textContent = data.ticker;
  const subtitles = [data.yes_subtitle && `YES: ${data.yes_subtitle}`, data.no_subtitle && `NO: ${data.no_subtitle}`].filter(Boolean);
  view.subtitle.textContent = subtitles.join(" · ");
  view.subtitle.hidden = !subtitles.length;
  for (const outcome of ["yes", "no"]) {
    const quote = data.quotes?.[outcome] || {};
    const side = view.sides[outcome];
    side.button.setAttribute("aria-pressed", String(card.outcome === outcome));
    side.action.textContent = selected === null ? "SELECT" : card.outcome !== outcome ? "CHOOSE" :
      selectedCard() === card ? "PRIMARY" : "OTHER";
    side.bid.textContent = quote.bid_price ? cents(quote.bid_price) : "—";
    side.ask.textContent = quote.ask_price ? cents(quote.ask_price) : "—";
    side.bidSize.textContent = `Size ${quote.bid_price ? quote.bid_quantity : "—"}`;
    side.askSize.textContent = `Size ${quote.ask_price ? quote.ask_quantity : "—"}`;
  }
  const quote = data.quotes?.[card.outcome] || {};
  view.bookLabel.textContent = `${card.outcome.toUpperCase()} depth · spread ${quote.spread ? cents(quote.spread) : "—"}`;
  const book = data.depth?.[card.outcome] || {bids: [], asks: []};
  const own = orders.find(order => order.ticker === data.ticker && order.outcome === card.outcome &&
    order.status === "resting" && Number(order.remaining_quantity) > 0);
  const fingerprint = JSON.stringify({book, own: own && [own.limit_price, own.remaining_quantity], selected: selectedCard() === card});
  if (view.book.open && fingerprint !== view.depthFingerprint) {
    for (const side of ["bids", "asks"]) {
      const container = view.depth[side];
      const levels = book[side] || [];
      if (!levels.length) {
        if (container.firstElementChild?.tagName !== "P") container.replaceChildren(el("p", "No visible levels", "empty-depth"));
        continue;
      }
      if (container.firstElementChild?.tagName === "P") container.replaceChildren();
      while (container.children.length > levels.length) container.lastElementChild.remove();
      for (const [index, level] of levels.entries()) {
        let row = container.children[index];
        if (!row) {
          row = el("button", "", "depth-row");
          row.type = "button";
          row.title = "Copy price to ticket; does not submit an order.";
          append(row, el("span"), el("small"));
          row.addEventListener("click", () => {
            draft.pricing = "fixed"; $("price").value = cents(row.dataset.price).slice(0, -1);
            ticketError = ""; ticketNotice = ""; renderTicket();
          });
          container.appendChild(row);
        }
        row.dataset.price = level.price;
        row.disabled = selectedCard() !== card;
        row.firstElementChild.textContent = cents(level.price);
        row.lastElementChild.textContent = `${level.quantity}${own?.limit_price === level.price ? " · yours" : ""}`;
      }
    }
    view.depthFingerprint = fingerprint;
  }
  view.ownOrder.textContent = own && ![...(book.bids || []), ...(book.asks || [])].some(level => level.price === own.limit_price) ?
    `Your order: ${cents(own.limit_price)} · ${own.remaining_quantity} remaining` : "";
  view.ownOrder.hidden = !view.ownOrder.textContent;
  view.primary.textContent = data.rules?.primary || "No primary rules supplied.";
  view.secondary.textContent = data.rules?.secondary || "";
  view.secondary.hidden = !view.secondary.textContent;
  view.error.textContent = card.error || "";
  view.error.hidden = !card.error;
}

async function load(card) {
  ticketError = ""; ticketNotice = "";
  const value = $(`ticker-${card.id}`).value.trim().toUpperCase();
  const changed = value !== card.ticker;
  if (changed) clearDraft();
  card.generation += 1;
  const generation = card.generation;
  card.ticker = value; card.error = null;
  if (changed) { card.data = null; card.view = null; if (selectedCard() === card) selected = null; }
  if (!/^[A-Z0-9][A-Z0-9_-]{1,199}$/.test(value)) {
    card.ticker = null;
    card.error = value ? "Enter a valid exact ticker." : "Enter an exact ticker.";
    openStream(); renderCard(card); renderTicket();
    return;
  }
  if (changed) openStream();
  renderCard(card); renderTicket();
  try {
    const data = await api(`/api/markets/${encodeURIComponent(value)}/summary`);
    if (generation !== card.generation) return;
    card.data = data; card.error = null;
  } catch (error) {
    if (generation !== card.generation) return;
    card.error = error.status === 404 ?
      `Exact ticker not found in ${status?.environment || "this"} markets. Check the full suffix and exchange environment.` : error.message;
  }
  renderCard(card); renderTicket();
}

const labels = {submitting: "Submitting", resting: "Resting", cancel_pending: "Cancel requested", filled: "Filled", canceled: "Canceled", expired: "Expired", rejected: "Rejected", unknown: "Checking exchange"};
function renderOrders() {
  const list = $("orders-list"); list.replaceChildren();
  if (!orders.length) { list.appendChild(el("p", "No orders placed from this app yet.")); return; }
  const displayOrders = [], grouped = new Set();
  for (const row of orders) {
    if (!row.pair_id) displayOrders.push(row);
    else if (!grouped.has(row.pair_id)) {
      grouped.add(row.pair_id);
      displayOrders.push(...orders.filter(item => item.pair_id === row.pair_id).sort((a, b) => a.leg_index - b.leg_index));
    }
  }
  const shownPairs = new Set();
  for (const order of displayOrders) {
    if (order.pair_id && !shownPairs.has(order.pair_id)) {
      shownPairs.add(order.pair_id);
      const group = el("div", "", "pair-group");
      let intent = null;
      try { intent = JSON.parse(order.pair_intent || "null"); } catch { /* Older records can omit intent. */ }
      const relation = intent?.second_target === "same_market" ? "Opposite side of same market" :
        intent?.relationship === "same_outcome" ? "Same outcome" :
        intent?.relationship === "opposite_outcomes" ? "Opposite outcomes" : "Paired";
      group.appendChild(el("strong", `Pair ${order.pair_id.slice(0, 8)} · ${relation} · independent legs`));
      const pairRows = orders.filter(row => row.pair_id === order.pair_id);
      if (pairRows.some(row => row.status === "resting" && Number(row.remaining_quantity) > 0)) {
        const cancelPair = el("button", "Cancel remaining pair orders", "pair-cancel");
        cancelPair.type = "button";
        cancelPair.addEventListener("click", async () => {
          cancelPair.disabled = true;
          try {
            const updated = await api(`/api/pairs/${order.pair_id}/cancel`, {method: "POST",
              headers: {"X-Request-Token": status.request_token}});
            mergeOrders(updated); renderTicket();
          } catch (error) { ticketError = error.message; cancelPair.disabled = false; renderTicket(); }
        });
        group.appendChild(cancelPair);
      }
      list.appendChild(group);
    }
    const row = el("div", "", "order-row"), main = el("div", "", "order-main"), info = el("div");
    const state = el("span", labels[order.status] || order.status, "order-status");
    if (order.pair_id) info.appendChild(el("small", `Leg ${order.leg_index + 1} of pair ${order.pair_id.slice(0, 8)}`));
    state.dataset.tone = ["resting", "filled"].includes(order.status) ? "good" : order.status === "rejected" ? "bad" :
      ["submitting", "unknown", "cancel_pending"].includes(order.status) ? "warn" : "neutral";
    append(info, el("strong", `${order.ticker} · ${order.outcome.toUpperCase()} · ${cents(order.limit_price)}`),
      el("small", `${order.filled_quantity} filled · ${order.remaining_quantity} remaining · ${new Date(order.created_at).toLocaleString()}`));
    const details = el("details", "", "order-detail"); details.appendChild(el("summary", "Order details"));
    for (const [name, value] of [
      ["Expiry", new Date(order.expires_at).toLocaleString()], ["Requested", order.requested_quantity],
      ["Canceled", order.canceled_quantity ?? "Pending"], ["Average fill price", order.average_fill_price ? cents(order.average_fill_price) : "Pending"],
      ["Actual fees", order.actual_fees === null ? "Pending" : `$${order.actual_fees}`],
      ["Request ID", order.request_id], ["Exchange order ID", order.exchange_order_id || "Pending"],
      ["Message", order.message || order.error_code || "—"]
    ]) details.appendChild(el("p", `${name}: ${value}`));
    info.appendChild(details);
    append(main, state, info);
    row.appendChild(main);
    if (order.status === "resting" && Number(order.remaining_quantity) > 0) {
      const cancel = el("button", "Cancel", "secondary"); cancel.type = "button";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try {
          const updated = await api(`/api/orders/${order.request_id}/cancel`, {
            method: "POST", headers: {"X-Request-Token": status.request_token}
          });
          orders = orders.map(item => item.request_id === updated.request_id ? updated : item);
          renderOrders(); renderTicket();
        } catch (error) { ticketError = error.message; cancel.disabled = false; renderTicket(); }
      });
      const actions = el("div", "", "order-actions"); actions.appendChild(cancel); row.appendChild(actions);
    }
    list.appendChild(row);
  }
}

const mergeOrders = rows => {
  const updates = Array.isArray(rows) ? rows : [rows];
  orders = [...updates, ...orders.filter(item => !updates.some(row => row.request_id === item.request_id))];
  orderFingerprint = JSON.stringify(orders);
  renderOrders(); cards.forEach(renderCard);
};
const receiptRows = rows => (Array.isArray(rows) ? rows : [rows]).map(receipt).join(" | ");
async function submit(payload, clickedAt = performance.now()) {
  if (submitting) return;
  ticketError = "";
  submitting = true; setPending(payload);
  const {kind, ...body} = payload;
  const paired = kind === "pair";
  const path = paired ? "/api/pairs" : "/api/orders";
  const lookup = paired ? `/api/pairs/${body.pair_id}` : `/api/orders/by-request/${body.request_id}`;
  try {
    sample("dispatch", performance.now() - clickedAt);
    const order = await api(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Request-Token": status.request_token},
      body: JSON.stringify(body), onResponse: response => {
        const match = /exchange;dur=([\d.]+)/.exec(response.headers.get("Server-Timing") || "");
        if (match) sample("exchange", Number(match[1]));
      }});
    setPending(null);
    mergeOrders(order);
    ticketNotice = receiptRows(order);
  } catch (error) {
    if (error.status >= 400 && error.status < 500 && error.status !== 409) {
      setPending(null);
      ticketError = error.message;
      return;
    }
    try {
      const order = await api(lookup);
      setPending(null);
      mergeOrders(order);
      ticketNotice = receiptRows(order);
    } catch (lookupError) {
      if (error.status === 409 && lookupError.status === 404) setPending(null);
      ticketError = error.status === 409 ? error.message : `${error.message}. Retry only this same request ID.`;
    }
  } finally { submitting = false; renderTicket(); }
}

function receipt(order) {
  if (order.status === "resting") return `Order resting: ${order.remaining_quantity} ${order.outcome.toUpperCase()} at ${cents(order.limit_price)}.`;
  if (order.status === "filled") return `Filled: ${order.filled_quantity} ${order.outcome.toUpperCase()} in ${order.ticker}.`;
  if (order.status === "rejected") return `Rejected: ${order.message || order.error_code || "exchange declined the order"}.`;
  return `${labels[order.status] || order.status}: ${order.ticker} ${order.outcome.toUpperCase()}.`;
}

function updateHeader() {
  if (!status) return;
  const isLive = status.environment === "production";
  $("environment").textContent = isLive ? "LIVE · REAL MONEY" : "DEMO";
  $("environment").dataset.tone = isLive ? "live" : "neutral";
  $("environment-note").textContent = isLive ? "Production markets · real orders" : "Demo markets · test funds";
  const trading = $("trading"), connection = $("connection");
  if (!status.trading_enabled) { trading.textContent = "Posting off"; trading.dataset.tone = "neutral"; }
  else if (status.trade_scope_status === "read_only") { trading.textContent = "Read-only API key"; trading.dataset.tone = "warn"; }
  else if (status.trade_scope_status === "wrong_subaccount") { trading.textContent = "Wrong subaccount"; trading.dataset.tone = "warn"; }
  else if (!status.authentication_ready) { trading.textContent = "Authentication unavailable"; trading.dataset.tone = "bad"; }
  else if (status.trade_scope_status === "unknown") { trading.textContent = "Permission unverified"; trading.dataset.tone = "warn"; }
  else { trading.textContent = "Posting enabled"; trading.dataset.tone = "good"; }
  if (!backendReachable) { connection.textContent = "Backend unreachable"; connection.dataset.tone = "bad"; }
  else if (status.feed_error) { connection.textContent = "Exchange feed error"; connection.dataset.tone = "bad"; }
  else if (!streamHealthy || !status.connected) { connection.textContent = "Feed connecting"; connection.dataset.tone = "warn"; }
  else if (status.recovering || !status.order_stream_ready) { connection.textContent = "Feed recovering"; connection.dataset.tone = "warn"; }
  else { connection.textContent = "Exchange connected"; connection.dataset.tone = "good"; }
  const banner = $("health-banner");
  let notice = status.feed_error || "";
  if (!notice && status.trading_enabled) {
    if (status.trade_scope_status === "read_only") notice = "This API key can read markets but cannot place orders. Configure a key with write or write::trade scope and restart the server.";
    else if (status.trade_scope_status === "wrong_subaccount") notice = "This API key is restricted to another subaccount; this tool submits from subaccount 0.";
    else if (!status.authentication_ready) notice = "Private exchange authentication is unavailable. Check that the key matches the selected environment.";
    else if (!status.order_stream_ready) notice = "Private order stream is not ready. Quotes may still update, but posting is paused.";
    else if (status.recovering) notice = "Previous orders are being recovered. Posting resumes when their state is known.";
  }
  banner.textContent = notice;
  banner.hidden = !notice;
}

function openStream() {
  if (!status) return;
  if (eventSource) eventSource.close();
  streamGeneration += 1;
  const generation = streamGeneration;
  streamHealthy = false; streamEpoch = null; streamRevision = 0; lastStateAt = 0;
  updateHeader(); cards.forEach(renderCard); renderTicket();
  const tickers = [...new Set(cards.filter(card => card.ticker).map(card => card.ticker))];
  eventSource = new EventSource(`/api/events?tickers=${encodeURIComponent(tickers.join(","))}`);
  eventSource.addEventListener("state", event => {
    if (generation !== streamGeneration) return;
    let envelope;
    try { envelope = JSON.parse(event.data); } catch { return; }
    if (envelope.schema_version !== 2 || !Number.isInteger(envelope.revision)) return;
    if (streamEpoch === envelope.stream_epoch && envelope.revision <= streamRevision) return;
    streamEpoch = envelope.stream_epoch; streamRevision = envelope.revision;
    const wasHealthy = streamHealthy;
    lastStateAt = Date.now(); streamHealthy = true; backendReachable = true;
    if (sessionEpoch === null) sessionEpoch = envelope.stream_epoch;
    const sessionChanged = sessionEpoch !== envelope.stream_epoch;
    status = {...envelope.status, request_token: sessionChanged ? null : status.request_token};
    if (sessionChanged && !refreshingSession) {
      refreshingSession = true;
      api("/api/status").then(refreshed => {
        if (generation === streamGeneration && streamEpoch === envelope.stream_epoch) {
          sessionEpoch = envelope.stream_epoch;
          status = {...status, ...refreshed};
        }
      }).catch(() => { backendReachable = false; }).finally(() => {
        refreshingSession = false; updateHeader(); renderTicket();
      });
    }
    const changedCards = [];
    for (const card of cards) {
      if (!card.ticker) continue;
      const updated = envelope.markets.find(market => market.ticker === card.ticker);
      if (!updated || (!card.data && updated.status === "unavailable")) continue;
      if (!card.data || JSON.stringify(updated) !== JSON.stringify(card.data)) {
        card.data = {...card.data, ...updated};
        changedCards.push(card);
      }
    }
    const nextOrderRows = envelope.orders.map(remote => {
      const local = orders.find(order => order.request_id === remote.request_id);
      return local && local.updated_at > remote.updated_at ? local : remote;
    });
    for (const local of orders) {
      if (!nextOrderRows.some(order => order.request_id === local.request_id) &&
          local.updated_at > envelope.server_time) nextOrderRows.push(local);
    }
    const nextOrders = JSON.stringify(nextOrderRows);
    let ordersChanged = false;
    if (nextOrders !== orderFingerprint) {
      orders = nextOrderRows;
      orderFingerprint = nextOrders;
      ordersChanged = true;
      renderOrders();
    }
    if (pending && (pending.kind === "pair" ? orders.filter(order => order.pair_id === pending.pair_id).length === 2 :
      orders.some(order => order.request_id === pending.request_id))) setPending(null);
    updateHeader();
    if (!wasHealthy || ordersChanged) cards.forEach(renderCard);
    else changedCards.forEach(renderCard);
    renderTicket();
    sample("display", Date.now() - new Date(envelope.server_time).getTime());
  });
  eventSource.onerror = () => {
    if (generation !== streamGeneration) return;
    streamHealthy = false; streamEpoch = null; streamRevision = 0;
    updateHeader(); cards.forEach(renderCard); renderTicket();
  };
}

async function bootstrap() {
  try {
    const [newStatus, newOrders] = await Promise.all([api("/api/status"), api("/api/orders")]);
    status = newStatus; orders = newOrders;
    orderFingerprint = JSON.stringify(orders); backendReachable = true;
    if (pending) {
      const found = pending.kind === "pair" ? orders.filter(order => order.pair_id === pending.pair_id) :
        orders.find(order => order.request_id === pending.request_id);
      if (found && (!Array.isArray(found) || found.length === 2)) { ticketNotice = receiptRows(found); setPending(null); }
      else {
        try { const order = await api(pending.kind === "pair" ? `/api/pairs/${pending.pair_id}` : `/api/orders/by-request/${pending.request_id}`);
          mergeOrders(order); ticketNotice = receiptRows(order); setPending(null); }
        catch { /* The same request remains available for an explicit retry. */ }
      }
    }
    renderOrders(); openStream();
  } catch (error) {
    backendReachable = false; $("connection").textContent = "Backend unreachable";
    setTimeout(bootstrap, 5000);
  }
  cards.forEach(renderCard); renderTicket();
}

function renderManualTicket() {
  const card = selectedCard(), data = card?.data, paired = draft.mode === "paired";
  for (const [id, value] of [["mode-single", !paired], ["mode-paired", paired],
    ["target-other", draft.secondTarget === "other_market"], ["target-same", draft.secondTarget === "same_market"],
    ["relation-same", draft.relationship === "same_outcome"], ["relation-opposite", draft.relationship === "opposite_outcomes"],
    ["price-live", draft.pricing === "live"], ["price-fixed", draft.pricing === "fixed"]])
    $(id).setAttribute("aria-pressed", String(value));
  $("pair-controls").hidden = !paired; $("pair-pricing").hidden = !paired;
  $("relationship-controls").hidden = draft.secondTarget !== "other_market";
  $("same-market-state").hidden = draft.secondTarget !== "same_market";
  $("price").readOnly = draft.pricing === "live";
  $("target-ticker").textContent = data ? data.ticker : "No market selected";
  $("outcome").textContent = data ? card.outcome.toUpperCase() : "—";
  const quote = data?.quotes?.[card.outcome] || {};
  $("use-bid").hidden = draft.pricing === "live";
  $("use-bid").disabled = !quote.bid_price;
  $("price-help").textContent = draft.pricing === "live" ? "Updates with the primary best bid." :
    "Stays at your chosen limit until you change it.";
  const lifetime = status?.order_expiry_seconds || 300;
  $("lifetime").textContent = lifetime % 60 === 0 ? `${lifetime / 60} minutes` : `${lifetime} seconds`;
  const units = primaryUnits();
  if (draft.pricing === "live") $("price").value = units === null ? "" : centsText(units);
  const other = otherCard();
  const candidate = otherMarketCandidate();
  const lockedOther = !!(paired && draft.secondTarget === "other_market" && secondSelection());
  $("swap-primary").hidden = !data || !other?.data;
  $("other-market-name").textContent = other?.ticker || "Load the other market";
  const mappedOutcome = candidate?.outcome;
  $("other-market-selection").textContent = !other?.data ? other?.ticker ? "Waiting for its book." : "Enter a ticker on the other card." :
    other.ticker === card?.ticker ? "Choose Same market above." :
    mappedOutcome ? `${other.outcome.toUpperCase()} selected → buy ${mappedOutcome.toUpperCase()}${lockedOther ? " · locked" : " · lock to post"}` :
    `${other.outcome.toUpperCase()} selected · choose the settlement relationship.`;
  if (paired && draft.secondTarget === "other_market") {
    $("lock-relationship").hidden = lockedOther;
    $("lock-relationship").disabled = !data || !other?.data || card.ticker === other.ticker || !draft.relationship || !fresh(card) || !fresh(other);
    $("lock-relationship").textContent = candidate && draft.relationship ? `Lock ${card.ticker} + ${other.ticker}` : "Lock relationship";
    $("relationship-state").textContent = lockedOther ?
      `Locked · second buy ${other.ticker} ${mappedOutcome.toUpperCase()}` :
      data && other?.data && card.ticker === other.ticker ? "For one ticker, choose Opposite side of same market." :
      candidate && draft.relationship ? `Second buy: ${other.ticker} ${mappedOutcome.toUpperCase()}. Lock this mapping.` :
      candidate ? `${other.ticker} is loaded. Choose the payoff relationship, then lock.` :
      "Load a distinct other market, choose its outcome, then lock the relationship.";
  }
  defaultSpread();
  const second = secondSelection(), secondPrice = paired ? secondUnits(units) : null;
  $("use-second-bid").hidden = draft.secondTarget !== "other_market" || !second;
  $("use-second-bid").disabled = !second || second.card !== other ||
    !second.card.data?.quotes?.[second.outcome]?.bid_price || units === null;
  const pulse = $("price-pulse"); pulse.replaceChildren();
  const addPulse = (label, market, outcome) => {
    const quote = market?.data?.quotes?.[outcome] || {};
    const item = el("div", "", "pulse-item");
    const values = el("div", "", "pulse-values");
    append(values,
      el("span", "BID", "pulse-side"), el("strong", quote.bid_price ? cents(quote.bid_price) : "—", "pulse-number"),
      el("span", "ASK", "pulse-side"), el("strong", quote.ask_price ? cents(quote.ask_price) : "—", "pulse-number"));
    append(item, el("span", label, "pulse-label"),
      el("strong", market?.ticker && outcome ? `${market.ticker} · ${outcome.toUpperCase()}` : "Select a market", "pulse-market"),
      values);
    pulse.appendChild(item);
  };
  addPulse(`PRIMARY · ${fresh(card) ? "LIVE" : "WAITING"}`, card, card?.outcome);
  if (paired) {
    if (second) addPulse(`SECOND · ${fresh(second.card) ? "LIVE" : "WAITING"}`, second.card, second.outcome);
    else if (draft.secondTarget === "other_market" && other?.data)
      addPulse("OTHER CARD · LOCK TO POST", other, candidate?.outcome || other.outcome);
    else addPulse("SECOND", null, null);
  }
  const preview = $("preview-legs"); preview.replaceChildren();
  const addPreview = (label, target, outcome, price, state = "") => {
    const row = el("div", "", state === "LOCK PENDING" ? "preview-leg pending-leg" : "preview-leg"), identity = el("div", "", "leg-identity");
    append(identity, el("strong", `${target || "Select market"} · ${(outcome || "—").toUpperCase()}`),
      el("small", `1 contract · post-only maker${state ? ` · ${state}` : ""}`));
    append(row, el("span", label, "leg-index"), identity,
      el("strong", price !== null && price > 0n && price < 10000n ? centsText(price) + "¢" : "—", "leg-price"));
    preview.appendChild(row);
  };
  const frozen = frozenLegs();
  if (frozen) frozen.forEach((leg, index) => addPreview(`${index + 1}`, leg.ticker, leg.outcome, leg.price, "FROZEN"));
  else { addPreview("1", data?.ticker, card?.outcome, units);
    if (paired) addPreview("2", second?.card?.ticker || (draft.secondTarget === "other_market" ? other?.ticker : data?.ticker),
      second?.outcome || candidate?.outcome || (draft.secondTarget === "same_market" && card ? opposite(card.outcome) : null),
      second ? secondPrice : null, second ? "" : "LOCK PENDING"); }
  const costUnits = frozen ? frozen.reduce((total, leg) => leg.price === null ? null : total === null ? null : total + leg.price, 0n) :
    units !== null && (!paired || secondPrice !== null && secondPrice > 0n && secondPrice < 10000n) ? units + (paired ? secondPrice : 0n) : null;
  $("cost").textContent = costUnits === null ? "—" : centsText(costUnits) + (frozen?.length === 2 || paired && !frozen ? "¢ total" : "¢");
  const legs = data ? [{card, outcome: card.outcome, price: units}] : [];
  if (paired && second) legs.push({card: second.card, outcome: second.outcome, price: secondPrice});
  let reason = "";
  if (!status) reason = "Connecting to the backend.";
  else if (!backendReachable) reason = "Backend unreachable.";
  else if (!status.request_token) reason = "Refreshing local session.";
  else if (!status.trading_enabled) reason = "Trading is disabled in backend configuration.";
  else if (status.trade_scope_status === "read_only") reason = "API key has read-only access.";
  else if (status.trade_scope_status === "wrong_subaccount") reason = "API key is restricted to a different subaccount.";
  else if (!status.authentication_ready) reason = "Private exchange authentication is unavailable.";
  else if (!status.order_stream_ready) reason = "Private order stream is not ready.";
  else if (status.recovering) reason = "Recovering previous orders.";
  else if (pending) reason = "Checking the original request. Do not start another submission.";
  else if (!data) reason = "Select a loaded primary market.";
  else if (!streamHealthy) reason = "Browser stream is disconnected.";
  else if (paired && draft.secondTarget === "other_market" && !other?.data) reason = other?.ticker ?
    `Waiting for ${other.ticker} to load as the second card.` : "Load a distinct ticker on the other card.";
  else if (paired && draft.secondTarget === "other_market" && other.ticker === card.ticker) reason = "Both cards have the same ticker. Choose Opposite side of same market.";
  else if (paired && draft.secondTarget === "other_market" && !fresh(other)) reason = `Waiting for ${other.ticker}'s live book.`;
  else if (paired && draft.secondTarget === "other_market" && !draft.relationship) reason = `Choose how ${card.ticker} and ${other.ticker} settle, then lock the relationship.`;
  else if (paired && !second) reason = `${other.ticker} is loaded as the second card. Lock the relationship above to post.`;
  else if (paired && spreadUnits($("spread").value) === null) reason = "Enter a valid spread in cents (up to two decimal places).";
  else for (const leg of legs) {
    if (!fresh(leg.card) || !leg.card.data.tradable) { reason = `${leg.card.ticker} book is not live and tradable.`; break; }
    if (orders.some(order => order.ticker === leg.card.ticker && order.outcome === leg.outcome && active(order))) { reason = `${leg.card.ticker} ${leg.outcome.toUpperCase()} already has an active app order.`; break; }
    if (leg.price === null || leg.price <= 0n || leg.price >= 10000n) { reason = "Enter valid buy limits in cents."; break; }
    if (!gridFor(leg.card, leg.price)) { reason = `${leg.card.ticker} ${leg.outcome.toUpperCase()} price is outside its valid tick grid.`; break; }
    const ask = leg.card.data.quotes?.[leg.outcome]?.ask_price;
    if (ask && leg.price >= dollarUnits(ask)) { reason = `${leg.card.ticker} ${leg.outcome.toUpperCase()} limit would cross the ask.`; break; }
  }
  const readiness = $("ticket-readiness"), panel = readiness.parentElement;
  readiness.textContent = submitting ? "Submitting" : pending ? "Checking original request" : ticketError ? "Review order issue" : reason ? "Action needed" : "Ready to post";
  panel.dataset.tone = submitting || pending || reason || ticketError ? "warn" : "good";
  $("ticket-message").textContent = submitting ? `Submitting ${paired ? "both independent orders" : "one order"}…` :
    ticketError || reason || "Review both limits, then post. Prices freeze when you click.";
  $("ticket-receipt").textContent = ticketNotice;
  $("post").disabled = !!reason || submitting;
  $("post").textContent = !status?.trading_enabled ? "Read-only" : paired ? "Post both maker orders" : "Post 1 maker order";
  $("retry").hidden = !pending || submitting;
}
const renderTicket = renderManualTicket;

cards.forEach(card => {
  $(`load-${card.id}`).addEventListener("click", () => load(card));
  $(`ticker-${card.id}`).addEventListener("keydown", event => { if (event.key === "Enter") load(card); });
  $(`primary-${card.id}`).addEventListener("click", () => {
    makePrimary(card);
  });
  renderCard(card);
});
const rerender = () => { ticketError = ""; ticketNotice = ""; renderTicket(); };
$("swap-primary").addEventListener("click", () => makePrimary(otherCard()));
$("mode-single").addEventListener("click", () => { draft.mode = "single"; draft.locked = null; notifyPaperSelectionChange(); rerender(); });
$("mode-paired").addEventListener("click", () => { draft.mode = "paired"; draft.locked = null; draft.spreadTouched = false; $("spread").value = ""; notifyPaperSelectionChange(); rerender(); });
$("target-other").addEventListener("click", () => { draft.secondTarget = "other_market"; draft.locked = null; draft.spreadTouched = false; $("spread").value = ""; notifyPaperSelectionChange(); rerender(); });
$("target-same").addEventListener("click", () => { draft.secondTarget = "same_market"; draft.locked = null; draft.spreadTouched = false; $("spread").value = ""; notifyPaperSelectionChange(); rerender(); });
for (const [id, relationship] of [["relation-same", "same_outcome"], ["relation-opposite", "opposite_outcomes"]])
  $(id).addEventListener("click", () => { draft.relationship = relationship; draft.locked = null; draft.spreadTouched = false; $("spread").value = ""; notifyPaperSelectionChange(); rerender(); });
$("lock-relationship").addEventListener("click", () => {
  const card = selectedCard(), other = otherCard();
  if (!card?.data || !other?.data || card.ticker === other.ticker || !draft.relationship || !fresh(card) || !fresh(other)) return;
  draft.locked = {primaryTicker: card.ticker, primaryOutcome: card.outcome, otherTicker: other.ticker,
    otherOutcome: other.outcome, relationship: draft.relationship};
  draft.spreadTouched = false; $("spread").value = ""; rerender();
});
$("price-live").addEventListener("click", () => { draft.pricing = "live"; rerender(); });
$("price-fixed").addEventListener("click", () => { draft.pricing = "fixed"; $("price").value = centsText(primaryUnits()); rerender(); $("price").focus(); });
$("price").addEventListener("input", () => { draft.pricing = "fixed"; rerender(); });
$("spread").addEventListener("input", () => { draft.spreadTouched = true; rerender(); });
$("use-second-bid").addEventListener("click", () => {
  const second = secondSelection(), primary = primaryUnits();
  const bid = second?.card?.data?.quotes?.[second.outcome]?.bid_price;
  if (!second || primary === null || !bid) return;
  const spread = 10000n - primary - dollarUnits(bid);
  if (spread < 0n || spread > 10000n) {
    ticketError = "The two current bids imply a negative spread. Enter a spread manually.";
    renderTicket(); return;
  }
  $("spread").value = centsText(spread);
  draft.spreadTouched = true;
  rerender();
});
$("use-bid").addEventListener("click", () => {
  const card = selectedCard(), bid = card?.data?.quotes?.[card.outcome]?.bid_price;
  if (!bid) return;
  draft.pricing = "fixed"; $("price").value = centsText(dollarUnits(bid)); rerender(); $("price").focus();
});
$("post").addEventListener("click", () => {
  const clickedAt = performance.now();
  if ($("post").disabled || selected === null) return;
  const card = selectedCard(), units = primaryUnits();
  if (units === null) return;
  if (draft.mode === "single") submit({kind: "single", request_id: crypto.randomUUID(), ticker: card.data.ticker,
    outcome: card.outcome, price: dollars(units), mode: "maker"}, clickedAt);
  else {
    const second = secondSelection(), secondPrice = secondUnits(units);
    if (!second || secondPrice === null) return;
    submit({kind: "pair", pair_id: crypto.randomUUID(), primary_ticker: card.data.ticker,
      primary_outcome: card.outcome, primary_price: dollars(units), second_target: draft.secondTarget,
      other_ticker: draft.secondTarget === "other_market" ? second.card.ticker : null,
      other_outcome: draft.secondTarget === "other_market" ? otherCard().outcome : null,
      relationship: draft.secondTarget === "other_market" ? draft.relationship : null,
      second_price: dollars(secondPrice), mode: "maker"}, clickedAt);
  }
});
$("retry").addEventListener("click", () => { if (pending) submit(pending); });
setInterval(() => {
  if (streamHealthy && Date.now() - lastStateAt > (status?.ui_stream_timeout_seconds || 5) * 1000) {
    streamHealthy = false;
    updateHeader(); cards.forEach(renderCard); renderTicket();
  }
}, 500);
bootstrap();
