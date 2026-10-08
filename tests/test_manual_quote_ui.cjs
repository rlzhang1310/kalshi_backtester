const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.handlers = {};
    this.attributes = {}; this.classList = {toggle() {}}; this.parentElement = {dataset: {}};
    this._text = ""; this.value = ""; this.hidden = false; this.disabled = false; this.open = false;
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  appendChild(child) { this.children.push(child); child.parentElement = this; return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this._text = ""; this.children = []; children.forEach(child => this.appendChild(child)); }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  remove() { const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this), 1); }
  focus() {}
  click() { return this.handlers.click?.({target: this}); }
  input() { return this.handlers.input?.({target: this}); }
}

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static/index.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
const requests = [], sources = [];
const paperState = {configured: false, observing: false, pair: null, current: [], episodes: [],
  would_submit: [], simulated_results: [], simulated_pnl: [], latency_ms: {}, evidence_file: null,
  evidence_overflow: 0, own_liquidity_note: "No identified own liquidity"};
const browserEvents = {};
const data = ticker => ({ticker, title: ticker, status: "active", book_state: "live", stale: false,
  tradable: true, price_ranges: [{start: "0", end: "1", step: "0.01"}],
  quotes: {yes: {bid_price: "0.46", bid_quantity: "5", ask_price: "0.49", ask_quantity: "5", spread: "0.03"},
    no: {bid_price: "0.51", bid_quantity: "5", ask_price: "0.54", ask_quantity: "5", spread: "0.03"}},
  depth: {yes: {bids: [], asks: []}, no: {bids: [], asks: []}}, rules: {primary: "Test rules", secondary: ""}});
const status = {environment: "demo", trading_enabled: true, authentication_ready: true,
  trade_scope_status: "allowed", recovering: false, connected: true, order_stream_ready: true,
  request_token: "token", order_expiry_seconds: 300};
const context = vm.createContext({
  document: {getElementById: id => elements[id], createElement: tag => new Element(tag)},
  window: {addEventListener: (name, callback) => { browserEvents[name] = callback; },
    dispatchEvent: event => browserEvents[event.type]?.(event)},
  Event: class { constructor(type) { this.type = type; } },
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  crypto: {randomUUID: () => "12345678-1234-4234-8234-123456789abc"},
  performance: {now: () => Date.now()}, Date, BigInt, Number, JSON, console,
  setInterval() {}, setTimeout() {},
  EventSource: class { constructor(url) { this.url = url; this.handlers = {}; sources.push(this); }
    addEventListener(name, callback) { this.handlers[name] = callback; } close() {} },
  fetch: async (url, options = {}) => {
    requests.push({url, method: options.method || "GET", body: options.body});
    if (url.startsWith("/api/paper")) {
      if (url === "/api/paper/configure") {
        paperState.configured = true; paperState.observing = false;
        paperState.pair = JSON.parse(options.body);
        paperState.simulated_pnl = paperState.pair.delays_ms.map(delay_ms => ({delay_ms, attempts: 0,
          paired: 0, unmatched: 0, missed: 0, paired_net: "0", unmatched_outlay: "0"}));
      } else if (url === "/api/paper/start") {
        paperState.observing = true;
        paperState.simulated_pnl[0] = {...paperState.simulated_pnl[0], attempts: 1, paired: 1, paired_net: "0.08"};
      }
      else if (url === "/api/paper/stop") paperState.observing = false;
      else if (url === "/api/paper/reset") {
        paperState.configured = false; paperState.observing = false; paperState.pair = null;
        paperState.simulated_pnl = [];
      }
      return {ok: true, status: 200, headers: {get: () => null}, json: async () => paperState};
    }
    const body = url === "/api/status" ? status : url === "/api/orders" ? [] :
      url.includes("/api/markets/") ? data(decodeURIComponent(url.split("/")[3])) :
      options.method === "POST" ? [] : null;
    return {ok: !!body, status: body ? 200 : 404, headers: {get: () => null}, json: async () => body};
  },
});
vm.runInContext(fs.readFileSync(path.join(root, "static/app.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(root, "static/paper.js"), "utf8"), context);
const settle = () => new Promise(resolve => setImmediate(resolve));
const sides = id => {
  const visit = node => node.className === "side-option" ? [node] : node.children.flatMap(visit);
  return visit(elements[`market-${id}`]);
};
const pushState = (markets, revision) => {
  const source = sources.at(-1);
  source.handlers.state({data: JSON.stringify({schema_version: 2, stream_epoch: "test-epoch", revision,
    server_time: new Date().toISOString(), status, markets, orders: []})});
};

(async () => {
  await settle();
  elements["ticker-a"].value = "TEST-A"; await elements["load-a"].click(); await settle();
  elements["ticker-b"].value = "TEST-B"; await elements["load-b"].click(); await settle();
  pushState([data("TEST-A"), data("TEST-B")], 1);
  const yesRow = sides("a")[0];
  sides("a")[0].click();
  assert.match(sides("a")[0].textContent, /YESPRIMARY/);
  assert.match(sides("a")[1].textContent, /NOCHOOSE/, "unselected outcome must not be marked primary");
  assert.match(sides("b")[0].textContent, /YESOTHER/);
  assert.equal(elements.price.value, "46");
  elements["mode-paired"].click(); elements["target-same"].click();
  assert.match(elements["preview-legs"].children[1].textContent, /TEST-A · NO.*51¢/);
  assert.equal(elements.post.disabled, false);
  elements["price-fixed"].click(); elements.price.value = "45"; elements.price.input();
  const moved = data("TEST-A"); moved.quotes.yes.bid_price = "0.47";
  pushState([moved, data("TEST-B")], 2);
  assert.equal(sides("a")[0], yesRow, "quote updates must keep the watchlist row mounted");
  assert.match(yesRow.textContent, /BID47¢Size 5ASK49¢Size 5/, "bid, ask, and sizes must stay in labeled slots");
  assert.equal(elements.price.value, "45", "fixed price must not follow the bid");
  elements["price-live"].click();
  assert.equal(elements.price.value, "47", "live price must resume following");
  elements["target-other"].click(); elements["relation-same"].click();
  elements["lock-relationship"].click();
  assert.equal(elements["lock-relationship"].hidden, true, "locked relationship should not keep showing a redundant action");
  assert.match(elements["preview-legs"].textContent, /TEST-B · NO/);
  sides("b")[1].click();
  assert.equal(elements.post.disabled, true, "outcome change must clear relationship lock");
  assert.equal(elements["lock-relationship"].hidden, false, "changed outcome must show the required relock action");
  elements["lock-relationship"].click();
  assert.match(elements["preview-legs"].textContent, /TEST-B · YES/);
  elements["use-second-bid"].click();
  assert.equal(elements.spread.value, "7", "second bid shortcut should set spread from actual mapped outcome");
  assert.equal(requests.filter(request => request.method === "POST").length, 0,
    "selection and stream updates must not submit or cancel orders");
  elements.post.click();
  assert.match(elements["preview-legs"].textContent, /FROZEN/, "in-flight preview must retain clicked prices");
  const newer = data("TEST-A"); newer.quotes.yes.bid_price = "0.48";
  pushState([newer, data("TEST-B")], 3);
  assert.match(elements["preview-legs"].children[0].textContent, /TEST-A · YES.*47¢/, "later quotes must not edit in-flight preview");
  elements.post.click(); await settle();
  const posted = requests.filter(request => request.method === "POST");
  assert.equal(posted.length, 1, "one click must send one paired request");
  const body = JSON.parse(posted[0].body);
  assert.equal(body.primary_ticker, "TEST-A");
  assert.equal(body.primary_price, "0.47", "later quotes must not edit submitted payload");
  assert.equal(body.other_outcome, "no", "request must use the selected outcome before mapping");
  assert.equal(body.relationship, "same_outcome");
  assert.equal(body.second_price, "0.46");
  elements["primary-b"].click();
  assert.equal(elements.post.disabled, true, "primary change must clear relationship lock");
  assert.equal(elements["other-market-name"].textContent, "TEST-A", "old primary must remain loaded as the other card");
  assert.match(elements["preview-legs"].children[1].textContent, /TEST-A · NO.*—/, "new second leg must show its ticker and mapped outcome before relocking");
  assert.match(elements["ticket-message"].textContent, /TEST-A is loaded as the second card/, "readiness must describe the actual next action");
  elements["lock-relationship"].click();
  assert.match(elements["preview-legs"].textContent, /TEST-A · NO/);
  elements.spread.value = "0"; elements.spread.input();
  assert.equal(elements.post.disabled, false, "relocked pair with valid spread must be ready");
  elements["swap-primary"].click();
  assert.equal(elements["other-market-name"].textContent, "TEST-B");
  assert.equal(elements.post.disabled, true, "swap control must also clear the lock");
  const decimalQuote = data("TEST-A"); decimalQuote.quotes.yes.bid_price = "0.475";
  pushState([decimalQuote, data("TEST-B")], 4);
  assert.equal(sides("a")[0], yesRow, "a longer decimal quote must not rebuild the watchlist row");
  assert.match(yesRow.textContent, /BID47\.5¢Size 5ASK49¢Size 5/);
  elements["lock-relationship"].click();
  elements["paper-min-profit"].value = "0";
  elements["paper-safety"].value = "0";
  elements["paper-delays"].value = "0,25,50,100";
  elements["tab-paper"].click(); await settle();
  await elements["paper-configure"].click(); await settle();
  assert.equal(paperState.configured, true);
  assert.equal(paperState.pair.quantity, "1");
  assert.deepEqual(paperState.pair.delays_ms, [0, 25, 50, 100]);
  assert.equal(paperState.pair.direct_account, false);
  assert.equal(paperState.pair.settlement_asserted, true);
  assert.equal(elements["paper-direct"], undefined);
  assert.equal(elements["paper-settlement"], undefined);
  await elements["paper-start"].click(); await settle();
  assert.equal(paperState.observing, true);
  assert.match(elements["paper-state"].textContent, /Observing/);
  elements["paper-view-pnl"].click();
  assert.equal(elements["paper-pnl-panel"].hidden, false);
  assert.equal(elements["paper-signals-panel"].hidden, true);
  assert.match(elements["paper-pnl-rows"].textContent, /0 ms1/);
  assert.match(elements["paper-pnl-rows"].textContent, /\$0\.08/);
  elements["paper-view-signals"].click();
  assert.equal(elements["paper-signals-panel"].hidden, false);
  await elements["paper-stop"].click(); await settle();
  assert.equal(paperState.observing, false);
  elements["tab-manual"].click();
  sides("b")[0].click(); await settle();
  assert.equal(paperState.configured, false, "changing the locked pair must reset paper observation");
  assert.equal(requests.filter(request => request.url.startsWith("/api/orders") && request.method === "POST").length, 0);
  console.log("manual quote UI smoke: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
