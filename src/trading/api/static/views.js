import { api, optional } from "./api.js";
import { barChart, heatmap, equityChart, meter } from "./charts.js";
import { agentState, agentTabs, loadAgent, renderAgentPanel } from "./agentpanel.js";
import { append, badge, card, chip, clear, el, empty, fmtDateTime, fmtMoney, fmtPct, fmtTime, ago, glyph, num, signedUsd, stable, table, trend, usd } from "./util.js";

// A panel remembers what it last rendered so a refresh with unchanged data touches nothing
// (no flicker, no lost hover/scroll/open <details> state).
function panel(title, opts) {
  const body = el("div");
  const node = card(title, body, opts);
  let last = null;
  return {
    node,
    set(data, render) {
      const sig = stable(data);
      if (sig === last) return;
      last = sig;
      clear(body).append(render(data));
    },
  };
}

// ---------- shared renderers ----------

function statusLevel(status) { return status === "COMPLETED" ? "good" : status === "RUNNING" ? "warn" : "bad"; }

function list(items) {
  const ul = el("ul", null, "plain");
  for (const i of items || []) ul.append(el("li", i));
  return ul;
}

function runDetail(run) {
  const r = run.result || {};
  const box = el("div", null, "stack");
  if (r.error) box.append(el("p", "Error: " + r.error, "bad"));
  switch (run.agent_name) {
    case "macro":
      if (r.regime) {
        const row = el("div", null, "row");
        row.append(chip(r.regime, r.regime === "TRENDING" ? "good" : r.regime === "HIGH_VOLATILITY" ? "bad" : "accent"),
          el("span", "entry size x" + r.risk_multiplier, "mono"), el("span", "confidence " + r.confidence, "muted small"));
        box.append(row, meter(r.risk_multiplier, Number(r.risk_multiplier) < 0.5 ? "bad" : Number(r.risk_multiplier) < 0.9 ? "warn" : "good"));
        box.append(el("p", "Valid until " + fmtDateTime(r.valid_until) + " ET", "muted small"), list(r.evidence), list(r.limitations));
      }
      break;
    case "fundamental":
      if (r.watchlist) {
        const row = el("div", null, "row");
        for (const w of r.watchlist) row.append(chip(w.rank + ". " + w.symbol, "accent"));
        box.append(row);
        box.append(table("Watchlist reasons", [
          { label: "#", get: (w) => w.rank }, { label: "Symbol", get: (w) => w.symbol },
          { label: "Why", get: (w) => w.reason, wrap: true }, { label: "Conf", num: true, get: (w) => w.confidence },
        ], r.watchlist));
        box.append(el("h3", "Catalysts"), table("Catalysts", [
          { label: "Symbol", get: (c) => c.symbol }, { label: "Kind", get: (c) => c.kind },
          { label: "What", get: (c) => c.description, wrap: true }, { label: "Date", get: (c) => c.event_date || "-" },
        ], r.catalysts || [], "no catalysts"));
        box.append(list(r.limitations));
      }
      break;
    case "consensus":
      if (r.entries !== undefined) {
        const kv = el("dl", null, "kv");
        for (const [k, v] of [["entries", r.entries], ["agreed", r.agreed], ["vetoed", r.vetoed], ["conflicts", r.conflicts],
          ["base P&L", usd(r.base_pnl)], ["consensus P&L", usd(r.consensus_pnl)], ["value added", signedUsd(r.value_added)], ["verdict", r.verdict]]) {
          kv.append(el("dt", k), el("dd", v));
        }
        box.append(kv, list(r.limitations));
      }
      break;
    case "evaluator":
      if (r.summary) box.append(el("p", r.summary, "prose"), list(r.findings), el("h3", "Suggestions"), list(r.suggestions));
      break;
    case "technical":
      if (r.assessment) {
        box.append(el("p", r.assessment, "prose"), list(r.notes));
        box.append(el("p", r.proposal_filed ? "Proposal filed for review." : "No proposal filed (" + (r.reason || "none") + ").", "muted small"));
      }
      break;
    default: break;
  }
  box.append(el("p", "model " + run.model + " | prompt " + run.prompt_version + " | " + fmtDateTime(run.started_at) + " ET", "muted small mono"));
  return box;
}

function runSummary(run) {
  const r = run.result || {};
  switch (run.agent_name) {
    case "macro": return r.conclusion || r.error || "";
    case "evaluator": return (r.summary || r.error || "").slice(0, 140);
    case "fundamental": return r.watchlist ? "watchlist " + r.watchlist.map((w) => w.symbol).join(" ") : r.error || "";
    case "consensus": return r.entries !== undefined ? r.entries + " entries, " + r.vetoed + " vetoed, value " + signedUsd(r.value_added) : "";
    case "technical": return r.assessment ? (r.proposal_filed ? "proposal filed" : "no proposal") : r.error || "";
    default: return r.error || "";
  }
}

function runItem(run, open, compact) {
  const d = el("details", null, "run");
  if (open) d.setAttribute("open", "");
  const s = el("summary");
  if (compact) append(s, el("span", runSummary(run) || "Details", "small"));
  else append(s, chip(run.agent_name), chip(run.status, statusLevel(run.status)), el("span", ago(run.started_at), "muted small"), el("span", runSummary(run), "small"));
  d.append(s, runDetail(run));
  return d;
}

// ---------- overview ----------

function deriveAlerts(ctx, data) {
  const out = [];
  const st = ctx.status;
  if (!st) return out;
  if (st.mode !== "PAPER_ONLY") out.push(["bad", "Mode is " + st.mode, "The system must only ever run PAPER_ONLY."]);
  if (st.risk_state !== "NORMAL") out.push(["bad", "Risk state " + st.risk_state, st.risk_reason || "New entries are blocked until this clears."]);
  if (st.trading_state !== "PAPER_RUNNING") out.push(["warn", "Trading is " + st.trading_state, "No new orders will be placed."]);
  const rd = ctx.ready;
  if (rd && rd.executor_lease !== "held") out.push(["bad", "Executor lease missing", "No worker is holding the trading lease."]);
  if (rd && rd.database !== "ok") out.push(["bad", "Database unreachable", ""]);
  if (rd && rd.market_data === "stale") out.push(["bad", "Market data is stale", "Fresh bars have stopped arriving."]);
  const cov = data.report && data.report.data_coverage;
  if (cov && data.report.ticks > 20) {
    const gappy = Object.entries(cov).filter(([, c]) => c.missing / data.report.ticks > 0.1).map(([s]) => s);
    if (gappy.length) out.push(["warn", gappy.length + " symbols missing >10% of bars", gappy.join(", ") + " (thin IEX volume is normal for some ETFs)"]);
  }
  if (st.session && st.session.is_open && data.report && data.report.ticks === 0 && ctx.isLive) out.push(["warn", "Market is open but no ticks yet", "The first tick arrives a minute after the open."]);
  if (data.pending > 0) out.push(["warn", data.pending + " strategy proposal" + (data.pending > 1 ? "s" : "") + " awaiting review", "Open Research to approve or reject."]);
  if (!out.length) out.push(["good", "All clear", "Mode, risk, lease, data and trading state look healthy."]);
  return out;
}

function mountOverview(root, ctx) {
  const kpiDefs = [
    ["equity", "Equity"], ["day", "Day P&L"], ["spy", "SPY (same period)"], ["exposure", "Exposure"], ["realized", "Realized P&L"], ["unreal", "Unrealized P&L"],
  ];
  const kpis = {};
  const grid = el("div", null, "grid");
  for (const [key, label] of kpiDefs) {
    const c = el("section", null, "card kpi col-2");
    const v = el("div", "-", "value");
    const sub = el("div", "", "sub");
    append(c, el("div", label, "label"), v, sub);
    kpis[key] = { v, sub, last: null };
    grid.append(c);
  }
  const alerts = panel("Alerts", { cls: "col-4" });
  const equity = panel("Equity (selected session, ET)", { cls: "col-8" });
  const now = panel("Agents right now", { cls: "col-4" });
  const positions = panel("Open positions", { cls: "col-4" });
  const pnl = panel("Strategy P&L", { cls: "col-4" });
  append(grid, equity.node, alerts.node, now.node, positions.node, pnl.node);
  root.append(grid);
  let firstChart = true;

  function setKpi(key, text, value, sub) {
    const k = kpis[key];
    if (k.last !== null && k.last !== text) {
      const dir = num(value) !== null && num(k.lastValue) !== null ? (num(value) > num(k.lastValue) ? "flash-up" : "flash-down") : null;
      if (dir) { k.v.classList.add(dir); requestAnimationFrame(() => requestAnimationFrame(() => k.v.classList.remove(dir))); }
    }
    k.last = text; k.lastValue = value;
    k.v.textContent = text;
    k.v.className = "value " + (key === "equity" || key === "exposure" ? "" : trend(value));
    k.sub.textContent = sub || "";
  }

  return (data) => {
    const p = data.portfolio, r = data.report;
    setKpi("equity", p ? usd(p.equity) : "-", p && p.equity, p ? "cash " + usd(p.cash) + " | buying power " + usd(p.buying_power) : "no snapshot yet");
    const dayPnl = r && r.equity && r.equity.pnl;
    setKpi("day", dayPnl !== null && dayPnl !== undefined ? glyph(dayPnl) + signedUsd(dayPnl) : "-", dayPnl, r && r.equity ? "max drawdown " + fmtPct(Math.abs(Number(r.equity.max_drawdown_pct))) : "");
    const spy = r && r.benchmark && r.benchmark.return_pct;
    setKpi("spy", spy !== null && spy !== undefined ? glyph(spy) + fmtPct(spy) : "-", spy, "buy-and-hold benchmark");
    setKpi("exposure", p ? usd(p.exposure) : "-", p && p.exposure, "");
    setKpi("realized", p ? signedUsd(p.realized_pnl) : "-", p && p.realized_pnl, r ? "fees " + usd(r.costs.fees) + " | slippage est. " + usd(r.costs.slippage_estimate) : "");
    setKpi("unreal", p ? signedUsd(p.unrealized_pnl) : "-", p && p.unrealized_pnl, "");

    alerts.set(deriveAlerts(ctx, data), (items) => {
      const box = el("div", null, "alerts");
      for (const [level, title, detail] of items) {
        const a = el("div", null, "alert " + level);
        const t = el("div");
        append(t, el("strong", title), detail ? el("span", detail, "muted small") : null);
        a.append(el("span", null, "dot"), t);
        box.append(a);
      }
      return box;
    });

    equity.set({ pts: data.equity && data.equity.points, s: ctx.session }, () => {
      const node = equityChart(data.equity && data.equity.points, ctx.session, firstChart);
      if (data.equity && data.equity.points && data.equity.points.length > 1) firstChart = false;
      return node;
    });

    const latest = {};
    for (const run of (data.agents && data.agents.items) || []) if (!latest[run.agent_name]) latest[run.agent_name] = run;
    now.set(latest, () => {
      const box = el("div", null, "stack");
      const macro = latest.macro;
      if (macro && macro.status === "COMPLETED") {
        const res = macro.result || {};
        const live = res.valid_until && new Date(res.valid_until).getTime() > Date.now();
        box.append(el("div", null, "row")); box.lastChild.append(chip("macro", "accent"), chip(res.regime || "?", "accent"), chip(live ? "valid" : "expired", live ? "good" : "warn"));
        box.append(meter(res.risk_multiplier, "good"), el("p", "entry size x" + res.risk_multiplier + " | " + ago(macro.started_at), "muted small"));
      } else box.append(el("p", macro ? "macro: last run " + macro.status.toLowerCase() : "macro agent has not run", "muted small"));
      for (const name of ["fundamental", "consensus", "evaluator", "technical"]) {
        const run = latest[name];
        if (!run) continue;
        const row = el("div", null, "row");
        append(row, chip(name), chip(run.status, statusLevel(run.status)), el("span", ago(run.started_at), "muted small"));
        box.append(row);
      }
      return box;
    });

    positions.set(data.positions && data.positions.items, (items) => table("Open positions", [
      { label: "Symbol", get: (x) => x.symbol }, { label: "Qty", num: true, get: (x) => x.qty },
      { label: "Value", num: true, get: (x) => (x.market_value ? usd(x.market_value) : "-") },
      { label: "P&L", num: true, get: (x) => (x.unrealized_pnl ? signedUsd(x.unrealized_pnl) : "-"), cls: (x) => trend(x.unrealized_pnl) },
    ], items || [], "flat: no open positions"));

    const strat = (r && r.strategies) || {};
    pnl.set(strat, () => table("Strategy P&L today", [
      { label: "Strategy", get: (x) => x[0] }, { label: "Signals", num: true, get: (x) => Object.values(x[1].signals).reduce((a, b) => a + b, 0) },
      { label: "Net P&L", num: true, get: (x) => glyph(x[1].net_pnl) + signedUsd(x[1].net_pnl), cls: (x) => trend(x[1].net_pnl) },
    ], Object.entries(strat), "no strategy activity yet"));
  };
}

// ---------- trading ----------

function filterBar(options, current, onPick, label) {
  const bar = el("div", null, "filters", { role: "group", "aria-label": label });
  for (const [value, text] of options) {
    const b = el("button", text, "btn ghost", { type: "button", "aria-pressed": String(value === current) });
    b.addEventListener("click", () => onPick(value));
    bar.append(b);
  }
  return bar;
}

function mountTrading(root) {
  const grid = el("div", null, "grid");
  const positions = panel("Positions", { cls: "col-12" });
  const orders = panel("Orders", { cls: "col-12" });
  const signals = panel("Signals", { cls: "col-12" });
  append(grid, positions.node, orders.node, signals.node);
  root.append(grid);
  let orderFilter = "all", signalFilter = "all", cache = {};

  function draw() {
    const d = cache;
    positions.set(d.positions && d.positions.items, (items) => table("Positions", [
      { label: "Symbol", get: (x) => x.symbol }, { label: "Qty", num: true, get: (x) => x.qty },
      { label: "Avg cost", num: true, get: (x) => fmtMoney(x.average_cost) }, { label: "Mark", num: true, get: (x) => (x.mark ? fmtMoney(x.mark) : "-") },
      { label: "Value", num: true, get: (x) => (x.market_value ? usd(x.market_value) : "-") },
      { label: "Unrealized", num: true, get: (x) => (x.unrealized_pnl ? signedUsd(x.unrealized_pnl) : "-"), cls: (x) => trend(x.unrealized_pnl) },
      { label: "By", get: (x) => (x.strategy_ids || []).join(", ") },
    ], items || [], "flat: no open positions"));

    const open = new Set(["CREATED", "RISK_APPROVED", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"]);
    const all = (d.orders && d.orders.items) || [];
    const shown = all.filter((o) => orderFilter === "all" || (orderFilter === "open" ? open.has(o.status) : orderFilter === "filled" ? o.status === "FILLED" : !open.has(o.status) && o.status !== "FILLED"));
    orders.set({ shown, orderFilter }, () => {
      const box = el("div", null, "stack");
      box.append(filterBar([["all", "All"], ["open", "Open"], ["filled", "Filled"], ["other", "Rejected / canceled"]], orderFilter, (v) => { orderFilter = v; cacheSig(); draw(); }, "Order filter"));
      box.append(table("Orders", [
        { label: "Time (ET)", get: (o) => fmtDateTime(o.created_at) }, { label: "Symbol", get: (o) => o.symbol },
        { label: "Side", get: (o) => chip(o.side, o.side === "BUY" ? "good" : "bad") }, { label: "Qty", num: true, get: (o) => o.qty },
        { label: "Filled", num: true, get: (o) => o.filled_qty }, { label: "Limit", num: true, get: (o) => (o.limit_price ? fmtMoney(o.limit_price) : "mkt") },
        { label: "Avg px", num: true, get: (o) => (o.avg_fill_price ? fmtMoney(o.avg_fill_price) : "-") },
        { label: "Status", get: (o) => chip(o.status + (o.status_reason ? ": " + o.status_reason : ""), o.status === "FILLED" ? "good" : open.has(o.status) ? "warn" : "") },
      ], shown, "no orders match"));
      return box;
    });

    const sigs = (d.signals && d.signals.items) || [];
    const ids = ["all", ...new Set(sigs.map((s) => s.strategy_id))];
    const shownSigs = sigs.filter((s) => signalFilter === "all" || s.strategy_id === signalFilter);
    signals.set({ shownSigs, signalFilter, ids }, () => {
      const box = el("div", null, "stack");
      box.append(filterBar(ids.map((i) => [i, i === "all" ? "All" : i]), signalFilter, (v) => { signalFilter = v; cacheSig(); draw(); }, "Signal filter"));
      box.append(table("Signals", [
        { label: "Time (ET)", get: (s) => fmtDateTime(s.generated_at) }, { label: "Strategy", get: (s) => s.strategy_id }, { label: "Symbol", get: (s) => s.symbol },
        { label: "Side", get: (s) => chip(s.side, s.side === "BUY" ? "good" : "bad") }, { label: "Notional", num: true, get: (s) => usd(s.target_notional) },
        { label: "Why", get: (s) => (s.reason_codes || []).join(", "), wrap: true }, { label: "Result", get: (s) => chip(s.status, s.status === "ALLOCATED" ? "good" : "") },
      ], shownSigs, "no signals match"));
      return box;
    });
  }
  function cacheSig() { /* filters change the panel signature; nothing else to invalidate */ }
  return (data) => { cache = data; draw(); };
}


// ---------- transactions ----------

function mountTransactions(root) {
  const grid = el("div", null, "grid");
  const summary = panel("Summary", { cls: "col-12" });
  const trips = panel("Round trips", { cls: "col-12" });
  const fills = panel("Fills (executions)", { cls: "col-12" });
  const ledger = panel("Cash ledger", { cls: "col-12" });
  append(grid, summary.node, trips.node, fills.node, ledger.node);
  root.append(grid);
  let filter = "all", cache = {};
  function draw() {
    const t = cache.trades || { items: [], summary: null };
    summary.set(t.summary, (sm) => {
      const row = el("div", null, "row");
      if (!sm || !sm.trades) { return empty("no trades in this window yet"); }
      const win = sm.trades ? Math.round((sm.wins / sm.trades) * 100) : 0;
      append(row, chip(sm.trades + (sm.trades === 1 ? " trade" : " trades")), chip(win + "% winners", win >= 50 ? "good" : "warn"),
        chip("net " + glyph(sm.net_pnl) + signedUsd(sm.net_pnl), Number(sm.net_pnl) >= 0 ? "good" : "bad"), chip("fees " + usd(sm.fees)));
      return row;
    });
    const ids = ["all", ...new Set(t.items.map((x) => x.strategy_id))];
    const shown = t.items.filter((x) => filter === "all" || x.strategy_id === filter);
    trips.set({ shown, filter, ids }, () => {
      const box = el("div", null, "stack");
      box.append(filterBar(ids.map((i) => [i, i === "all" ? "All" : i]), filter, (v) => { filter = v; draw(); }, "Strategy filter"));
      box.append(table("Round trips", [
        { label: "Opened (ET)", get: (x) => fmtDateTime(x.opened_at) }, { label: "Strategy", get: (x) => x.strategy_id }, { label: "Symbol", get: (x) => x.symbol },
        { label: "State", get: (x) => chip(x.status, x.status === "OPEN" ? "warn" : "") },
        { label: "Held", num: true, get: (x) => (x.hold_minutes === null ? "-" : x.hold_minutes + "m") },
        { label: "Qty", num: true, get: (x) => x.qty }, { label: "Entry", num: true, get: (x) => fmtMoney(x.avg_entry) },
        { label: "Exit", num: true, get: (x) => (x.avg_exit ? fmtMoney(x.avg_exit) : "-") },
        { label: "Fees", num: true, get: (x) => usd(x.fees) },
        { label: "Net P&L", num: true, get: (x) => glyph(x.net_pnl) + signedUsd(x.net_pnl), cls: (x) => trend(x.net_pnl) },
      ], shown, "no round trips match"));
      return box;
    });
    fills.set(cache.fills && cache.fills.items, (items) => table("Fills", [
      { label: "Time (ET)", get: (f) => fmtDateTime(f.ts) + ":" + fmtTime(f.ts).slice(6) }, { label: "Symbol", get: (f) => f.symbol },
      { label: "Side", get: (f) => chip(f.side, f.side === "BUY" ? "good" : "bad") }, { label: "Qty", num: true, get: (f) => f.qty },
      { label: "Price", num: true, get: (f) => fmtMoney(f.price) }, { label: "Notional", num: true, get: (f) => usd(f.notional) },
      { label: "Fees", num: true, get: (f) => usd(f.fees) }, { label: "Slippage est.", num: true, get: (f) => usd(f.slippage_estimate) },
      { label: "By", get: (f) => f.strategies.map((s) => s.strategy_id).join(", ") },
    ], items || [], "no fills yet"));
    ledger.set(cache.ledger && cache.ledger.items, (items) => table("Cash ledger", [
      { label: "Time (ET)", get: (e) => fmtDateTime(e.ts) }, { label: "Type", get: (e) => e.source_type },
      { label: "Amount", num: true, get: (e) => signedUsd(e.amount), cls: (e) => trend(e.amount) },
      { label: "Settles", get: (e) => e.settles_on }, { label: "Reference", get: (e) => e.source_id || "-" },
    ], items || [], "no cash movements yet"));
  }
  return (data) => { cache = data; draw(); };
}

// ---------- history ----------

function mountHistory(root) {
  const grid = el("div", null, "grid");
  const chart = panel("Daily P&L", { cls: "col-12" });
  const rows = panel("Sessions", { cls: "col-12" });
  append(grid, chart.node, rows.node);
  root.append(grid);
  return (data) => {
    const items = (data.sessions && data.sessions.items) || [];
    chart.set(items, (x) => barChart(x));
    rows.set(items, (x) => table("Sessions", [
      { label: "Date", get: (r) => r.date }, { label: "Start equity", num: true, get: (r) => usd(r.start_equity) }, { label: "End equity", num: true, get: (r) => usd(r.end_equity) },
      { label: "P&L", num: true, get: (r) => glyph(r.pnl) + signedUsd(r.pnl), cls: (r) => trend(r.pnl) }, { label: "Return", num: true, get: (r) => fmtPct(r.return_pct), cls: (r) => trend(r.return_pct) },
    ], x, "no sessions recorded yet"));
  };
}

// ---------- strategies ----------

function mountStrategies(root) {
  const wrap = el("div", null, "cards");
  root.append(wrap);
  let last = null;
  return (data) => {
    const sig = stable(data);
    if (sig === last) return;
    last = sig;
    clear(wrap);
    const report = (data.report && data.report.strategies) || {};
    const pend = {};
    for (const p of (data.proposals && data.proposals.items) || []) if (p.status === "PENDING_REVIEW") pend[p.strategy_id] = (pend[p.strategy_id] || 0) + 1;
    const posBy = {};
    for (const pos of (data.positions && data.positions.items) || []) for (const s of pos.strategy_ids || []) posBy[s] = (posBy[s] || 0) + 1;
    const reasons = {};
    for (const s of (data.signals && data.signals.items) || []) for (const rc of s.reason_codes || []) { reasons[s.strategy_id] = reasons[s.strategy_id] || {}; reasons[s.strategy_id][rc] = (reasons[s.strategy_id][rc] || 0) + 1; }
    for (const s of (data.strategies && data.strategies.items) || []) {
      const c = el("section", null, "card");
      const head = el("div", null, "agent-head");
      append(head, el("h3", s.name), chip("v" + s.version), chip(s.enabled ? "enabled" : "disabled", s.enabled ? "good" : "warn"));
      if (pend[s.id]) head.append(chip(pend[s.id] + " proposal pending", "accent"));
      c.append(head);
      const t = report[s.id];
      const kv = el("dl", null, "kv");
      kv.append(el("dt", "budget"), el("dd", usd(s.capital_budget)), el("dt", "open positions"), el("dd", posBy[s.id] || 0));
      if (t) kv.append(el("dt", "signals today"), el("dd", Object.entries(t.signals).map(([k, v]) => k + " " + v).join(", ") || "0"), el("dt", "net P&L today"), el("dd", glyph(t.net_pnl) + signedUsd(t.net_pnl)));
      c.append(kv);
      const params = Object.entries(s.params || {});
      if (params.length) {
        const d = el("details", null, "run");
        d.append(el("summary", "Parameters (" + params.length + ")"));
        const pk = el("dl", null, "kv");
        for (const [k, v] of params) pk.append(el("dt", k), el("dd", Array.isArray(v) ? v.join(", ") : String(v)));
        d.append(pk);
        c.append(d);
      }
      const rs = Object.entries(reasons[s.id] || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
      if (rs.length) { const row = el("div", null, "row"); for (const [k, v] of rs) row.append(chip(k + " x" + v)); c.append(el("p", "Recent signal reasons", "muted small"), row); }
      wrap.append(c);
    }
    if (!wrap.children.length) wrap.append(empty("no strategies"));
  };
}

// ---------- agents ----------

function mountAgents(root, ctx) {
  const grid = el("div", null, "grid");
  const tabs = panel("Agents", { cls: "col-12" });
  const host = el("div", null, "stack col-12");
  const history = panel("Run history", { cls: "col-12" });
  append(grid, tabs.node, host, history.node);
  root.append(grid);
  let cache = [], list = null, current = null, drawn = null;

  function drawTabs() { tabs.set({ list, selected: agentState.selected }, () => agentTabs(list && list.items, pick)); }
  function drawDetail(force) {
    const sig = stable({ current, drafts: agentState.drafts[agentState.selected] || null });
    if (!force && sig === drawn) return;
    drawn = sig;
    renderAgentPanel(host, current, ctx.actions, () => drawDetail(true));
  }
  function drawHistory() {
    const shown = cache.filter((r) => r.agent_name === agentState.selected);
    history.set({ shown }, () => {
      const box = el("div", null, "stack");
      if (!shown.length) box.append(empty("no runs"));
      for (const run of shown) box.append(runItem(run, false));
      return box;
    });
  }
  async function pick(name) {
    agentState.selected = name;
    drawTabs();
    current = await loadAgent(name);
    drawDetail(true);
    drawHistory();
  }
  return (data) => {
    cache = (data.runs && data.runs.items) || [];
    list = data.list;
    if (data.current && data.current.name === agentState.selected) current = data.current;
    drawTabs();
    drawDetail(false);
    drawHistory();
  };
}

// ---------- research ----------

function paramDiff(current, proposed) {
  const kv = el("dl", null, "kv diff");
  for (const [k, v] of Object.entries(proposed)) {
    const dd = el("dd");
    dd.append(el("span", current && k in current ? String(current[k]) : "?", "old"), document.createTextNode(" → "), el("span", Array.isArray(v) ? v.join(", ") : String(v), "new"));
    kv.append(el("dt", k), dd);
  }
  return kv;
}

function mountResearch(root, ctx) {
  const grid = el("div", null, "grid");
  const proposals = panel("Strategy config proposals", { cls: "col-12" });
  const experiments = panel("Experiment registry", { cls: "col-12" });
  append(grid, proposals.node, experiments.node);
  root.append(grid);
  return (data) => {
    const current = {};
    for (const s of (data.strategies && data.strategies.items) || []) current[s.id] = s;
    proposals.set(data.proposals && data.proposals.items, (items) => {
      if (!items || !items.length) return empty("no proposals yet. The technical agent files one only when walk-forward evidence supports it.");
      const box = el("div", null, "stack");
      for (const p of items) {
        const d = el("section", null, "card");
        const head = el("div", null, "agent-head");
        append(head, el("h3", p.strategy_id), chip("base v" + p.base_version), chip(p.status, p.status === "APPLIED" ? "good" : p.status === "PENDING_REVIEW" ? "accent" : p.status === "REJECTED" ? "bad" : ""),
          el("span", "by " + p.proposed_by + " | " + ago(p.created_at), "muted small"));
        d.append(head, paramDiff(current[p.strategy_id] && current[p.strategy_id].params, p.parameters), el("p", p.rationale, "prose"));
        if (p.reviewed_by) d.append(el("p", "reviewed by " + p.reviewed_by + (p.applied_version ? " | applied as v" + p.applied_version : ""), "muted small"));
        if (p.status === "PENDING_REVIEW") {
          const row = el("div", null, "row");
          const approve = el("button", "Approve", "btn primary", { type: "button" });
          const reject = el("button", "Reject", "btn bad", { type: "button" });
          approve.addEventListener("click", () => ctx.actions.review(p, true));
          reject.addEventListener("click", () => ctx.actions.review(p, false));
          append(row, approve, reject, el("span", "Approval needs a different principal than the proposer and applies at the next session start.", "muted small"));
          d.append(row);
        }
        box.append(d);
      }
      return box;
    });
    experiments.set(data.experiments && data.experiments.items, (items) => table("Experiments", [
      { label: "When", get: (e) => fmtDateTime(e.created_at) }, { label: "Kind", get: (e) => e.kind }, { label: "Strategy", get: (e) => e.strategy_id || "-" },
      { label: "Verdict", get: (e) => chip(e.metrics.verdict || (e.metrics.robust === undefined ? e.status : e.metrics.robust ? "ROBUST" : "FRAGILE"), e.status === "COMPLETED" ? "good" : e.status === "REJECTED" ? "bad" : "warn") },
      { label: "OOS P&L", num: true, get: (e) => (e.metrics.out_of_sample_net_pnl !== undefined ? signedUsd(e.metrics.out_of_sample_net_pnl) : "-") },
      { label: "p", num: true, get: (e) => (e.metrics.p_value ? Number(e.metrics.p_value).toFixed(3) : "-") },
      { label: "Days", num: true, get: (e) => e.data_range.days ?? "-" },
      { label: "Hypothesis", get: (e) => e.hypothesis, wrap: true },
    ], items || [], "no experiments yet. Run `trading research` after collecting data."));
  };
}

// ---------- data ----------

function mountData(root, ctx) {
  const grid = el("div", null, "grid");
  const heat = panel("Bars per half hour (ET)", { cls: "col-12" });
  const cov = panel("Coverage summary", { cls: "col-12" });
  append(grid, heat.node, cov.node);
  root.append(grid);
  return (data) => {
    heat.set({ c: data.coverage, live: ctx.isLive }, () => heatmap(data.coverage, ctx.isLive ? Date.now() : null, ctx.openMs));
    const r = data.report;
    cov.set(r && r.data_coverage, (c) => {
      const box = el("div", null, "stack");
      box.append(el("p", "provider " + (data.coverage ? data.coverage.provider : "-") + " | ticks " + (r ? r.ticks : 0) + " | market data " + (ctx.ready ? ctx.ready.market_data : "?"), "muted small"));
      box.append(table("Coverage summary", [
        { label: "Symbol", get: (x) => x[0] }, { label: "Bars", num: true, get: (x) => x[1].bars },
        { label: "Missing", num: true, get: (x) => x[1].missing, cls: (x) => (x[1].missing > 0 ? "warn" : "") },
        { label: "Flagged", num: true, get: (x) => x[1].flagged, cls: (x) => (x[1].flagged > 0 ? "bad" : "") },
        { label: "Coverage", num: true, get: (x) => (x[1].expected ? ((x[1].bars / x[1].expected) * 100).toFixed(1) + "%" : "-") },
      ], Object.entries(c || {}), "no data yet"));
      return box;
    });
  };
}

// ---------- activity ----------

function mountActivity(root) {
  const grid = el("div", null, "grid");
  const log = panel("Audit log (append-only)", { cls: "col-12" });
  grid.append(log.node);
  root.append(grid);
  let items = [], cursor = null, older = false, loading = false;
  async function more() {
    if (!cursor || loading) return;
    loading = true;
    try { const page = await api("/v1/audit?limit=50&cursor=" + encodeURIComponent(cursor)); items = items.concat(page.items); cursor = page.next_cursor; older = true; draw(); } finally { loading = false; }
  }
  function draw() {
    log.set({ items, cursor }, () => {
      const box = el("div", null, "stack");
      box.append(table("Audit log", [
        { label: "Time (ET)", get: (a) => fmtDateTime(a.ts) + ":" + fmtTime(a.ts).slice(6) }, { label: "Actor", get: (a) => a.actor },
        { label: "Action", get: (a) => a.action }, { label: "Target", get: (a) => a.target, wrap: true }, { label: "Result", get: (a) => chip(a.result, a.result === "ok" || a.result === "APPLIED" ? "good" : "") },
      ], items, "no audit events"));
      if (cursor) { const b = el("button", "Load older", "btn", { type: "button" }); b.addEventListener("click", more); box.append(b); }
      return box;
    });
  }
  return (data) => { if (!older) { items = (data.audit && data.audit.items) || []; cursor = data.audit && data.audit.next_cursor; } draw(); };
}

// ---------- registry ----------

export const VIEWS = {
  overview: {
    label: "Overview", icon: "overview", mount: mountOverview,
    fetch: (c) => Promise.all([optional("/v1/portfolio"), optional("/v1/equity?date=" + c.day), optional("/v1/report/daily?date=" + c.day), optional("/v1/agents/runs?limit=20"),
      optional("/v1/positions?limit=50"), optional("/v1/config-proposals?status=PENDING_REVIEW&limit=50"), optional("/v1/strategies")])
      .then(([portfolio, equity, report, agents, positions, props, strategies]) => ({ portfolio, equity, report, agents, positions, strategies, pending: props ? props.items.length : 0 })),
  },
  trading: {
    label: "Trading", icon: "trading", mount: mountTrading,
    fetch: () => Promise.all([optional("/v1/positions?limit=100"), optional("/v1/orders?limit=80"), optional("/v1/signals?limit=80")]).then(([positions, orders, signals]) => ({ positions, orders, signals })),
  },
  transactions: {
    label: "Transactions", icon: "transactions", mount: mountTransactions,
    fetch: (c) => {
      const scoped = c.explicit ? "?date=" + c.day + "&" : "?";
      return Promise.all([optional("/v1/trades" + (c.explicit ? "?date=" + c.day : "?days=30")), optional("/v1/fills" + scoped + "limit=150"), optional("/v1/ledger?limit=100")])
        .then(([trades, fills, ledger]) => ({ trades, fills, ledger }));
    },
  },
  history: { label: "History", icon: "history", mount: mountHistory, fetch: () => optional("/v1/sessions?limit=60").then((sessions) => ({ sessions })) },
  strategies: {
    label: "Strategies", icon: "strategies", mount: mountStrategies,
    fetch: (c) => Promise.all([optional("/v1/strategies"), optional("/v1/report/daily?date=" + c.day), optional("/v1/signals?limit=100"), optional("/v1/config-proposals?limit=50"), optional("/v1/positions?limit=100")])
      .then(([strategies, report, signals, proposals, positions]) => ({ strategies, report, signals, proposals, positions })),
  },
  agents: {
    label: "Agents", icon: "agents", mount: mountAgents,
    fetch: () => Promise.all([optional("/v1/agents"), optional("/v1/agents/runs?limit=80"), loadAgent(agentState.selected)]).then(([list, runs, current]) => ({ list, runs, current })),
  },
  research: {
    label: "Research", icon: "research", mount: mountResearch,
    fetch: () => Promise.all([optional("/v1/config-proposals?limit=50"), optional("/v1/research/experiments?limit=50"), optional("/v1/strategies")]).then(([proposals, experiments, strategies]) => ({ proposals, experiments, strategies })),
  },
  data: {
    label: "Data", icon: "data", mount: mountData,
    fetch: (c) => Promise.all([optional("/v1/data/coverage?date=" + c.day), optional("/v1/report/daily?date=" + c.day)]).then(([coverage, report]) => ({ coverage, report })),
  },
  activity: { label: "Activity", icon: "activity", mount: mountActivity, fetch: () => optional("/v1/audit?limit=50").then((audit) => ({ audit })) },
};
