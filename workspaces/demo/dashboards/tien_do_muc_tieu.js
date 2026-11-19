// Ví dụ widget tuỳ chỉnh bằng code: thanh tiến độ đạt mục tiêu theo cửa hàng.
// ctx = { widget, data: {columns, rows}, datasets, filters, echarts, format }
window.BizagentWidgets = window.BizagentWidgets || {};
window.BizagentWidgets["tien_do_muc_tieu"] = function (el, ctx) {
  const d = ctx.data;
  const i = (c) => d.columns.indexOf(c);
  if (!d.rows.length) { el.textContent = "Chưa có mục tiêu tháng này (chạy bizagent sync)"; return; }
  if (!ctx.echarts) { el.textContent = "Không tải được thư viện biểu đồ"; return; }
  const names = d.rows.map((r) => r[i("cua_hang")]);
  const pct = d.rows.map((r) => r[i("ty_le_dat")]);
  const chart = ctx.echarts.init(el, window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : null);
  chart.setOption({
    backgroundColor: "transparent",
    grid: { left: 8, right: 40, top: 10, bottom: 8, containLabel: true },
    xAxis: { type: "value", max: (v) => Math.max(100, Math.ceil(v.max / 10) * 10), axisLabel: { formatter: "{value}%" } },
    yAxis: { type: "category", data: names },
    tooltip: { trigger: "axis", formatter: (p) => {
      const r = d.rows[p[0].dataIndex];
      return `${r[i("cua_hang")]}<br>Thực tế: ${ctx.format(r[i("thuc_te")], "currency")}<br>Mục tiêu: ${ctx.format(r[i("muc_tieu")], "currency")}`;
    } },
    series: [{
      type: "bar", data: pct.map((v) => ({ value: v, itemStyle: { color: v >= 100 ? "#059669" : v >= 70 ? "#d97706" : "#dc2626" } })),
      label: { show: true, position: "right", formatter: "{c}%" },
      markLine: { symbol: "none", data: [{ xAxis: 100 }], lineStyle: { type: "dashed" } },
    }],
  });
  window.addEventListener("resize", () => chart.resize());
};
