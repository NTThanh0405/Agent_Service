/* Dashboard sống: đọc spec + dữ liệu từ server, vẽ bằng ECharts, tự làm mới theo refresh_seconds.
   Widget custom: file dashboards/<ten>.js tự đăng ký
     window.BizagentWidgets = window.BizagentWidgets || {};
     BizagentWidgets["<ten>"] = function (el, ctx) { ... }   // ctx: {widget, data, datasets, filters, echarts, format}
*/
(function () {
  "use strict";
  const slug = document.body.dataset.slug;
  const $ = (id) => document.getElementById(id);
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  let spec = null, timer = null, charts = [], loadedScripts = {};
  const nf = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 });

  function format(v, kind) {
    if (v === null || v === undefined || v === "") return "–";
    if (typeof v !== "number") return String(v);
    if (kind === "percent") return nf.format(v) + "%";
    if (kind === "currency") {
      const a = Math.abs(v);
      if (a >= 1e9) return nf.format(v / 1e9) + " tỷ";
      if (a >= 1e6) return nf.format(v / 1e6) + " tr";
      return nf.format(v) + " ₫";
    }
    return nf.format(v);
  }

  async function api(path) {
    const r = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("auth"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
    return j;
  }

  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") e.textContent = v; else if (k === "class") e.className = v; else e.setAttribute(k, v);
    }
    (children || []).forEach((c) => c && e.appendChild(c));
    return e;
  }

  function col(ds, name) { return ds.columns.indexOf(name); }
  function column(ds, name) { const i = col(ds, name); return i < 0 ? [] : ds.rows.map((r) => r[i]); }

  // ------------------------------------------------------------------ bộ lọc
  function params() { return new URLSearchParams(location.search); }

  function renderFilters(result) {
    const form = $("filters");
    form.innerHTML = "";
    const qs = params();
    for (const [name, f] of Object.entries(spec.filters || {})) {
      const label = el("label", { text: f.label || name });
      const st = result.filters[name] || {};
      let input;
      if (f.type === "select") {
        input = el("select", { name });
        if (f.allow_all) input.appendChild(el("option", { value: "__all__", text: "Tất cả" }));
        (result.options[name] || []).forEach((o) => input.appendChild(el("option", { value: String(o), text: String(o) })));
        input.value = qs.get(name) || (st.value == null ? "__all__" : String(st.value));
      } else {
        input = el("input", { name, type: "text", value: qs.get(name) || st.value || "", placeholder: f.type === "period" ? "vd: tháng này, 7 ngày qua" : "" });
        if (f.type === "period") input.setAttribute("list", "period-presets");
      }
      input.addEventListener("change", () => {
        const q = params(); q.set(name, input.value); history.replaceState(null, "", "?" + q.toString()); load();
      });
      label.appendChild(input);
      form.appendChild(label);
      if (f.type === "period" && st.start) form.appendChild(el("span", { class: "range", text: st.start + " → " + st.end }));
    }
    if (!$("period-presets")) {
      const dl = el("datalist", { id: "period-presets" });
      ["hôm nay", "hôm qua", "tuần này", "tuần trước", "tháng này", "tháng trước", "7 ngày qua", "30 ngày qua", "quý này", "năm nay"]
        .forEach((p) => dl.appendChild(el("option", { value: p })));
      document.body.appendChild(dl);
    }
    form.onsubmit = (e) => { e.preventDefault(); load(); };
  }

  // ------------------------------------------------------------------ widget
  function baseOption() {
    return { backgroundColor: "transparent", tooltip: { trigger: "axis", valueFormatter: (v) => format(v) },
             grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true }, legend: { top: 0, type: "scroll" } };
  }

  function seriesData(w, ds) {
    if (w.series) {               // pivot: mỗi giá trị cột series là một đường/cột
      const xi = col(ds, w.x), si = col(ds, w.series), yi = col(ds, w.y[0]);
      const xs = [...new Set(ds.rows.map((r) => r[xi]))];
      const names = [...new Set(ds.rows.map((r) => r[si]))];
      const map = new Map(); ds.rows.forEach((r) => map.set(r[xi] + "\u0000" + r[si], r[yi]));
      return { xs, series: names.map((n) => ({ name: String(n), data: xs.map((x) => map.get(x + "\u0000" + n) ?? null) })) };
    }
    return { xs: column(ds, w.x), series: w.y.map((y) => ({ name: y, data: column(ds, y) })) };
  }

  function chart(container, option) {
    if (!window.echarts) return false;
    const c = window.echarts.init(container, dark ? "dark" : null);
    c.setOption(option); charts.push(c); return true;
  }

  function renderTable(container, ds, cols, fmtKind) {
    const use = cols && cols.length ? cols : ds.columns;
    const idx = use.map((c) => col(ds, c));
    const thead = el("thead", {}, [el("tr", {}, use.map((c) => el("th", { text: c })))]);
    const tbody = el("tbody");
    ds.rows.forEach((r) => tbody.appendChild(el("tr", {}, idx.map((i) => {
      const v = r[i]; return el("td", { class: typeof v === "number" ? "num" : "", text: typeof v === "number" ? format(v, fmtKind) : (v ?? "") });
    }))));
    container.appendChild(el("div", { class: "tbl" }, [el("table", {}, [thead, tbody])]));
  }

  function renderWidget(w, data, result) {
    const card = el("section", { class: "card " + w.type });
    card.style.gridColumn = "span " + (w.width || 6);
    if (w.title) card.appendChild(el("h2", { text: w.title }));
    const ds = w.dataset ? data[w.dataset] : null;
    if (ds && ds.error) { card.appendChild(el("div", { class: "err", text: ds.error })); return card; }

    if (w.type === "kpi") {
      const row = ds.rows[0] || [];
      const v = row[col(ds, w.value)];
      card.appendChild(el("div", { class: "value", text: format(v, w.format) }));
      if (w.compare) {
        const p = row[col(ds, w.compare)];
        if (typeof v === "number" && typeof p === "number" && p !== 0) {
          const d = (v - p) / Math.abs(p) * 100;
          card.appendChild(el("div", { class: "delta " + (d >= 0 ? "up" : "down"), text: (d >= 0 ? "▲ " : "▼ ") + nf.format(Math.abs(d)) + "% so với kỳ trước (" + format(p, w.format) + ")" }));
        }
      }
    } else if (w.type === "table") {
      renderTable(card, ds, w.columns, w.format);
    } else if (w.type === "custom") {
      const box = el("div", { class: "chart" }); box.style.height = (w.height || 320) + "px"; card.appendChild(box);
      const key = w.script.replace(/\.js$/, "");
      const run = () => {
        const fn = (window.BizagentWidgets || {})[key];
        if (fn) fn(box, { widget: w, data: ds, datasets: data, filters: result.filters, echarts: window.echarts, format });
        else box.textContent = "Script " + w.script + " chưa đăng ký BizagentWidgets[\"" + key + "\"]";
      };
      if (loadedScripts[key]) run();
      else {
        const s = el("script", { src: "/api/v1/dashboards/" + slug + "/script/" + encodeURIComponent(w.script) });
        s.onload = () => { loadedScripts[key] = true; run(); };
        document.head.appendChild(s);
      }
    } else {
      const box = el("div", { class: "chart" }); box.style.height = (w.height || 320) + "px"; card.appendChild(box);
      let option;
      if (w.type === "pie") {
        const names = column(ds, w.x), vals = column(ds, w.y[0]);
        option = { backgroundColor: "transparent", tooltip: { trigger: "item", valueFormatter: (v) => format(v, w.format) },
                   legend: { top: 0, type: "scroll" },
                   series: [{ type: "pie", radius: ["40%", "70%"], top: 30, data: names.map((n, i) => ({ name: String(n), value: vals[i] })) }] };
      } else {
        const { xs, series } = seriesData(w, ds);
        const cat = { type: "category", data: xs }, val = { type: "value", axisLabel: { formatter: (v) => format(v, w.format) } };
        option = Object.assign(baseOption(), {
          xAxis: w.horizontal ? val : cat, yAxis: w.horizontal ? cat : val,
          series: series.map((s) => ({ name: s.name, data: s.data, type: w.type === "area" ? "line" : w.type,
            areaStyle: w.type === "area" ? {} : undefined, smooth: w.type !== "bar", stack: w.stack ? "total" : undefined,
            showSymbol: xs.length < 40 })),
        });
        option.tooltip.valueFormatter = (v) => format(v, w.format);
      }
      if (!chart(box, option)) { box.style.height = "auto"; renderTable(box, ds, [], w.format); }
    }
    const notes = [];
    if (w.note) notes.push(w.note);
    if (ds && ds.truncated) notes.push("Dữ liệu bị cắt bớt (vượt max_rows của dataset)");
    if (ds && ds.synced_at && Object.keys(ds.synced_at).length) notes.push("Đồng bộ: " + Object.values(ds.synced_at).join(", "));
    if (notes.length) card.appendChild(el("div", { class: "meta", text: notes.join(" · ") }));
    return card;
  }

  // ------------------------------------------------------------------ vòng tải
  async function load() {
    clearTimeout(timer);
    try {
      if (!spec) {
        spec = await api("/api/v1/dashboards/" + slug);
        document.title = spec.title;
        $("title").textContent = spec.title;
        $("desc").textContent = spec.description || "";
      }
      const result = await api("/api/v1/dashboards/" + slug + "/data?" + params().toString());
      charts.forEach((c) => c.dispose()); charts = [];
      renderFilters(result);
      const grid = $("grid"); grid.innerHTML = "";
      spec.widgets.forEach((w) => grid.appendChild(renderWidget(w, result.data, result)));
      $("updated").textContent = "Cập nhật " + new Date(result.generated_at).toLocaleTimeString("vi-VN");
      $("error").hidden = true;
    } catch (e) {
      if (e.message !== "auth") { $("error").textContent = "Lỗi: " + e.message; $("error").hidden = false; }
    }
    if (spec) timer = setTimeout(() => { if (!document.hidden) load(); else document.addEventListener("visibilitychange", load, { once: true }); }, spec.refresh_seconds * 1000);
  }

  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
  $("refresh").addEventListener("click", load);
  window.addEventListener("DOMContentLoaded", load);
})();
