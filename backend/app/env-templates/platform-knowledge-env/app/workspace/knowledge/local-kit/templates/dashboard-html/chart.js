// A dependency-free SVG chart helper: bars with an optional line on one value axis.
// No library and no network: a web view gets none (dashboards/1, D9).
//
//   CinnaChart.barLine(element, {
//     points: [{ id, label, value }],      // one bar per point
//     line: [number],                       // optional, same length as points
//     selected: id,                         // optional, drawn highlighted
//     onSelect: (point) => {},              // optional, a bar was clicked
//   });
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) node.setAttribute(key, String(attrs[key]));
    if (parent) parent.appendChild(node);
    return node;
  }

  function barLine(container, options) {
    const points = options.points || [];
    const line = options.line || [];
    const width = 600;
    const height = 260;
    const pad = { top: 12, right: 12, bottom: 28, left: 48 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const max = Math.max(1, ...points.map((p) => p.value), ...line);
    const y = (v) => pad.top + plotH - (v / max) * plotH;
    const step = points.length ? plotW / points.length : plotW;

    container.textContent = "";
    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "xMidYMid meet" }, container);

    for (let i = 0; i <= 4; i += 1) {
      const value = (max / 4) * i;
      el("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value) }, svg);
      el("text", { class: "axis", x: pad.left - 6, y: y(value) + 4, "text-anchor": "end" }, svg)
        .textContent = Math.round(value).toLocaleString();
    }

    points.forEach((point, i) => {
      const barW = Math.max(2, step * 0.6);
      const x = pad.left + step * i + (step - barW) / 2;
      const bar = el("rect", {
        class: point.id === options.selected ? "bar selected" : "bar",
        x, y: y(point.value), width: barW, height: Math.max(0, pad.top + plotH - y(point.value)),
      }, svg);
      el("title", {}, bar).textContent = `${point.label}: ${point.value.toLocaleString()}`;
      if (options.onSelect) bar.addEventListener("click", () => options.onSelect(point));
      el("text", { class: "axis", x: x + barW / 2, y: height - 10, "text-anchor": "middle" }, svg)
        .textContent = point.label;
    });

    if (line.length === points.length && line.length > 1) {
      const path = line
        .map((v, i) => `${i ? "L" : "M"}${pad.left + step * i + step / 2},${y(v)}`)
        .join(" ");
      el("path", { class: "line", d: path }, svg);
    }
    return svg;
  }

  window.CinnaChart = { barLine };
})();
