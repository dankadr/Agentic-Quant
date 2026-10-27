// One agent's panel: numbers, rules, settings (reviewed edits), per-decision "why", suggestions.
// Everything an agent or an API returns is rendered through textContent (see util.js): model output
// is untrusted. Locked scopes (risk, broker, strategies, LLM endpoint) are listed, never editable.
import { optional } from "./api.js";
import { append, badge, chip, clear, el, empty, ago, fmtDateTime } from "./util.js";

export const AGENT_ORDER = ["macro", "fundamental", "evaluator", "consensus", "technical"];
export const agentState = { selected: "macro", drafts: {} };

const HEALTH_LEVEL = { ok: "good", degraded: "warn", failing: "bad", idle: "" };

export async function loadAgent(name) {
  const base = "/v1/agents/" + encodeURIComponent(name);
  const [detail, decisions, suggestions] = await Promise.all([
    optional(base), optional(base + "/decisions?limit=20"), optional(base + "/suggestions"),
  ]);
  return { name, detail, decisions, suggestions };
}

export function agentTabs(items, onPick) {
  const bar = el("div", null, "filters", { role: "group", "aria-label": "Agent" });
  const byName = Object.fromEntries((items || []).map((a) => [a.name, a]));
  for (const name of AGENT_ORDER) {
    const a = byName[name];
    const b = el("button", null, "btn ghost", { type: "button", "aria-pressed": String(name === agentState.selected) });
    b.append(el("span", null, "dot " + (a ? HEALTH_LEVEL[a.health] || "" : "")), el("span", name));
    if (a && !a.enabled) b.append(el("span", "off", "muted small"));
    b.addEventListener("click", () => onPick(name));
    bar.append(b);
  }
  return bar;
}

function section(title, body) {
  const c = el("section", null, "card");
  c.append(el("h2", title), body);
  return c;
}

function kv(pairs) {
  const dl = el("dl", null, "kv");
  for (const [k, v] of pairs) dl.append(el("dt", k), v instanceof Node ? append(el("dd"), v) : el("dd", v));
  return dl;
}

function numbers(d) {
  const s = d.detail.summary;
  const bad = s.rejected + s.failed;
  const row = el("div", null, "row");
  row.append(
    badge(s.health, HEALTH_LEVEL[s.health] || ""),
    chip(s.runs + " runs / 24 h"),
    chip(s.completed + " valid" + (bad ? ", " + bad + " rejected or failed" : ""), bad ? "warn" : "good"),
    chip(s.prompt_tokens + " prompt / " + s.completion_tokens + " completion tokens"),
    el("span", s.last_run_at ? "last run " + ago(s.last_run_at) : "never ran", "muted small"),
  );
  const box = el("div", null, "stack");
  box.append(row);
  if (s.last_error) box.append(el("p", "Last error: " + s.last_error, "bad small"));
  return section("Numbers", box);
}

function rules(d) {
  const i = d.detail.info;
  const box = el("div", null, "stack");
  box.append(kv([
    ["Job", i.job],
    ["Can affect", i.can_affect.join("; ")],
    ["Cannot", i.cannot.join("; ")],
    ["Model", i.uses_llm ? "LLM, prompt " + i.prompt_version : "deterministic, no model (" + i.prompt_version + ")"],
    ["Valid output", i.valid_output],
    ["When it fails", i.on_failure],
  ]));
  if (i.prompt_text) {
    const det = el("details", null, "run");
    det.append(el("summary", "Prompt text (read-only)"), el("pre", i.prompt_text, "mono small wrap"));
    box.append(det);
  }
  return section("Rules this agent follows", box);
}

function parse(field, text) {
  if (Array.isArray(field.value)) return text.split(",").map((t) => t.trim()).filter(Boolean);
  return Number(text);
}

function draftOf(name) { return (agentState.drafts[name] = agentState.drafts[name] || {}); }

function settings(d, actions, redraw) {
  const detail = d.detail;
  const draft = draftOf(d.name);
  const box = el("div", null, "stack");
  const inputs = {};
  const sync = () => {
    const changes = {};
    for (const f of detail.config) {
      if (!(f.name in draft)) continue;
      if (JSON.stringify(draft[f.name]) !== JSON.stringify(f.value)) changes[f.name] = draft[f.name];
    }
    review.disabled = Object.keys(changes).length === 0;
    review.textContent = Object.keys(changes).length ? "Review change (" + Object.keys(changes).length + ")" : "Review change";
    return changes;
  };
  for (const f of detail.config) {
    const row = el("div", null, "row");
    row.append(el("label", f.name, "mono", { for: "cfg-" + f.name }));
    let input;
    if (typeof f.value === "boolean") {
      input = el("input", null, null, { type: "checkbox", id: "cfg-" + f.name });
      input.checked = f.name in draft ? draft[f.name] : f.value;
      input.addEventListener("change", () => { draft[f.name] = input.checked; sync(); });
    } else {
      input = el("input", null, "mono", { type: Array.isArray(f.value) ? "text" : "number", id: "cfg-" + f.name });
      input.value = f.name in draft ? [].concat(draft[f.name]).join(", ") : [].concat(f.value).join(", ");
      input.addEventListener("input", () => { draft[f.name] = parse(f, input.value); sync(); });
    }
    inputs[f.name] = input;
    row.append(input);
    if (f.pending !== null && f.pending !== undefined) row.append(chip("approved: " + [].concat(f.pending).join(", ") + " (applies after the session)", "accent"));
    box.append(row);
  }
  const locked = el("p", null, "muted small");
  locked.textContent = "Locked, not editable here: " + detail.locked.join(", ") + ".";
  const review = el("button", "Review change", "btn primary", { type: "button", disabled: "" });
  const reset = el("button", "Reset", "btn ghost", { type: "button" });
  reset.addEventListener("click", () => { agentState.drafts[d.name] = {}; redraw(); });
  review.addEventListener("click", async () => {
    const changes = sync();
    if (!Object.keys(changes).length) return;
    if (await actions.agentChange(d.name, changes, detail.config)) { agentState.drafts[d.name] = {}; redraw(); }
  });
  const buttons = el("div", null, "row");
  append(buttons, review, reset);
  sync();
  box.append(buttons, locked, el("p", "Edits are saved as a proposal, shown as a before/after diff, and apply only after the trading session ends. Every change is logged.", "muted small"));
  return section("Settings", box);
}

function decisions(d) {
  const items = (d.decisions && d.decisions.items) || [];
  const box = el("div", null, "stack");
  if (!items.length) box.append(empty("no decisions yet"));
  for (const x of items) {
    const det = el("details", null, "run");
    const sum = el("summary");
    append(sum, el("span", fmtDateTime(x.at), "mono small"), document.createTextNode(" "),
      chip(x.status, x.status === "COMPLETED" ? "good" : "bad"), document.createTextNode(" "),
      el("span", x.headline), x.confidence ? el("span", " confidence " + x.confidence, "muted small") : null);
    det.append(sum);
    const body = el("div", null, "stack");
    if (x.error) body.append(el("p", "Error: " + x.error, "bad"));
    if (x.evidence.length) body.append(el("p", "Evidence", "label"), listOf(x.evidence));
    if (x.limitations.length) body.append(el("p", "Limits it stated", "label"), listOf(x.limitations));
    if (x.affected_orders.length) body.append(el("p", "Orders sized under this view: " + x.affected_orders.join(", "), "small"));
    if (x.input_hash) body.append(el("p", "input " + x.input_hash, "muted small mono"));
    det.append(body);
    box.append(det);
  }
  return section("Recent decisions: why", box);
}

function listOf(items) {
  const ul = el("ul", null, "plain");
  for (const i of items) ul.append(el("li", i));
  return ul;
}

function suggestions(d, actions) {
  const items = (d.suggestions && d.suggestions.items) || [];
  const box = el("div", null, "stack");
  if (!items.length) box.append(empty("nothing to suggest: not enough evidence of a problem"));
  for (const s of items) {
    const c = el("div", null, "run");
    const head = el("div", null, "row");
    head.append(el("strong", s.title), chip(s.status === "proposable" ? "proposable" : "watching", s.status === "proposable" ? "accent" : ""),
      chip("evidence " + s.evidence_n + " / " + s.evidence_needed));
    c.append(head, el("p", s.rationale, "muted small"));
    if (s.field && s.proposed !== null && s.proposed !== undefined) {
      c.append(el("p", s.field + ": " + s.current + " → " + s.proposed, "mono small"));
      if (s.status === "proposable") {
        const b = el("button", "Save as proposal", "btn", { type: "button" });
        b.addEventListener("click", () => actions.agentSuggestion(d.name, s));
        c.append(b);
      }
    }
    box.append(c);
  }
  return section("AI suggestions from past trades", box);
}

function proposals(d, actions) {
  const items = d.detail.proposals || [];
  if (!items.length) return null;
  const box = el("div", null, "stack");
  for (const p of items) {
    const row = el("div", null, "row");
    const diff = Object.entries(p.changes).map(([k, v]) => k + ": " + [].concat(p.base[k]).join(", ") + " → " + [].concat(v).join(", ")).join("; ");
    append(row, chip(p.status, p.status === "APPLIED" ? "good" : p.status === "REJECTED" ? "bad" : p.status === "PENDING_REVIEW" ? "warn" : "accent"),
      el("span", diff, "mono small"), el("span", p.proposed_by + ", " + ago(p.created_at), "muted small"));
    if (p.status === "PENDING_REVIEW") {
      const ok = el("button", "Approve", "btn", { type: "button" });
      const no = el("button", "Reject", "btn ghost", { type: "button" });
      ok.addEventListener("click", () => actions.agentReview(p, true));
      no.addEventListener("click", () => actions.agentReview(p, false));
      append(row, ok, no);
    }
    box.append(row);
  }
  return section("Edit history", box);
}

export function renderAgentPanel(host, data, actions, redraw) {
  clear(host);
  if (!data || !data.detail) { host.append(empty("agent data unavailable")); return; }
  append(host, numbers(data), rules(data), settings(data, actions, redraw), decisions(data), suggestions(data, actions), proposals(data, actions));
}
