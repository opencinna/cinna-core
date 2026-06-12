# Local CLI Development

## Purpose

Describes how to develop and test the `cinna` CLI tool against a local instance of the platform backend. During development, `cinna-cli` is not published to PyPI — it lives in a separate local repository and is installed from source. This doc covers how to install the CLI in editable mode and run the live-sync flow end-to-end without relying on the hosted platform.

## Prerequisites

- Platform backend running locally (`http://localhost:8000`)
- Platform frontend running locally (`http://localhost:5173`)
- `cinna-cli` repository cloned locally <!-- nocheck -->
- Mutagen installed locally (`brew install mutagen-io/mutagen/mutagen` on macOS — the CLI verifies the version pinned by `GET /api/v1/cli/agents/{id}/sync-runtime`)
- Docker **not** required on the developer machine — the remote agent environment is the only runtime in live-sync mode

## Setup Flow (Development)

The production flow uses `curl | python3` to bootstrap the CLI. During development you either install the CLI in editable mode and let the normal `cinna setup` flow run, or walk through the underlying steps manually to debug a specific stage.

### 1. Install `cinna` in editable mode

Using `uv` (preferred):

```
uv tool install --force -e /path/to/cinna-cli
```

Using `pip` (fallback):

```
pip install -e /path/to/cinna-cli
```

Both make the `cinna` command available globally and reflect code changes immediately.

### 2. Generate a setup token and run the full bootstrap

Easiest path — mirrors the production flow:

1. Open `http://localhost:5173`, go to an agent's **Integrations** tab, click **Setup**
2. Copy the displayed `curl … | python3 -` command and paste it into the terminal
3. The bootstrap script detects the editable `cinna` install and runs `cinna setup <url>` in the current working directory
4. `cinna setup` exchanges the token, installs/verifies Mutagen, clones the workspace, writes `CLAUDE.md` + `BUILDING_AGENT.md` + companion prompt guides, starts the live sync session, and attaches the foreground TUI
5. Ctrl-C exits the TUI and stops the sync session; run `cinna dev` later to resume

### 3. Manual token exchange (for debugging the bootstrap path)

If you want to skip the bootstrap script and drive the flow by hand:

```
curl -X POST http://localhost:8000/api/v1/cli/setup-tokens \
  -H "Authorization: Bearer <your-platform-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-uuid>"}'
```

Then exchange the setup token via the bare `cinna setup <token>` form (`CINNA_PLATFORM_URL=http://localhost:8000` env var provides the platform URL when only a token is given).

### 4. Everyday commands

Once registered, the standard cinna command set works against the local platform — see the [main feature doc](cinna_cli_integration.md#cli-commands) for the full list. The common loop:

- `cinna dev` — attach the foreground TUI + sync
- `cinna exec python scripts/main.py` — run a command remotely from a second terminal
- `cinna list` — see every agent registered on this machine
- `cinna sync status` / `cinna sync conflicts` — read-only inspection

## Differences from Production

| Aspect | Production | Local Dev |
|--------|-----------|-----------|
| CLI install | `curl \| python3` bootstrap, `uv tool install cinna-cli` from PyPI | `uv tool install --force -e /path/to/cinna-cli` from local repo |
| Platform URL | `https://app.example.com` | `http://localhost:8000` |
| Frontend URL | Same domain as platform | `http://localhost:5173` (captured in `agents.json` so `cinna list` links work) |
| Agent env image | Built and pushed by the platform deploy pipeline | Built locally the first time `make up` brings up the backend stack |
| Sync transport | Mutagen SSH shim → WSS → env-core | Mutagen SSH shim → WS (ws://) → env-core |
| Mutagen daemon | One shared per-user daemon serves all registered agents | Same — the SSH shim reads `~/.cinna/agents.json` to resolve per-agent credentials on each connection |

## Debugging Tips

- **The CLI logs to `cinna.log` inside each agent workspace** — tail it when reproducing sync issues
- **Backend logs**: `docker compose logs -f backend` shows every `/api/v1/cli/...` call including the sync WebSocket open/close events and heartbeat-driven activity-tracker updates
- **Env-core logs**: `docker compose logs -f <agent_env_container>` shows the `mutagen-agent` subprocess output and the `/sync/exec` WS handshakes
- **Stale sync daemon**: if Mutagen is in a weird state, `mutagen daemon stop && mutagen daemon start` resets it without touching `~/.cinna/agents.json`
- **Cache miss after token rotation**: the daemon captures env vars at start. Per-agent credentials come from the JSON registry on every SSH shim invocation, so rotating a token in the platform and refreshing `agents.json` is enough — no daemon restart needed

## Testing Cycle

1. Make changes to `cinna-cli` source code
2. Changes are immediately available (editable install)
3. Run `cinna` commands against the local platform
4. Verify API calls hit `http://localhost:8000` (backend logs)
5. Run the full cinna-cli test suite from the cinna-cli repo: `pytest` (single `pyproject.toml` configures the suite)
6. For end-to-end verification, run the backend CLI tests: `docker compose exec backend python -m pytest tests/api/cli/ -v`

## Troubleshooting

### CLI token expired or revoked

Run the setup curl command again from the Integrations tab — `cinna setup` will re-register the agent and refresh `~/.cinna/agents.json` with a fresh token.

### Backend not recognizing the CLI JWT

Ensure the backend's `SECRET_KEY` in `.env` is stable across restarts — changing it invalidates every existing JWT, including CLI tokens.

### `cinna dev` fails with "unable to receive server magic number: EOF"

The env-core `/sync/exec` WebSocket accepted the handshake but `mutagen-agent` couldn't start. Rebuild the agent env — usually means the Mutagen agent binary wasn't baked into the image or the pinned version drifted between the CLI and the image.

### `cinna sync status` shows `State: missing`

The remote sync root path is wrong — `mutagen sync list` reports the beta endpoint as non-existent. Check `sync_session.py` builds the URL against `/app/workspace` (the container bind-mount), not `/workspace`.

### SGR escape sequences leaking into the TUI

The bootstrap `curl … | python3 -` inherits a non-tty stdin; the child `cinna setup` process then can't enter raw mode. The fix is in the bootstrap script itself — it re-attaches stdin to `/dev/tty` before exec'ing `cinna setup`. If you see this again, confirm the bootstrap script on the backend still contains `_reattach_stdin_to_tty()`.
