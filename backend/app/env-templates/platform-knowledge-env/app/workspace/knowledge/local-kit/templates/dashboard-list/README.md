# Dashboard starter: list

A native `list` component (`cinna_protocol: dashboards/1`). Copy this folder to
`dashboards/<name>/` in the agent, rename it, then change `component.json` and
`api/items.py` to what the owner wants to see.

- The endpoint reads `app-data/dashboards/<name>/items.json` when something wrote it,
  and returns sample items with a `notice` until then.
- Check it before you offer it:
  `uv run python <cinna-dashboards skill>/scripts/check_component.py dashboards/<name> --run`.
- Delete this README once the component is yours; it is not read by any host.

See the `cinna-dashboards` skill and `guides/14-dashboards.md`.
