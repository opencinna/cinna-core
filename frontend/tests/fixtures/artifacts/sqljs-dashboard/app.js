// A real data app: sql.js (SQLite compiled to WebAssembly) on the main thread
// opens the artifact's own data.sqlite and renders a GROUP BY query.
;(async () => {
  const status = document.getElementById("status")
  try {
    const SQL = await initSqlJs({ locateFile: (file) => file })
    const bytes = new Uint8Array(
      await (await fetch("data.sqlite")).arrayBuffer(),
    )
    const db = new SQL.Database(bytes)
    const [result] = db.exec(
      "SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY total DESC",
    )
    const body = document.querySelector("#totals tbody")
    for (const [region, total] of result.values) {
      const row = document.createElement("tr")
      for (const value of [region, total]) {
        const cell = document.createElement("td")
        cell.textContent = String(value)
        row.appendChild(cell)
      }
      body.appendChild(row)
    }
    db.close()
    status.textContent = `${result.values.length} regions`
    document.body.dataset.ready = "true"
  } catch (error) {
    status.textContent = `Failed: ${error}`
    document.body.dataset.ready = "error"
  }
})()
