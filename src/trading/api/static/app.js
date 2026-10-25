import { api, ApiError, post, ready, session } from "./api.js";
import { VIEWS } from "./views.js";
import { badge, clear, el, icon, append } from "./util.js";

const REFRESH_MS = 15000;
const $ = (id) => document.getElementById(id);
const state = { route: "overview", date: null, status: null, ready: null, mounted: null, update: null, timer: null, inflight: false, pending: 0 };

// ---------- theme ----------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const b = clear($("theme"));
  b.append(icon(theme === "dark" ? "sun" : "moon"));
}
function initTheme() {
  const stored = localStorage.getItem("dashboard_theme");
  applyTheme(stored || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  $("theme").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    localStorage.setItem("dashboard_theme", next);
    applyTheme(next);
  });
}

// ---------- toasts + dialog ----------
export function toast(message, bad) {
  const t = el("div", message, "toast" + (bad ? " bad" : ""));
  $("toasts").append(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add("show")));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, bad ? 6000 : 3500);
}

function openDialog({ title, body, confirm = "Confirm", danger = false }) {
  const dlg = $("dialog");
  $("dialog-title").textContent = title;
  clear($("dialog-body")).append(body);
  const ok = $("dialog-ok");
  ok.textContent = confirm;
  ok.className = "btn " + (danger ? "bad" : "primary");
  return new Promise((resolve) => {
    let result = false;
    const finish = () => {
      dlg.classList.add("closing");
      setTimeout(() => { dlg.close(); dlg.classList.remove("closing"); resolve(result); }, 160);
    };
    $("dialog-form").onsubmit = (e) => { e.preventDefault(); result = true; finish(); };
    $("dialog-cancel").onclick = () => finish();
    dlg.oncancel = (e) => { e.preventDefault(); finish(); };
    dlg.showModal();
  });
}

// ---------- context ----------
function lastSessionDay() {
  const d = new Date();
  while ([0, 6].includes(d.getUTCDay())) d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}
function context() {
  const live = !state.date;
  const status = state.status;
  const day = state.date || (status && status.session && status.session.date) || lastSessionDay();
  const sess = status && status.session && status.session.date === day ? status.session : null;
  return {
    status, ready: state.ready, day, isLive: live && !!sess && sess.is_open,
    session: sess, openMs: sess ? new Date(sess.open_at).getTime() : new Date(day + "T13:30:00Z").getTime(),
    actions: { review: reviewProposal },
  };
}

// ---------- header ----------
function renderHeader() {
  const st = state.status;
  if (!st) return;
  const box = clear($("badges"));
  box.append(badge(st.mode, st.mode === "PAPER_ONLY" ? "good" : "bad"));
  box.append(badge(st.trading_state.replace("_", " ").toLowerCase(), st.trading_state === "PAPER_RUNNING" ? "good" : "warn", st.trading_state === "PAPER_RUNNING" ? "live" : ""));
  box.append(badge("risk " + st.risk_state.toLowerCase(), st.risk_state === "NORMAL" ? "good" : "bad"));
  const r = state.ready;
  if (r) {
    box.append(badge("data " + r.market_data.replace("_", " "), r.market_data === "ok" ? "good" : r.market_data === "closed" || r.market_data === "not_configured" ? "" : "bad"));
    box.append(badge("lease " + r.executor_lease, r.executor_lease === "held" ? "good" : "bad"));
  }
  if (st.session) box.append(badge(st.session.is_open ? "market open" : "market closed", st.session.is_open ? "good" : ""));
  $("pause").hidden = st.trading_state !== "PAPER_RUNNING";
  const input = $("date");
  input.max = new Date().toISOString().slice(0, 10);
  input.value = context().day;
  $("live").hidden = !state.date;
  const nav = $("nav");
  const count = nav.querySelector("[data-route='research'] .count");
  if (count) { count.textContent = String(state.pending); count.hidden = !state.pending; }
}

function buildNav() {
  const nav = clear($("nav"));
  for (const [id, v] of Object.entries(VIEWS)) {
    const a = el("a", null, null, { href: "#/" + id, "data-route": id });
    append(a, icon(v.icon), el("span", v.label));
    if (id === "research") { const c = el("span", "0", "count"); c.hidden = true; a.append(c); }
    nav.append(a);
  }
}
function markNav() {
  for (const a of $("nav").querySelectorAll("a")) {
    if (a.dataset.route === state.route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

// ---------- rendering ----------
function mountRoute(animate) {
  const view = clear($("view"));
  const ctx = context();
  state.update = VIEWS[state.route].mount(view, ctx);
  state.mounted = state.route;
  if (animate) {
    const cards = view.querySelectorAll(":scope .grid > *, :scope.cards > *, :scope > .cards > *");
    cards.forEach((c, i) => { c.classList.add("enter"); c.style.setProperty("--i", Math.min(i, 8)); });
    requestAnimationFrame(() => requestAnimationFrame(() => cards.forEach((c) => c.classList.add("in"))));
  }
}

async function refresh(forceMount) {
  if (state.inflight) return;
  state.inflight = true;
  try {
    const [status, rd] = await Promise.all([api("/v1/system/status"), ready()]);
    state.status = status; state.ready = rd;
    const ctx = context();
    const routeChanged = state.mounted !== state.route || forceMount;
    if (routeChanged) mountRoute(true);
    const data = await VIEWS[state.route].fetch(ctx);
    if (state.route === "overview") state.pending = data.pending || 0;
    else if (state.route === "research" && data.proposals) state.pending = data.proposals.items.filter((p) => p.status === "PENDING_REVIEW").length;
    renderHeader();
    state.update(data, ctx);
    $("updated").textContent = "updated " + new Date().toLocaleTimeString("en-GB", { hour12: false });
    showApp(true);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) { signOut("That key was rejected."); return; }
    $("updated").textContent = "error: " + e.message;
  } finally {
    state.inflight = false;
  }
}

function showApp(on) {
  for (const id of ["topbar", "nav", "view"]) $(id).hidden = !on;
  $("login").hidden = on;
}
function signOut(message) {
  session.key = null;
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  state.mounted = null; state.status = null;
  showApp(false);
  $("login-error").textContent = message || "";
}
function start() {
  if (state.timer) clearInterval(state.timer);
  refresh(true);
  state.timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, REFRESH_MS);
}

// ---------- actions ----------
async function pauseTrading() {
  if (!state.status) return;
  const body = el("p", "New entries stop immediately. Exits and the end-of-day flatten continue. You can resume from the API.");
  const ok = await openDialog({ title: "Pause trading?", body, confirm: "Pause", danger: true });
  if (!ok) return;
  try {
    const result = await post("/v1/control/pause", { reason: "paused from dashboard", expected_version: state.status.version });
    toast("Trading paused (state v" + result.version + ")");
    refresh();
  } catch (e) {
    toast("Pause failed: " + e.message + (e.status === 403 ? " (an operator key is required)" : ""), true);
  }
}

async function reviewProposal(p, approve) {
  const body = el("div", null, "stack");
  body.append(el("p", (approve ? "Approve" : "Reject") + " the proposed change to " + p.strategy_id + "? " + (approve ? "It is applied at the next session start." : "")));
  const note = el("textarea", null, null, { "aria-label": "Optional comment", placeholder: "Optional comment" });
  body.append(note);
  const ok = await openDialog({ title: approve ? "Approve proposal" : "Reject proposal", body, confirm: approve ? "Approve" : "Reject", danger: !approve });
  if (!ok) return;
  try {
    await post("/v1/config-proposals/" + p.id + "/" + (approve ? "approve" : "reject"), { comment: note.value.trim() || null });
    toast("Proposal " + (approve ? "approved" : "rejected"));
    refresh();
  } catch (e) {
    toast("Review failed: " + e.message + (e.status === 403 ? " (needs an operator key that is not the proposer)" : ""), true);
  }
}

// ---------- routing ----------
function routeFromHash() {
  const id = location.hash.replace(/^#\//, "");
  state.route = VIEWS[id] ? id : "overview";
  markNav();
  document.title = VIEWS[state.route].label + " | Paper Trading";
  if (session.key) refresh();
}

// ---------- boot ----------
initTheme();
buildNav();
$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  session.key = $("key").value.trim();
  $("key").value = "";
  $("login-error").textContent = "";
  start();
});
$("logout").addEventListener("click", () => signOut(""));
$("pause").addEventListener("click", pauseTrading);
$("date").addEventListener("change", (e) => { state.date = e.target.value || null; refresh(true); });
$("live").addEventListener("click", () => { state.date = null; refresh(true); });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && session.key) refresh(); });
window.addEventListener("hashchange", routeFromHash);
if (!location.hash) history.replaceState(null, "", "#/overview");
state.route = VIEWS[location.hash.replace(/^#\//, "")] ? location.hash.replace(/^#\//, "") : "overview";
markNav();
if (session.key) start(); else showApp(false);
