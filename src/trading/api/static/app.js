"use strict";
// Every piece of dynamic text goes through textContent. Agent output and news headlines are
// untrusted, so this file must never build HTML from strings.

const REFRESH_MS = 15000;
const SVG_NS = "http://www.w3.org/2000/svg";
const KEY_NAME = "dashboard_api_key";
let timer = null;
let lastStatus = null;

const $ = (id) => document.getElementById(id);

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function clear(node) { node.replaceChildren(); return node; }

function fmtMoney(value) {
  if (value === null || value === undefined) return "-";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(value);
}
function fmtPct(value) {
  if (value === null || value === undefined) return "-";
  const n = Number(value) * 100;
  return Number.isFinite(n) ? n.toFixed(2) + "%" : String(value);
}
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString("en-GB", { hour12: false }) + " " + d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}
function signClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "good" : "bad";
}

async function api(path, options) {
  const key = sessionStorage.getItem(KEY_NAME);
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: "Bearer " + key, ...(options && options.headers) },
    cache: "no-store",
  });
  if (response.status === 401) { throw new Error("unauthorized"); }
  if (!response.ok) {
    let detail = "";
    try { const body = await response.json(); detail = body.detail || body.title || ""; } catch (e) { /* not json */ }
    const error = new Error("HTTP " + response.status + (detail ? " " + detail : ""));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function optional(path) {
  try { return await api(path); } catch (e) { if (e.message === "unauthorized") throw e; return null; }
}

function table(columns, rows, emptyText) {
  if (!rows.length) return el("p", emptyText || "nothing yet", "empty");
  const t = el("table");
  const head = el("tr");
  columns.forEach((c) => head.append(el("th", c.label, c.num ? "num" : "")));
  const thead = el("thead"); thead.append(head); t.append(thead);
  const body = el("tbody");
  rows.forEach((row) => {
    const tr = el("tr");
    columns.forEach((c) => {
      const raw = c.get(row);
      const cls = [c.num ? "num" : "", c.cls ? c.cls(row) : ""].join(" ").trim();
      tr.append(el("td", raw, cls));
    });
    body.append(tr);
  });
  t.append(body);
  return t;
}

function badge(text, level) { return el("span", text, "badge " + (level || "")); }

function renderBadges(status, ready) {
  const box = clear($("badges"));
  box.append(badge(status.mode, status.mode === "PAPER_ONLY" ? "good" : "bad"));
  box.append(badge(status.trading_state, status.trading_state === "PAPER_RUNNING" ? "good" : "warn"));
  box.append(badge("risk " + status.risk_state, status.risk_state === "NORMAL" ? "good" : "bad"));
  if (ready) {
    box.append(badge("data " + ready.market_data, ready.market_data === "ok" ? "good" : (ready.market_data === "closed" ? "" : "warn")));
    box.append(badge("lease " + ready.executor_lease, ready.executor_lease === "held" ? "good" : "bad"));
  }
  if (status.session) box.append(badge(status.session.is_open ? "market open" : "market closed", status.session.is_open ? "good" : ""));
}

function renderKpis(portfolio, report) {
  const box = clear($("kpis"));
  const add = (label, value, cls) => {
    const k = el("div", null, "kpi");
    k.append(el("div", label, "label"), el("div", value, "value " + (cls || "")));
    box.append(k);
  };
  if (portfolio) {
    add("Equity", "$" + fmtMoney(portfolio.equity));
    add("Cash", "$" + fmtMoney(portfolio.cash));
    add("Buying power", "$" + fmtMoney(portfolio.buying_power));
    add("Exposure", "$" + fmtMoney(portfolio.exposure));
    add("Realized P&L", "$" + fmtMoney(portfolio.realized_pnl), signClass(portfolio.realized_pnl));
    add("Unrealized P&L", "$" + fmtMoney(portfolio.unrealized_pnl), signClass(portfolio.unrealized_pnl));
  } else {
    add("Portfolio", "no snapshot yet");
  }
  if (report && report.equity && report.equity.pnl !== null) {
    add("Day P&L", "$" + fmtMoney(report.equity.pnl), signClass(report.equity.pnl));
    if (report.benchmark && report.benchmark.return_pct !== null) add("SPY today", fmtPct(report.benchmark.return_pct), signClass(report.benchmark.return_pct));
  }
}

function renderEquity(points) {
  const box = clear($("equity-chart"));
  if (!points || points.length < 2) { box.append(el("p", "no intraday points yet", "empty")); return; }
  const values = points.map((p) => Number(p.equity));
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const W = 800, H = 180, PAD = 6;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("preserveAspectRatio", "none");
  const coords = values.map((v, i) => {
    const x = PAD + (i / (values.length - 1)) * (W - 2 * PAD);
    const y = H - PAD - ((v - lo) / span) * (H - 2 * PAD);
    return x.toFixed(1) + "," + y.toFixed(1);
  });
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", coords.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", values[values.length - 1] >= values[0] ? "#3fb950" : "#f85149");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(line);
  box.append(svg);
  box.append(el("p", "low $" + fmtMoney(lo) + "  |  high $" + fmtMoney(hi) + "  |  " + points.length + " points, " + fmtTime(points[0].ts) + " to " + fmtTime(points[points.length - 1].ts), "muted"));
}

function renderHealth(ready, report) {
  const box = clear($("health"));
  if (ready) {
    box.append(el("p", "database " + ready.database + " | market data " + ready.market_data + " | executor lease " + ready.executor_lease));
  }
  const cov = clear($("coverage"));
  if (!report || !report.data_coverage) return;
  const rows = Object.entries(report.data_coverage).map(([symbol, c]) => ({ symbol, ...c }));
  box.append(el("p", "ticks today: " + report.ticks, "muted"));
  cov.append(table([
    { label: "Symbol", get: (r) => r.symbol },
    { label: "Bars", num: true, get: (r) => r.bars },
    { label: "Missing", num: true, get: (r) => r.missing, cls: (r) => (r.missing > 0 ? "warn" : "") },
    { label: "Flagged", num: true, get: (r) => r.flagged, cls: (r) => (r.flagged > 0 ? "bad" : "") },
  ], rows));
}

function renderStrategies(strategies, report) {
  const box = clear($("strategies"));
  const perf = (report && report.strategies) || {};
  const rows = (strategies || []).map((s) => ({ ...s, today: perf[s.id] }));
  box.append(table([
    { label: "Strategy", get: (r) => r.id },
    { label: "Ver", get: (r) => r.version },
    { label: "On", get: (r) => (r.enabled ? "yes" : "no") },
    { label: "Budget", num: true, get: (r) => "$" + fmtMoney(r.capital_budget) },
    { label: "Signals", num: true, get: (r) => (r.today ? Object.values(r.today.signals).reduce((a, b) => a + b, 0) : 0) },
    { label: "P&L", num: true, get: (r) => (r.today ? fmtMoney(r.today.net_pnl) : "-"), cls: (r) => (r.today ? signClass(r.today.net_pnl) : "") },
  ], rows));
}

function renderPositions(items) {
  clear($("positions")).append(table([
    { label: "Symbol", get: (r) => r.symbol },
    { label: "Qty", num: true, get: (r) => r.qty },
    { label: "Avg cost", num: true, get: (r) => fmtMoney(r.average_cost) },
    { label: "Mark", num: true, get: (r) => (r.mark ? fmtMoney(r.mark) : "-") },
    { label: "Unrealized", num: true, get: (r) => (r.unrealized_pnl ? fmtMoney(r.unrealized_pnl) : "-"), cls: (r) => signClass(r.unrealized_pnl) },
    { label: "By", get: (r) => (r.strategy_ids || []).join(", ") },
  ], items || [], "flat - no open positions"));
}

function renderOrders(items) {
  clear($("orders")).append(table([
    { label: "Time", get: (r) => fmtTime(r.created_at) },
    { label: "Symbol", get: (r) => r.symbol },
    { label: "Side", get: (r) => r.side, cls: (r) => (r.side === "BUY" ? "good" : "bad") },
    { label: "Qty", num: true, get: (r) => r.qty },
    { label: "Filled", num: true, get: (r) => r.filled_qty },
    { label: "Limit", num: true, get: (r) => (r.limit_price ? fmtMoney(r.limit_price) : "mkt") },
    { label: "Avg px", num: true, get: (r) => (r.avg_fill_price ? fmtMoney(r.avg_fill_price) : "-") },
    { label: "Status", get: (r) => r.status + (r.status_reason ? " (" + r.status_reason + ")" : "") },
  ], items || [], "no orders yet"));
}

function renderSignals(items) {
  clear($("signals")).append(table([
    { label: "Time", get: (r) => fmtTime(r.generated_at) },
    { label: "Strategy", get: (r) => r.strategy_id },
    { label: "Symbol", get: (r) => r.symbol },
    { label: "Side", get: (r) => r.side, cls: (r) => (r.side === "BUY" ? "good" : "bad") },
    { label: "Notional", num: true, get: (r) => "$" + fmtMoney(r.target_notional) },
    { label: "Why", get: (r) => (r.reason_codes || []).join(", ") },
    { label: "Status", get: (r) => r.status },
  ], items || [], "no signals yet"));
}

function agentSummary(run) {
  const r = run.result || {};
  switch (run.agent_name) {
    case "macro": return r.conclusion ? r.conclusion + " (valid until " + fmtTime(r.valid_until) + ", confidence " + r.confidence + ")" : (r.error || "");
    case "evaluator": return r.summary || r.error || "";
    case "fundamental": return r.watchlist ? "watchlist: " + r.watchlist.map((w) => w.symbol).join(", ") + " | " + (r.catalysts || []).length + " catalysts" : (r.error || "");
    case "consensus": return r.entries !== undefined ? r.entries + " entries, " + r.agreed + " agreed, " + r.vetoed + " vetoed; value added $" + fmtMoney(r.value_added) + " (" + r.verdict + ")" : "";
    case "technical": return r.assessment ? r.assessment + (r.proposal_filed ? " -> proposal filed" : " (no proposal: " + r.reason + ")") : (r.error || "");
    default: return r.error || "";
  }
}

function renderAgents(runs) {
  const box = clear($("agents"));
  if (!runs || !runs.length) { box.append(el("p", "no agent runs yet - agents are off or have not fired", "empty")); return; }
  const seen = new Set();
  runs.forEach((run) => {
    if (seen.has(run.agent_name) && run.status === "COMPLETED") return; // newest per agent, plus any failures
    seen.add(run.agent_name);
    const card = el("div", null, "agent");
    const level = run.status === "COMPLETED" ? "good" : (run.status === "RUNNING" ? "warn" : "bad");
    const title = el("div", null, "title");
    title.append(el("span", run.agent_name + " "), badge(run.status, level), el("span", " " + fmtTime(run.started_at) + " | " + run.model, "muted"));
    card.append(title);
    const summary = agentSummary(run);
    if (summary) card.append(el("p", summary));
    box.append(card);
  });
}

function renderCosts(report) {
  const box = clear($("costs"));
  if (!report) { box.append(el("p", "no report yet", "empty")); return; }
  box.append(el("p", "fees $" + fmtMoney(report.costs.fees) + " | est. slippage $" + fmtMoney(report.costs.slippage_estimate)));
  box.append(el("p", "orders " + report.orders.total + " | fills " + report.orders.fills));
  const denials = Object.entries(report.risk_denials || {});
  if (!denials.length) box.append(el("p", "no risk denials today", "muted"));
  else box.append(table([{ label: "Risk denial", get: (r) => r[0] }, { label: "Count", num: true, get: (r) => r[1] }], denials));
}

function sessionDate(status) {
  if (status && status.session && status.session.date) return status.session.date;
  return new Date().toISOString().slice(0, 10);
}

async function refresh() {
  try {
    const status = await api("/v1/system/status");
    lastStatus = status;
    const day = sessionDate(status);
    const [ready, portfolio, positions, orders, signals, strategies, equity, report, agents] = await Promise.all([
      fetch("/v1/health/ready", { cache: "no-store" }).then((r) => r.json()).catch(() => null),
      optional("/v1/portfolio"),
      optional("/v1/positions?limit=50"),
      optional("/v1/orders?limit=15"),
      optional("/v1/signals?limit=15"),
      optional("/v1/strategies"),
      optional("/v1/equity?date=" + day),
      optional("/v1/report/daily?date=" + day),
      optional("/v1/agents/runs?limit=30"),
    ]);
    renderBadges(status, ready);
    renderKpis(portfolio, report);
    renderEquity(equity && equity.points);
    renderHealth(ready, report);
    renderStrategies(strategies && strategies.items, report);
    renderPositions(positions && positions.items);
    renderOrders(orders && orders.items);
    renderSignals(signals && signals.items);
    renderAgents(agents && agents.items);
    renderCosts(report);
    $("updated").textContent = "updated " + new Date().toLocaleTimeString("en-GB", { hour12: false });
    showMain(true);
  } catch (e) {
    if (e.message === "unauthorized") { signOut("That key was rejected."); return; }
    $("updated").textContent = "error: " + e.message;
  }
}

function showMain(on) {
  $("main").hidden = !on;
  $("login").hidden = on;
  $("pause").hidden = !on;
  $("logout").hidden = !on;
}

function signOut(message) {
  sessionStorage.removeItem(KEY_NAME);
  if (timer) { clearInterval(timer); timer = null; }
  showMain(false);
  $("login-error").textContent = message || "";
}

async function pauseTrading() {
  if (!lastStatus) return;
  if (!window.confirm("Pause trading? New entries stop; exits and flattening continue.")) return;
  try {
    const result = await api("/v1/control/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ reason: "paused from dashboard", expected_version: lastStatus.version }),
    });
    $("updated").textContent = "paused (state v" + result.version + ")";
    refresh();
  } catch (e) {
    window.alert("Pause failed: " + e.message + (e.status === 403 ? " (an operator key is required)" : ""));
  }
}

function start() {
  if (timer) clearInterval(timer);
  refresh();
  timer = setInterval(refresh, REFRESH_MS);
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem(KEY_NAME, $("key").value.trim());
  $("key").value = "";
  $("login-error").textContent = "";
  start();
});
$("logout").addEventListener("click", () => signOut(""));
$("pause").addEventListener("click", pauseTrading);

if (sessionStorage.getItem(KEY_NAME)) start(); else showMain(false);
