// Reads ./data.json (relative to this page) and renders tiles and a table.
// No storage APIs, no workers, no network beyond this folder: the artifact
// sandbox blocks them. State lives in memory.
//
// To use SQLite instead: copy sql-wasm.js and sql-wasm.wasm into this folder,
// add <script src="./sql-wasm.js"></script> before this file, then:
//   const SQL = await initSqlJs({ locateFile: (f) => "./" + f });
//   const buf = await (await fetch("./data.sqlite")).arrayBuffer();
//   const db = new SQL.Database(new Uint8Array(buf));
//   const [result] = db.exec("SELECT ...");
// sql.js runs on the main thread; keep the database under ~20 MB.

async function main() {
  const response = await fetch("./data.json");
  const data = await response.json();

  document.getElementById("title").textContent = data.title;
  document.title = data.title;
  document.getElementById("subtitle").textContent = data.subtitle || "";

  const tiles = document.getElementById("tiles");
  for (const tile of data.tiles || []) {
    const el = document.createElement("div");
    el.className = "tile";
    const label = document.createElement("div");
    label.className = "muted";
    label.textContent = tile.label;
    const value = document.createElement("div");
    value.className = "value";
    value.textContent = tile.value;
    el.append(label, value);
    tiles.append(el);
  }

  const table = document.getElementById("rows");
  const columns = data.columns || [];
  const head = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    head.append(th);
  }
  table.tHead.append(head);
  for (const row of data.rows || []) {
    const tr = document.createElement("tr");
    for (const cell of row) {
      const td = document.createElement("td");
      td.textContent = cell;
      tr.append(td);
    }
    table.tBodies[0].append(tr);
  }
}

main().catch((error) => {
  document.getElementById("subtitle").textContent = "Could not load data.json: " + error;
});
