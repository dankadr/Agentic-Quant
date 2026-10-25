// DOM + formatting helpers. Every piece of dynamic text goes through textContent: agent output
// and news headlines are untrusted, so no module may ever build HTML from strings.

const SVG_NS = "http://www.w3.org/2000/svg";
export const ET = "America/New_York";

export function el(tag, text, cls, attrs) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

export function svg(tag, attrs, cls) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (cls) node.setAttribute("class", cls);
  return node;
}

export function clear(node) { node.replaceChildren(); return node; }
export function append(parent, ...children) { for (const c of children) if (c) parent.append(c); return parent; }

const ICONS = {
  overview: "M3 12h4l3-8 4 16 3-8h4",
  trading: "M3 17l6-6 4 4 8-8M15 7h6v6",
  strategies: "M12 2l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5",
  agents: "M12 8V4M8 4h8M5 8h14v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8zM9 13h.01M15 13h.01M9 17h6",
  research: "M10 2v6L4 19a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-6-11V2M8 2h8",
  data: "M3 5c0-1.1 4-2 9-2s9 .9 9 2-4 2-9 2-9-.9-9-2zM3 5v14c0 1.1 4 2 9 2s9-.9 9-2V5M3 12c0 1.1 4 2 9 2s9-.9 9-2",
  activity: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  pause: "M6 4h4v16H6zM14 4h4v16h-4z",
};
export function icon(name) {
  const s = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" }, "ico");
  s.append(svg("path", { d: ICONS[name] || "" }));
  return s;
}

const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export function num(value) { const n = Number(value); return Number.isFinite(n) ? n : null; }
export function fmtMoney(value) { const n = num(value); return n === null ? "-" : money.format(n); }
export function usd(value) {
  const n = num(value);
  if (n === null) return "-";
  return (n < 0 ? "-$" : "$") + money.format(Math.abs(n));
}
export function signedUsd(value) {
  const n = num(value);
  if (n === null) return "-";
  return (n > 0 ? "+" : n < 0 ? "-" : "") + "$" + money.format(Math.abs(n));
}
export function fmtPct(value, digits = 2) {
  const n = num(value);
  return n === null ? "-" : (n > 0 ? "+" : "") + (n * 100).toFixed(digits) + "%";
}
export function trend(value) {
  const n = num(value);
  if (n === null || n === 0) return "";
  return n > 0 ? "good" : "bad";
}
export function glyph(value) { const n = num(value); return n === null || n === 0 ? "" : n > 0 ? "▲ " : "▼ "; }

const timeFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ET });
const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", timeZone: ET });
export function fmtTime(iso) { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "-" : timeFmt.format(d); }
export function fmtDateTime(iso) { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "-" : dayFmt.format(d) + " " + timeFmt.format(d).slice(0, 5); }
export function ago(iso, now = Date.now()) {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (!Number.isFinite(s)) return "-";
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 172800) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

export function chip(text, level) { return el("span", text, "chip" + (level ? " " + level : "")); }
export function badge(text, level, extra) { return el("span", text, "badge" + (level ? " " + level : "") + (extra ? " " + extra : "")); }
export function empty(text) { return el("p", text, "empty"); }

// Generic table: columns [{label, get, num, cls, wrap}], rows array. Returns a node.
export function table(caption, columns, rows, emptyText) {
  if (!rows.length) return empty(emptyText || "nothing yet");
  const wrap = el("div", null, "tablewrap");
  const t = el("table");
  t.append(el("caption", caption, "sr-only"));
  const head = el("tr");
  for (const c of columns) head.append(el("th", c.label, c.num ? "num" : "", { scope: "col" }));
  t.append(append(el("thead"), head));
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const c of columns) {
      const value = c.get(row);
      const cell = value instanceof Node ? el("td", null, c.num ? "num" : "") : el("td", value, (c.num ? "num " : "") + (c.wrap ? "wrap " : "") + (c.cls ? c.cls(row) : ""));
      if (value instanceof Node) cell.append(value);
      tr.append(cell);
    }
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  return wrap;
}

export function card(title, body, opts = {}) {
  const c = el("section", null, "card " + (opts.cls || ""));
  const h = el("h2");
  h.append(el("span", title));
  if (opts.actions) { const a = el("div", null, "actions"); a.append(...opts.actions); h.append(a); }
  c.append(h);
  if (body) c.append(body);
  return c;
}

export function stable(value) { return JSON.stringify(value); }
