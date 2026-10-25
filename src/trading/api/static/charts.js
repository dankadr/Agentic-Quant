import { ET, el, svg, usd, signedUsd, fmtTime, num } from "./util.js";

const W = 900, H = 280, PAD = { l: 62, r: 26, t: 14, b: 26 };
const hourFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ET });

function niceTicks(lo, hi, count) {
  const step = (hi - lo) / count;
  return Array.from({ length: count + 1 }, (_, i) => lo + step * i);
}

// Equity curve with a hover/keyboard crosshair. `session` = {open_at, close_at} fixes the x axis
// so a live day fills in from the left. `animate` draws the line in once (first paint only).
export function equityChart(points, session, animate) {
  const root = el("div", null, "chart");
  if (!points || points.length < 2) { root.append(el("p", "no intraday points yet", "empty")); return root; }
  const times = points.map((p) => new Date(p.ts).getTime());
  const values = points.map((p) => Number(p.equity));
  const open = session && session.open_at ? new Date(session.open_at).getTime() : times[0];
  const close = session && session.close_at ? new Date(session.close_at).getTime() : times[times.length - 1];
  const x0 = Math.min(open, times[0]), x1 = Math.max(close, times[times.length - 1]);
  const baseline = values[0];
  let lo = Math.min(...values, baseline), hi = Math.max(...values, baseline);
  const pad = Math.max((hi - lo) * 0.15, 0.25);
  lo -= pad; hi += pad;
  const sx = (t) => PAD.l + ((t - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r);
  const sy = (v) => H - PAD.b - ((v - lo) / (hi - lo || 1)) * (H - PAD.t - PAD.b);
  const up = values[values.length - 1] >= baseline;

  const s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "group", tabindex: "0",
    "aria-label": "Equity curve for the session. Use the left and right arrow keys to inspect points." });
  const defs = svg("defs");
  for (const [id, color] of [["fill-up", "#3ddc97"], ["fill-down", "#ff7b7b"]]) {
    const g = svg("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
    g.append(svg("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.28 }), svg("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
    defs.append(g);
  }
  s.append(defs);

  for (const v of niceTicks(lo, hi, 4)) {
    s.append(svg("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(v), y2: sy(v) }, "grid-line"));
    const label = svg("text", { x: PAD.l - 8, y: sy(v) + 4, "text-anchor": "end" }, "axis");
    label.textContent = "$" + v.toFixed(2);
    s.append(label);
  }
  for (let t = Math.ceil(x0 / 3600000) * 3600000; t <= x1; t += 3600000) {
    const label = svg("text", { x: sx(t), y: H - 6, "text-anchor": sx(t) > W - PAD.r - 14 ? "end" : "middle" }, "axis");
    label.textContent = hourFmt.format(new Date(t));
    s.append(label);
  }
  s.append(svg("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(baseline), y2: sy(baseline) }, "baseline"));

  const d = points.map((p, i) => (i ? "L" : "M") + sx(times[i]).toFixed(1) + " " + sy(values[i]).toFixed(1)).join(" ");
  const bottom = sy(lo);
  s.append(svg("path", { d: d + " L" + sx(times[times.length - 1]).toFixed(1) + " " + bottom + " L" + sx(times[0]).toFixed(1) + " " + bottom + " Z" }, "area " + (up ? "up" : "down")));
  const line = svg("path", { d, pathLength: 1 }, "line " + (up ? "up" : "down") + (animate ? " draw" : ""));
  s.append(line);

  const cross = svg("line", { y1: PAD.t, y2: H - PAD.b, x1: 0, x2: 0 }, "cross");
  const dot = svg("circle", { r: 4, cx: 0, cy: 0 }, "dot");
  s.append(cross, dot);
  const hit = svg("rect", { x: PAD.l, y: PAD.t, width: W - PAD.l - PAD.r, height: H - PAD.t - PAD.b, fill: "transparent" });
  s.append(hit);

  const tip = el("div", null, "tip", { role: "status" });
  root.append(s, tip);

  let index = values.length - 1;
  function show(i, fromPointer) {
    index = Math.max(0, Math.min(values.length - 1, i));
    const x = sx(times[index]), y = sy(values[index]);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x);
    dot.setAttribute("cx", x); dot.setAttribute("cy", y);
    tip.textContent = fmtTime(points[index].ts) + " ET  " + usd(values[index]) + "  (" + signedUsd(values[index] - baseline) + " vs open)";
    const rect = s.getBoundingClientRect();
    const px = (x / W) * rect.width;
    tip.style.transform = "translateX(" + Math.max(0, Math.min(rect.width - tip.offsetWidth, px - tip.offsetWidth / 2)) + "px)";
    root.classList.add("active");
    if (!fromPointer) tip.setAttribute("aria-live", "polite");
  }
  function nearest(clientX) {
    const rect = s.getBoundingClientRect();
    const t = x0 + ((clientX - rect.left) / rect.width * W - PAD.l) / (W - PAD.l - PAD.r) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < times.length; i++) if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i;
    return best;
  }
  hit.addEventListener("pointermove", (e) => show(nearest(e.clientX), true));
  hit.addEventListener("pointerdown", (e) => show(nearest(e.clientX), true));
  hit.addEventListener("pointerleave", () => root.classList.remove("active"));
  s.addEventListener("keydown", (e) => {
    const k = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: values.length - 1 }[e.key];
    if (k !== undefined) { e.preventDefault(); show(k, false); }
  });
  s.addEventListener("blur", () => root.classList.remove("active"));
  if (animate) requestAnimationFrame(() => requestAnimationFrame(() => line.classList.add("go")));
  return root;
}

// Bars received per symbol per half hour. A real <table>, so it is also the accessible alternative.
export function heatmap(coverage, nowMs, openMs) {
  const wrap = el("div", null, "tablewrap");
  if (!coverage || !coverage.symbols.length) { wrap.append(el("p", "no session on this date", "empty")); return wrap; }
  const t = el("table", null, "heat");
  t.append(el("caption", "Bars received per symbol per half hour of the session", "sr-only"));
  const head = el("tr");
  head.append(el("th", "", "", { scope: "col" }));
  for (let i = 0; i < coverage.buckets; i++) {
    const mins = 9 * 60 + 30 + i * coverage.bucket_minutes;
    head.append(el("th", String(Math.floor(mins / 60)).padStart(2, "0") + ":" + String(mins % 60).padStart(2, "0"), "col", { scope: "col" }));
  }
  const thead = el("thead");
  thead.append(head);
  t.append(thead);
  const body = el("tbody");
  for (const row of coverage.symbols) {
    const tr = el("tr");
    tr.append(el("th", row.symbol, "", { scope: "row" }));
    row.bars.forEach((n, i) => {
      const start = openMs + i * coverage.bucket_minutes * 60000;
      let level = "lv3", text = String(n);
      if (nowMs !== null && start > nowMs) { level = "future"; text = "·"; }
      else {
        const ratio = n / coverage.expected_per_bucket;
        level = n === 0 ? "lv0" : ratio < 0.5 ? "lv1" : ratio < 0.9 ? "lv2" : "lv3";
      }
      const td = el("td", text, level, { title: row.symbol + " bucket " + (i + 1) + ": " + n + "/" + coverage.expected_per_bucket + " bars" + (row.flagged[i] ? ", " + row.flagged[i] + " flagged" : "") });
      tr.append(td);
    });
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  const legend = el("div", null, "legend");
  for (const [cls, label] of [["lv3", "90-100% of minutes"], ["lv2", "50-90%"], ["lv1", "under 50%"], ["lv0", "no bars"]]) {
    const item = el("span");
    item.append(el("i", null, "heat-swatch " + cls), document.createTextNode(label));
    legend.append(item);
  }
  wrap.append(legend);
  return wrap;
}

export function meter(value, level) {
  const m = el("div", null, "meter" + (level ? " " + level : ""), { role: "meter", "aria-valuemin": 0, "aria-valuemax": 1, "aria-valuenow": num(value) ?? 0 });
  const fill = el("span");
  m.append(fill);
  requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.transform = "scaleX(" + Math.max(0, Math.min(1, num(value) ?? 0)) + ")"; }));
  return m;
}
