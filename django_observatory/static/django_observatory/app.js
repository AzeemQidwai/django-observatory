/* django-observatory UI. Vanilla JS, no dependencies, no network beyond this site. */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { return null; }
  }

  /* theme: OS preference by default, toggle overrides both ways */
  var root = document.documentElement;
  var saved = store("obs-theme");
  if (saved) root.setAttribute("data-theme", saved);
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-theme-toggle]");
    if (!t) return;
    var dark = root.getAttribute("data-theme")
      ? root.getAttribute("data-theme") === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.setAttribute("data-theme", dark ? "light" : "dark");
    store("obs-theme", dark ? "light" : "dark");
  });

  function fmt(v, unit) {
    if (v === null || v === undefined) return "–";
    if (unit === "ms") return v >= 1000 ? (v / 1000).toFixed(2) + " s" : v.toFixed(v < 10 ? 1 : 0) + " ms";
    if (unit === "%") return (v % 1 ? v.toFixed(1) : String(v)) + "%";
    var s = Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (v % 1 ? v.toFixed(2) : String(v));
    return unit ? s + " " + unit : s;
  }
  function timeLabel(iso, withDate) {
    var d = new Date(iso);
    var t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return withDate ? d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t : t;
  }

  /* ---- tooltips -------------------------------------------------------------
     One floating element, positioned from the pointer in viewport coordinates
     (position: fixed), so it can never be clipped by a scrolling card and never
     depends on SVG layout properties. */
  var tipEl = null;
  function tipShow(ev, build) {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "tip";
      tipEl.setAttribute("role", "status");
      document.body.appendChild(tipEl);
    }
    tipEl.textContent = "";
    build(tipEl);
    tipEl.style.display = "block";
    var w = tipEl.offsetWidth, h = tipEl.offsetHeight, pad = 14;
    var x = ev.clientX + pad, y = ev.clientY - h - pad;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - pad;   // flip to the left of the pointer
    if (x < 8) x = 8;
    if (y < 8) y = ev.clientY + pad;                               // flip below the pointer
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
  }
  function tipHide() { if (tipEl) tipEl.style.display = "none"; }
  function tipLine(parent, name, value, color) {
    var row = document.createElement("div");
    if (color) {
      var sw = document.createElement("i");
      sw.style.background = color;
      row.appendChild(sw);
    }
    row.appendChild(document.createTextNode(name ? name + ": " : ""));
    var b = document.createElement("b");
    b.textContent = value;
    row.appendChild(b);
    parent.appendChild(row);
  }
  function tipHead(parent, text) {
    var head = document.createElement("div");
    head.className = "tip-head";
    head.textContent = text;
    parent.appendChild(head);
  }
  /* any element with data-tip="text" (bars, waterfall rows) */
  document.addEventListener("pointermove", function (ev) {
    var t = ev.target.closest && ev.target.closest("[data-tip]");
    if (!t) { if (tipEl && tipEl.dataset.owner === "attr") { tipHide(); tipEl.dataset.owner = ""; } return; }
    tipShow(ev, function (el) {
      el.dataset.owner = "attr";
      var parts = t.getAttribute("data-tip").split(" | ");
      tipHead(el, parts[0]);
      for (var i = 1; i < parts.length; i++) tipLine(el, "", parts[i]);
    });
  });

  /* ---- charts ---------------------------------------------------------------- */
  var PALETTE = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];
  var TONES = { good: "var(--good)", warning: "var(--warning)", serious: "var(--serious)", critical: "var(--critical)", neutral: "var(--text-3)" };
  function colorOf(s, i) { return TONES[s.color] || PALETTE[i % PALETTE.length]; }

  function legend(box, series, colors) {
    var lg = document.createElement("div");
    lg.className = "legend";
    series.forEach(function (s, i) {
      var item = document.createElement("span"), sw = document.createElement("i");
      sw.style.background = colors[i];
      item.appendChild(sw);
      item.appendChild(document.createTextNode(s.name));
      lg.appendChild(item);
    });
    box.appendChild(lg);
  }
  /* axis scale: at most 5 gridlines on round values (1, 2, 2.5, 5 x 10^k), never 33.33 */
  function niceScale(v, integer) {
    if (!(v > 0)) return { max: 1, step: 1 };
    var raw = v / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 && !integer ? 2.5 : f <= 5 ? 5 : 10) * p;
    if (integer && step < 1) step = 1;  // counts never get fractional gridlines
    return { max: Math.ceil(v / step - 1e-9) * step, step: step };
  }
  function frame(box, data, peak, H, integer) {
    var W = Math.max(box.clientWidth, 280), L = 56, R = 12, T = 10, B = 24, unit = data.unit || "";
    var scale = data.max ? { max: data.max, step: data.max / 4 } : niceScale(peak, integer);  // fixed 0-100 for percentages
    var max = scale.max;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": box.dataset.title || "chart" });
    svg.style.height = H + "px";
    box.appendChild(svg);
    var y = function (v) { return T + (1 - v / max) * (H - T - B); };
    for (var gv = 0; gv <= max + scale.step / 1000; gv += scale.step) {
      el("line", { class: "grid-line", x1: L, x2: W - R, y1: y(gv), y2: y(gv) }, svg);
      el("text", { class: "axis", x: L - 8, y: y(gv) + 4, "text-anchor": "end" }, svg).textContent = fmt(Math.round(gv * 1000) / 1000, unit);
    }
    return { svg: svg, W: W, H: H, L: L, R: R, T: T, B: B, y: y };
  }
  function xLabels(f, data, x, n, anchorEdges) {
    var span = data.categorical ? 0 : new Date(data.labels[n - 1]) - new Date(data.labels[0]);
    var ticks = Math.min(data.categorical ? n : 6, n), step = Math.max(Math.ceil(n / ticks), 1);
    if (data.categorical && f.W / n < 46) step = Math.ceil(46 / (f.W / n));
    for (var i = 0; i < n; i += step) {
      var anchor = !anchorEdges ? "middle" : i === 0 ? "start" : i + step >= n ? "end" : "middle";
      el("text", { class: "axis", x: x(i), y: f.H - 6, "text-anchor": anchor }, f.svg)
        .textContent = data.categorical ? data.labels[i] : timeLabel(data.labels[i], span > 864e5);
    }
  }
  function headText(data, i) { return data.categorical ? data.labels[i] : timeLabel(data.labels[i], true); }

  function lineChart(box, data) {
    var n = data.labels.length, series = data.series, unit = data.unit || "", max = 0;
    series.forEach(function (s) { s.values.forEach(function (v) { if (v > max) max = v; }); });
    var colors = series.map(colorOf);
    if (series.length > 1) legend(box, series, colors);
    var f = frame(box, data, max, 200, false);
    var x = function (i) { return f.L + (n === 1 ? (f.W - f.L - f.R) / 2 : (i / (n - 1)) * (f.W - f.L - f.R)); };
    xLabels(f, data, x, n, true);
    series.forEach(function (s, si) {
      var d = s.values.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + f.y(v).toFixed(1); }).join(" ");
      if (series.length === 1) {
        el("path", { class: "area", fill: colors[si], d: d + " L" + x(n - 1) + " " + f.y(0) + " L" + x(0) + " " + f.y(0) + " Z" }, f.svg);
      }
      el("path", { class: "line", stroke: colors[si], d: d }, f.svg);
      if (n === 1) el("circle", { r: 4, cx: x(0), cy: f.y(s.values[0]), fill: colors[si] }, f.svg);
    });
    var cross = el("line", { class: "cross", y1: f.T, y2: f.H - f.B, visibility: "hidden" }, f.svg);
    var dots = series.map(function (s, i) { return el("circle", { class: "dot", r: 4.5, fill: colors[i], visibility: "hidden" }, f.svg); });
    // transparent hit area: the whole plot is the hover target, not the 2px line
    var hit = el("rect", { x: f.L, y: f.T, width: f.W - f.L - f.R, height: f.H - f.T - f.B, fill: "transparent" }, f.svg);
    function move(ev) {
      var rect = f.svg.getBoundingClientRect();
      var px = ((ev.clientX - rect.left) / rect.width) * f.W;
      var i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((px - f.L) / (f.W - f.L - f.R)) * (n - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      series.forEach(function (s, si) {
        dots[si].setAttribute("cx", x(i)); dots[si].setAttribute("cy", f.y(s.values[i])); dots[si].setAttribute("visibility", "visible");
      });
      tipShow(ev, function (tip) {
        tip.dataset.owner = "chart";
        tipHead(tip, headText(data, i));
        series.forEach(function (s, si) { tipLine(tip, s.name, fmt(s.values[i], unit), colors[si]); });
      });
    }
    function leave() {
      tipHide(); cross.setAttribute("visibility", "hidden");
      dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", leave);
  }

  /* stacked bars: time buckets or categories (distributions) */
  function barChart(box, data) {
    var n = data.labels.length, series = data.series, unit = data.unit || "", max = 0, totals = [];
    for (var i = 0; i < n; i++) {
      var t = 0;
      series.forEach(function (s) { t += s.values[i] || 0; });
      totals.push(t);
      if (t > max) max = t;
    }
    var colors = series.map(colorOf);
    if (series.length > 1) legend(box, series, colors);
    var f = frame(box, data, max, box.dataset.height ? +box.dataset.height : 200, !unit);
    var band = (f.W - f.L - f.R) / n, bw = Math.max(Math.min(band - 2, 46), 1);
    var x = function (i) { return f.L + band * i + band / 2; };
    xLabels(f, data, x, n, false);
    var marks = [];
    for (i = 0; i < n; i++) {
      var base = 0, col = el("g", {}, f.svg);
      series.forEach(function (s, si) {
        var v = s.values[i] || 0;
        if (!v) return;
        var y1 = f.y(base + v), h = f.y(base) - y1;
        // 1px surface gap between stacked segments
        el("rect", { x: x(i) - bw / 2, y: y1, width: bw, height: Math.max(h - (base ? 1 : 0), 1), rx: 2, fill: colors[si] }, col);
        base += v;
      });
      marks.push(col);
    }
    var hl = el("rect", { class: "band", y: f.T, height: f.H - f.T - f.B, width: band, visibility: "hidden" }, f.svg);
    f.svg.insertBefore(hl, f.svg.firstChild);
    var hit = el("rect", { x: f.L, y: f.T, width: f.W - f.L - f.R, height: f.H - f.T - f.B, fill: "transparent" }, f.svg);
    function move(ev) {
      var rect = f.svg.getBoundingClientRect();
      var px = ((ev.clientX - rect.left) / rect.width) * f.W;
      var i = Math.max(0, Math.min(n - 1, Math.floor((px - f.L) / band)));
      hl.setAttribute("x", f.L + band * i); hl.setAttribute("visibility", "visible");
      tipShow(ev, function (tip) {
        tip.dataset.owner = "chart";
        tipHead(tip, headText(data, i));
        for (var si = series.length - 1; si >= 0; si--) {
          if (series.length === 1 || series[si].values[i]) tipLine(tip, series[si].name, fmt(series[si].values[i] || 0, unit), colors[si]);
        }
        if (series.length > 1) tipLine(tip, "Total", fmt(totals[i], unit));
      });
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", function () { tipHide(); hl.setAttribute("visibility", "hidden"); });
  }

  function chart(box) {
    var data;
    try { data = JSON.parse(document.getElementById(box.dataset.chart).textContent); } catch (e) { return; }
    box.textContent = "";
    var any = data.labels.length && data.series.some(function (s) { return s.values.some(function (v) { return v > 0; }); });
    if (!any) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "No data in this time range";
      box.appendChild(empty);
      return;
    }
    (data.type === "bar" ? barChart : lineChart)(box, data);
  }

  /* relationship graph: layered left-to-right tree */
  function graph(box) {
    var data;
    try { data = JSON.parse(document.getElementById(box.dataset.graph).textContent); } catch (e) { return; }
    if (!data.nodes.length) return;
    var depth = {}, children = {}, hasParent = {};
    data.edges.forEach(function (e) { (children[e[0]] = children[e[0]] || []).push(e[1]); hasParent[e[1]] = true; });
    var queue = data.nodes.filter(function (nd) { return !hasParent[nd.id]; }).map(function (nd) { depth[nd.id] = 0; return nd.id; });
    while (queue.length) {
      var id = queue.shift();
      (children[id] || []).forEach(function (c) { if (depth[c] === undefined) { depth[c] = depth[id] + 1; queue.push(c); } });
    }
    var cols = [];
    data.nodes.forEach(function (nd) { var d = depth[nd.id] || 0; (cols[d] = cols[d] || []).push(nd); });
    var NW = 176, NH = 44, GX = 56, GY = 12;
    var rows = Math.max.apply(null, cols.map(function (c) { return c.length; }));
    var W = cols.length * (NW + GX) - GX + 4, H = rows * (NH + GY) - GY + 4;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Relationship graph" });
    svg.style.maxWidth = W + "px";
    var pos = {};
    cols.forEach(function (col, ci) {
      var offset = ((rows - col.length) * (NH + GY)) / 2;
      col.forEach(function (nd, ri) { pos[nd.id] = { x: 2 + ci * (NW + GX), y: 2 + offset + ri * (NH + GY) }; });
    });
    data.edges.forEach(function (e) {
      var a = pos[e[0]], b = pos[e[1]];
      if (!a || !b) return;
      var x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
      el("path", { class: "edge", d: "M" + x1 + " " + y1 + " C" + mx + " " + y1 + " " + mx + " " + y2 + " " + x2 + " " + y2 }, svg);
    });
    data.nodes.forEach(function (nd) {
      var p = pos[nd.id];
      var parent = nd.href ? el("a", { href: nd.href }, svg) : svg;
      var grp = el("g", { class: "n-" + nd.kind, transform: "translate(" + p.x + "," + p.y + ")" }, parent);
      el("title", {}, grp).textContent = nd.label + (nd.detail ? " — " + nd.detail : "");
      el("rect", { width: NW, height: NH, rx: 6 }, grp);
      el("text", { class: "kind", x: 10, y: 16 }, grp).textContent = nd.kind;
      el("text", { x: 10, y: 33 }, grp).textContent = nd.label.length > 25 ? nd.label.slice(0, 24) + "…" : nd.label;
    });
    box.appendChild(svg);
  }

  /* live refresh: re-fetch this page and swap the results region */
  var timer = null;
  function live(on) {
    clearInterval(timer);
    var btn = document.querySelector("[data-live]");
    if (btn) { btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", on ? "true" : "false"); }
    store("obs-live", on ? "1" : "0");
    if (!on) return;
    timer = setInterval(function () {
      if (document.hidden) return;
      fetch(location.href, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("results");
          var cur = document.getElementById("results");
          if (fresh && cur) { cur.replaceWith(fresh); fresh.querySelectorAll("[data-chart]").forEach(chart); }
        })
        .catch(function () {});
    }, 5000);
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-live]");
    if (b) live(!b.classList.contains("on"));
  });

  /* copy to clipboard: navigator.clipboard needs HTTPS or localhost, so fall back to
     execCommand for intranet sites served over plain HTTP */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject();
    });
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-copy]");
    if (!b) return;
    var src = document.querySelector(b.getAttribute("data-copy"));
    if (!src) return;
    var label = b.dataset.label || (b.dataset.label = b.textContent);
    function done(msg) {
      b.textContent = msg;
      setTimeout(function () { b.textContent = label; }, 1800);
    }
    copyText(src.textContent).then(function () { done("Copied ✓"); }, function () { done("Copy failed — select the text manually"); });
  });

  function init() {
    document.querySelectorAll("[data-chart]").forEach(chart);
    document.querySelectorAll("[data-graph]").forEach(graph);
    document.querySelectorAll("time[datetime]").forEach(function (t) {
      var d = new Date(t.getAttribute("datetime"));
      if (!isNaN(d)) { t.title = d.toISOString(); t.textContent = d.toLocaleString([], { hour12: false }); }
    });
    if (document.querySelector("[data-live]") && store("obs-live") === "1") live(true);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { document.querySelectorAll("[data-chart]").forEach(chart); }, 150);
  });
})();
