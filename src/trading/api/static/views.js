import { api, optional, checked } from "./api.js";
import { barChart, heatmap, equityChart, meter } from "./charts.js";
import { renderPerformance } from "./performance.js";
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
      const next = render(data);
      // Retain keyed meters so unrelated result updates do not replay their fill.
      const previous = new Map([...body.querySelectorAll(".meter[data-meter-key]")].map(m => [m.dataset.meterKey, m]));
      for (const fresh of next.querySelectorAll(".meter[data-meter-key]")) {
        const existing = previous.get(fresh.dataset.meterKey);
        if (!existing) continue;
        existing.className = fresh.className;
        existing.setAttribute("aria-valuenow", fresh.getAttribute("aria-valuenow"));
        const target = fresh.firstElementChild.style.transform;
        if (existing.firstElementChild.style.transform !== target) existing.firstElementChild.style.transform = target;
        fresh.replaceWith(existing);
      }
      clear(body).append(next);
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

function readableResult(value) {
  if (value === null || typeof value !== "object") return el("p", value == null ? "Unavailable" : String(value), "small");
  const dl = el("dl", null, "kv");
  for (const [key, item] of Object.entries(value)) { const dd = el("dd"); dd.append(readableResult(item)); dl.append(el("dt", key.replaceAll("_", " ")), dd); }
  return dl;
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
        const riskMeter = meter(r.risk_multiplier, Number(r.risk_multiplier) < 0.5 ? "bad" : Number(r.risk_multiplier) < 0.9 ? "warn" : "good");
        riskMeter.dataset.meterKey = "macro:" + run.id;
        box.append(row, riskMeter);
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
    case "data_quality":
    case "execution_quality":
    case "opportunities":
      if (r.conclusion) box.append(el("p", r.conclusion, "prose"));
      if (r.reason) box.append(el("p", "Reason: " + r.reason, "muted small"));
      if (r.data_at) box.append(el("p", "Data as of " + fmtDateTime(r.data_at) + " ET", "muted small"));
      for (const [key, value] of Object.entries(r)) {
        if (["conclusion", "reason", "data_at", "error"].includes(key)) continue;
        const details = el("details", null, "run");
        details.append(el("summary", key.replaceAll("_", " ")), readableResult(value));
        box.append(details);
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
    default: return r.conclusion || r.reason || r.error || "";
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

function beforeMarketOpen(ctx) {
  return !ctx.explicit && !!ctx.session && Date.now() < new Date(ctx.session.open_at).getTime();
}

function deriveAlerts(ctx, data) {
  const out = [];
  const st = ctx.status;
  if (!st) out.push(["warn", "System status unavailable", "Health could not be verified."]);
  for (const name of data.loadErrors || []) out.push(["warn", name + " unavailable", "Retry to refresh this section."]);
  if (!ctx.ready) out.push(["warn", "Readiness unavailable", "Lease and market data health could not be verified."]);
  if (!st) return out;
  if (beforeMarketOpen(ctx)) out.push(["", "Market opens at " + fmtTime(ctx.session.open_at) + " ET", "Waiting for today’s session. The account baseline is recorded 30 minutes before the open; market data and intraday results begin after the first tick."]);
  if (st.mode !== "PAPER_ONLY") out.push(["bad", "Mode is " + st.mode, "The system must only ever run PAPER_ONLY."]);
  if (st.risk_state !== "NORMAL") out.push(["bad", "Risk state " + st.risk_state, st.risk_reason || "New entries are blocked until this clears."]);
  if (st.trading_state !== "PAPER_RUNNING") out.push(["warn", "Trading is " + st.trading_state, "No new orders will be placed."]);
  const rd = ctx.ready;
  if (rd && rd.executor_lease !== "held") out.push(["bad", "Executor lease missing", "No worker is holding the trading lease."]);
  if (rd && rd.database !== "ok") out.push(["bad", "Database unreachable", ""]);
  if (rd && rd.market_data === "stale") out.push(["bad", "Market data is stale", "Fresh bars have stopped arriving."]);
  if (rd && rd.market_data === "incomplete") out.push(["warn", "Market data incomplete", "Some configured symbols lack current bars; check Data for coverage."]);
  const cov = data.report && data.report.data_coverage;
  if (cov && data.report.ticks > 20) {
    const gappy = Object.entries(cov).filter(([, c]) => c.missing / data.report.ticks > 0.1).map(([s]) => s);
    if (gappy.length) out.push(["warn", gappy.length + " symbols missing >10% of bars", gappy.join(", ") + " (thin IEX volume is normal for some ETFs)"]);
  }
  if (st.session && st.session.is_open && data.report && data.report.ticks === 0 && ctx.isLive) out.push(["warn", "Market is open but no ticks yet", "The first tick arrives a minute after the open."]);
  for (const agent of (data.agentList && data.agentList.items) || []) {
    if (!agent.enabled || !["failing", "degraded"].includes(agent.health)) continue;
    const state = (agent.schedule_state || "schedule unknown").replaceAll("_", " ");
    const skipped = agent.last_skip_reason ? "Last skipped: " + agent.last_skip_reason.replaceAll("_", " ") + ". " : "";
    out.push([agent.health === "failing" ? "bad" : "warn", agent.name + " needs attention", agent.health + "; " + state + ". " + skipped + (agent.last_run_at ? "Last run " + ago(agent.last_run_at) + "." : "No run recorded.")]);
  }
  if (data.pending > 0) out.push(["warn", data.pending + " strategy proposal" + (data.pending > 1 ? "s" : "") + " awaiting review", "Open Research to approve or reject."]);
  if (!out.length) out.push(["good", "All clear", "Mode, risk, lease, data and trading state look healthy."]);
  return out;
}

function mountOverview(root, ctx) {
  const heading = el("header", null, "overview-heading");
  const title = el("div");
  title.append(el("h1", "Trading operations"), el("p", "Your paper portfolio, execution health and agent activity in one workspace.", "muted"));
  const sessionLabel = el("div", null, "session-label");
  heading.append(title, sessionLabel);
  root.append(heading);
  const kpiDefs = [
    ["equity", "Equity"], ["day", "Day P&L"], ["spy", "SPY (same period)"], ["exposure", "Current exposure"], ["realized", "Day realized change"], ["unreal", "Day unrealized change"],
  ];
  const kpis = {};
  const grid = el("div", null, "grid overview-grid");
  for (const [key, label] of kpiDefs) {
    const c = el("section", null, "card kpi col-2");
    const v = el("div", "Loading…", "value");
    const sub = el("div", "", "sub");
    append(c, el("div", label, "label"), v, sub);
    kpis[key] = { v, sub, last: null };
    grid.append(c);
  }
  const alerts = panel("Attention", { cls: "col-12" });
  alerts.node.classList.add("attention-panel");
  grid.prepend(alerts.node);
  alerts.set("loading", () => empty("Loading system checks…"));
  const equity = panel("Equity (selected session, ET)", { cls: "col-8" });
  const now = panel("Agent roster", { cls: "col-4 agents-status" });
  const positions = panel("Open positions", { cls: "col-4" });
  const pnl = panel("Strategy P&L", { cls: "col-4" });
  const costs = panel("Session costs & reconciliation", { cls: "col-6" });
  const diagnostics = panel("Sampled NO_ENTRY evaluations", { cls: "col-6" });
  const performance = el("div", null, "performance-layout col-12");
  const content = el("div", null, "performance-content");
  content.append(equity.node, positions.node, pnl.node);
  performance.append(content, now.node);
  append(grid, performance, costs.node, diagnostics.node);
  root.append(grid);
  let firstChart = true;

  function setKpi(key, text, value, sub) {
    const k = kpis[key];
    if (k.flashFrame) cancelAnimationFrame(k.flashFrame);
    k.v.className = "value " + (key === "equity" || key === "exposure" ? "" : trend(value));
    if (k.last !== null && k.last !== text) {
      const dir = num(value) !== null && num(k.lastValue) !== null ? (num(value) > num(k.lastValue) ? "flash-up" : "flash-down") : null;
      if (dir) {
        k.v.classList.add(dir);
        k.flashFrame = requestAnimationFrame(() => {
          k.flashFrame = requestAnimationFrame(() => { k.v.classList.remove(dir); k.flashFrame = null; });
        });
      }
    }
    k.last = text; k.lastValue = value;
    k.v.textContent = text;
    k.sub.textContent = sub || "";
  }

  return (data, nextCtx) => {
    if (nextCtx) ctx = nextCtx;
    sessionLabel.replaceChildren(el("span", ctx.explicit ? "Selected session" : "Current session", "muted small"), el("strong", ctx.day), el("span", "Eastern time", "muted small"));
    const p = data.portfolio, r = data.report;
    const eq = r && r.equity;
    const premarket = beforeMarketOpen(ctx);
    const waiting = premarket && r;
    const missing = r ? "No session snapshot" : "Report unavailable";
    const pendingValue = waiting ? "Awaiting open" : "Unavailable";
    const present = (v) => v !== null && v !== undefined;
    const latestEquity = waiting && p && present(p.equity);
    setKpi("equity", eq && present(eq.end) ? usd(eq.end) : latestEquity ? usd(p.equity) : "Unavailable", eq && present(eq.end) ? eq.end : latestEquity ? p.equity : null, eq && present(eq.end) ? "selected session end" : latestEquity ? "Latest account snapshot · " + fmtDateTime(p.as_of) + " ET" : missing);
    const dayPnl = eq && eq.pnl;
    setKpi("day", present(dayPnl) ? glyph(dayPnl) + signedUsd(dayPnl) : pendingValue, dayPnl, "selected session · excludes capital flows");
    const spy = r && r.benchmark && r.benchmark.return_pct;
    setKpi("spy", present(spy) ? glyph(spy) + fmtPct(spy) : pendingValue, spy, "buy-and-hold benchmark");
    setKpi("exposure", !ctx.explicit && p ? usd(p.exposure) : "Unavailable", !ctx.explicit && p && p.exposure, ctx.explicit ? "unavailable for historical date" : p ? "latest portfolio snapshot" : "Portfolio unavailable");
    for (const [key, field] of [["realized", "realized_pnl_change"], ["unreal", "unrealized_pnl_change"]]) {
      const value = eq && eq[field];
      setKpi(key, present(value) ? signedUsd(value) : pendingValue, value, "change during selected session");
    }

    alerts.set(deriveAlerts(ctx, data), (items) => {
      const box = el("div", null, "alerts");
      for (const [level, title, detail] of items) {
        const a = el("div", null, "alert " + level);
        const t = el("div");
        append(t, el("strong", title), detail ? el("span", detail, "muted small") : null);
        a.append(el("span", null, "dot"), t);
        box.append(a);
      }
      if ((data.loadErrors || []).length) {
        const retry = el("button", "Retry unavailable sections", "btn", { type: "button" });
        retry.addEventListener("click", () => ctx.actions.refresh());
        box.append(retry);
      }
      return box;
    });

    equity.set({ pts: data.equity && data.equity.points, s: ctx.session, premarket, available: !!data.equity }, () => {
      if (!data.equity) return empty("Equity chart unavailable");
      if (premarket && !(data.equity.points || []).length) return empty("Awaiting the first market tick after " + fmtTime(ctx.session.open_at) + " ET.");
      const node = equityChart(data.equity && data.equity.points, ctx.session, firstChart);
      if (data.equity && data.equity.points && data.equity.points.length > 1) firstChart = false;
      return node;
    });

    const latest = {};
    for (const run of (data.agents && data.agents.items) || []) if (!latest[run.agent_name]) latest[run.agent_name] = run;
    now.set({ latest, list: data.agentList, errors: data.loadErrors, explicit: ctx.explicit }, () => {
      if (!data.agentList) return empty("Agent status unavailable");
      const box = el("div", null, "stack");
      box.append(el("p", "Current health · " + (ctx.explicit ? "selected session runs" : "latest run per agent"), "muted small"));
      box.append(table("Current agent health and latest runs", [
        { label: "Agent", get: agent => agent.name.replaceAll("_", " ") },
        { label: "Now", get: agent => chip(agent.enabled ? agent.health : "disabled", agent.enabled ? ({ ok: "good", degraded: "warn", failing: "bad" }[agent.health] || "") : "") },
        { label: "Latest run", wrap: true, get: agent => {
          const run = latest[agent.name];
          const cell = el("div", null, "small");
          cell.append(el("div", run ? run.status + " · " + ago(run.started_at) : (data.loadErrors || []).includes("Agent runs") ? "Unavailable" : "No run recorded"), el("div", (agent.schedule_state || "schedule unknown").replaceAll("_", " "), "muted"));
          return cell;
        } },
      ], data.agentList.items || []));
      return box;
    });

    positions.set({ items: data.positions && data.positions.items, historical: ctx.explicit }, ({ items, historical }) => historical ? empty("Current positions unavailable for historical date") : !items ? empty("Positions unavailable") : table("Current open positions", [
      { label: "Symbol", get: (x) => x.symbol }, { label: "Qty", num: true, get: (x) => x.qty },
      { label: "Value", num: true, get: (x) => (x.market_value ? usd(x.market_value) : "-") },
      { label: "P&L", num: true, get: (x) => (x.unrealized_pnl != null ? signedUsd(x.unrealized_pnl) : "-"), cls: (x) => trend(x.unrealized_pnl) },
    ], items || [], "flat: no open positions"));

    const strat = (r && r.strategies) || {};
    pnl.set({ strat, available: !!r }, () => !r ? empty("Report unavailable") : table("Strategy realized P&L for selected session", [
      { label: "Strategy", get: (x) => x[0] }, { label: "Signals", num: true, get: (x) => Object.values(x[1].signals).reduce((a, b) => a + b, 0) },
      { label: "Realized net P&L", num: true, get: (x) => glyph(x[1].net_pnl) + signedUsd(x[1].net_pnl), cls: (x) => trend(x[1].net_pnl) },
    ], Object.entries(strat), "no strategy activity yet"));
    costs.set(r, () => {
      if (!r) return empty("Report unavailable");
      const box = el("div", null, "stack"), kv = el("dl", null, "kv");
      for (const [label, value] of [["Fees", r.costs?.fees], ["Slippage estimate", r.costs?.slippage_estimate], ["Capital flows", eq?.capital_flows], ["P&L reconciliation residual", eq?.reconciliation_residual], ["Attribution residual", eq?.attribution_residual]]) kv.append(el("dt", label), el("dd", present(value) ? usd(value) : "Unavailable"));
      box.append(kv, el("p", "Slippage is already reflected in fill prices; do not subtract it again. Reconciliation residual compares session P&L with realized and unrealized changes.", "muted small"));
      return box;
    });
    diagnostics.set(r && r.diagnostic_counts, (counts) => {
      if (!counts) return empty("Sampled evaluation data unavailable");
      const box = el("div", null, "stack");
      box.append(el("p", counts.sampled_evaluations + " sampled evaluations, not unique opportunities.", "muted small"));
      const rows = Object.entries(counts.by_strategy || {}).flatMap(([strategy, reasons]) => Object.entries(reasons).map(([reason, count]) => ({ strategy, reason, count })));
      box.append(table("Sampled NO_ENTRY reasons", [{ label: "Strategy", get: x => x.strategy }, { label: "Reason", get: x => x.reason, wrap: true }, { label: "Samples", num: true, get: x => x.count }], rows, "No sampled NO_ENTRY evaluations"));
      return box;
    });
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
  const performance = panel("Agent scorecards", { cls: "col-12" });
  const tabs = panel("Agents", { cls: "col-12" });
  const host = el("div", null, "stack col-12");
  const history = panel("Run history", { cls: "col-12" });
  append(grid, performance.node, tabs.node, host, history.node);
  root.append(grid);
  let cache = [], list = null, current = null, drawn = null;

  function drawTabs() { tabs.set({ list, selected: agentState.selected }, () => agentTabs(list && list.items, pick)); }
  function drawDetail(force) {
    const sig = stable(current);  // drafts are deliberately not part of it: typing must not redraw
    if (!force && sig === drawn) return;
    if (!force && host.contains(document.activeElement)) return;  // never yank a field mid-typing
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
    performance.set(data.performance, () => renderPerformance(data.performance, () => ctx.actions.refresh()));
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
    fetch: async (c) => {
      const paths = { portfolio: ["Portfolio", "/v1/portfolio"], equity: ["Equity chart", "/v1/equity?date=" + c.day], report: ["Report", "/v1/report/daily?date=" + c.day], agents: ["Agent runs", "/v1/agents/runs?latest_per_agent=true" + (c.explicit ? "&date=" + c.day : "")], agentList: ["Agent status", "/v1/agents"], positions: ["Positions", "/v1/positions?limit=50"], props: ["Proposals", "/v1/config-proposals?status=PENDING_REVIEW&limit=50"], strategies: ["Strategies", "/v1/strategies"] };
      const results = await Promise.all(Object.entries(paths).map(async ([key, [label, path]]) => [key, label, await checked(path)]));
      const data = { loadErrors: [] };
      for (const [key, label, result] of results) { data[key] = result.data; if (result.error) data.loadErrors.push(label); }
      data.pending = data.props ? data.props.items.length : null;
      return data;
    },
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
    fetch: () => Promise.all([optional("/v1/agents"), optional("/v1/agents/runs?limit=80"), loadAgent(agentState.selected), checked("/v1/agents/performance?days=30")]).then(([list, runs, current, evidence]) => ({ list, runs, current, performance: evidence.data })),
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
