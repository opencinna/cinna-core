# Dashboard starter: web view

An `html` component (`cinna_protocol: dashboards/1`): a page in the host's sandbox
that draws revenue as bars and a line with the dependency-free helper in `chart.js`.
Copy this folder to `dashboards/<name>/` in the agent, rename it, then change it.

- `app.js` shows the four calls a page needs: `cinna.onData`, `cinna.setParams`,
  `cinna.setContext` and `cinna.call`.
- `api/data.py` is the source (Python); `api/week_detail.mjs` is a drill-down (Node,
  built-ins only). Keep one runtime if the agent uses only one; delete the other.
- No network: every script and style is in this folder, loaded by relative path.
- Check it before you offer it:
  `uv run python <cinna-dashboards skill>/scripts/check_component.py dashboards/<name> --run`.
- Delete this README once the component is yours.

See the `cinna-dashboards` skill and `guides/14-dashboards.md`.
