// Dashboard endpoint (Node): the deals behind one week, for `cinna.call("week_detail", {week})`.
// Usage: echo '{"week": "2026-W45"}' | node dashboards/<name>/api/week_detail.mjs
// stdin is the params object; stdout is one JSON value. cwd is the agent folder.
// Built-ins only: a package works only when the agent already installed node_modules.
import { readFileSync, existsSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const component = basename(dirname(dirname(fileURLToPath(import.meta.url))));
const dataFile = join("app-data", "dashboards", component, "deals.json");

let input = "";
for await (const chunk of process.stdin) input += chunk;
const params = JSON.parse(input || "{}");

const deals = existsSync(dataFile)
  ? (JSON.parse(readFileSync(dataFile, "utf8"))[params.week] || [])
  : [
      { id: "d-1", name: "Acme renewal", value: 9800 },
      { id: "d-2", name: "Globex pilot", value: 4200 },
    ];

process.stdout.write(JSON.stringify({ week: params.week || null, deals }));
