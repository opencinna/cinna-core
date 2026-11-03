# Dashboard starter: web view

An `html` component (`cinna_protocol: dashboards/1`): a page in the host's sandbox
that draws revenue as bars and a line with the dependency-free helper in `chart.js`.
Copy this folder to `dashboards/<name>/` in the agent, rename it, then change it.

- `app.js` shows the four calls a page needs: `cinna.onData`, `cinna.setParams`,
  `cinna.setContext` and `cinna.call` (which resolves to the endpoint's JSON value
  itself and rejects with `{message}`). The host injects `window.cinna` (the kit's
  `protocols/dashboards-bridge.js`); never copy it into the folder. Opened outside a
  host, the page only says where to open it.
- `api/data.py` is the source (Python); `api/week_detail.mjs` is a drill-down (Node,
  built-ins only). Keep one runtime if the agent uses only one; delete the other.
- The sandbox, and why (`check_component.py` flags each):
  - No network: every script, style, font and image is in this folder, because the
    policy allows only the component's own folder.
  - `fetch`/XHR reach only this folder, by relative path; `api/` is never served
    (404), so endpoint data comes through `cinna.onData` and `cinna.call`.
  - Relative paths without a leading slash (`./app.js` or `app.js`): the page lives
    under a per-tile path prefix, so `/app.js` resolves outside it and is blocked.
  - No `<base>` (`base-uri 'none'`), no `<iframe>` (`frame-src 'none'`), no Web
    Workers (`worker-src 'none'`), no form submission (`form-action 'none'`: a
    submitted form replaces the page with an error page).
  - No `eval`, `new Function` or string `setTimeout` (no `'unsafe-eval'`; pick chart
    libraries that do not need it) and no WebAssembly (no `'wasm-unsafe-eval'`).
  - No `localStorage`, `sessionStorage`, IndexedDB or cookies: the origin is opaque,
    so each throws `SecurityError`. Persist state in params (`cinna.setParams`) or
    in `app-data/` through an endpoint.
  - No `window.open` and no top navigation, not even on a click: use
    `cinna.openLink(url)`, which the owner confirms.
  - Inline `<script>` and `<style>` are allowed.
- `api/week_detail.mjs` is pure JS (Node built-ins only): native addons do not load
  under a host's own Node runtime. Use Python when you need a native module.
- Check it before you offer it:
  `uv run python <cinna-dashboards skill>/scripts/check_component.py dashboards/<name> --run`.
- Delete this README once the component is yours.

See the `cinna-dashboards` skill and `guides/14-dashboards.md`.
