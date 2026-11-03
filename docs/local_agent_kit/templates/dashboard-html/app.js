// The page side of a dashboards/1 web view. The host injects `window.cinna`:
//   await cinna.ready            -> { component, title, params, theme, size }
//   cinna.onData(cb)             -> cb(data, { stale, fetched_at }); cached first, then fresh
//   cinna.setParams(params)      -> stored on the tile; the source recomputes, arrives via onData
//   cinna.setContext({ summary, selection, visible }) -> what the owner's chat sees; last call wins
//   await cinna.call(endpoint, params) -> one of this component's own endpoints (drill-downs)
// Relative paths only, every library in this folder, no network.
(function () {
  const chart = document.getElementById("chart");
  const status = document.getElementById("status");
  const month = document.getElementById("month");
  const detail = document.getElementById("detail");
  const detailTitle = document.getElementById("detail-title");
  const detailList = document.getElementById("detail-list");

  let weeks = [];
  let selected = null;

  if (!window.cinna) {
    status.textContent = "Open this component in Cinna Desktop to see its data.";
    return;
  }
  const cinna = window.cinna;

  function tellContext() {
    const total = weeks.reduce((sum, w) => sum + w.value, 0);
    cinna.setContext({
      summary: `Revenue for ${month.value}: ${total.toLocaleString()} EUR over ${weeks.length} weeks.` +
        (selected ? ` Selected week ${selected}.` : ""),
      selection: selected ? [selected] : [],
      visible: weeks.map((w) => w.id),
    });
  }

  function draw() {
    CinnaChart.barLine(chart, {
      points: weeks,
      line: weeks.map((_, i) => weeks.slice(0, i + 1).reduce((s, w) => s + w.value, 0) / (i + 1)),
      selected,
      onSelect: selectWeek,
    });
  }

  async function selectWeek(week) {
    selected = week.id;
    draw();
    tellContext();
    detailTitle.textContent = `Deals in ${week.label}`;
    detailList.textContent = "";
    detail.hidden = false;
    try {
      const result = await cinna.call("week_detail", { week: week.id });
      for (const deal of result.deals || []) {
        const li = document.createElement("li");
        li.textContent = `${deal.name}: ${deal.value.toLocaleString()} EUR`;
        detailList.appendChild(li);
      }
    } catch (error) {
      detailList.textContent = `Could not load the week: ${error.message || error}`;
    }
  }

  cinna.onData((data, meta) => {
    weeks = data.weeks || [];
    if (data.month) month.value = data.month;
    status.textContent = meta && meta.stale ? "Updating…" : data.notice || "";
    if (!weeks.some((w) => w.id === selected)) selected = null;
    draw();
    tellContext();
  });

  month.addEventListener("change", () => {
    selected = null;
    detail.hidden = true;
    cinna.setParams({ month: month.value });
  });

  cinna.ready.then((init) => {
    if (init && init.params && init.params.month) month.value = init.params.month;
  });
})();
