# Local CLI Development

## Purpose

Describes how to develop and test the `cinna` CLI tool against a local instance of the platform backend. During development, `cinna-cli` is not published to PyPI — it lives in a separate local repository. This doc covers how to install, configure, and test the CLI locally without the bootstrap script.

## Prerequisites

- Platform backend running locally (`http://localhost:8000`)
- Platform frontend running locally (`http://localhost:5173`)
- `cinna-cli` repository cloned locally (see [reference](../../../) for repo location) <!-- nocheck -->
- Docker installed (for the local agent container)

## Setup Flow (Development)

The production flow uses `curl | python3` to bootstrap everything automatically. During development, you perform each step manually.

### 1. Install cinna-cli in editable mode

Using `uv` (preferred):
```
cd /path/to/cinna-cli
uv tool install -e .
```

Or using `pip` (fallback):
```
cd /path/to/cinna-cli
pip install -e .
```

Both make the `cinna` command available globally and reflect code changes immediately without reinstalling.

### 2. Generate a setup token from the UI

1. Start the backend and frontend locally
2. Open `http://localhost:5173` and navigate to an agent's **Integrations** tab
3. Click **Setup** in the Local Development card
4. The UI shows a `curl` command — ignore the command itself, but note the **token string** (the `tok_...` part in the URL)

Alternatively, generate a setup token via the API directly:

```
curl -X POST http://localhost:8000/api/v1/cli/setup-tokens \
  -H "Authorization: Bearer <your-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-uuid>"}'
```

The response includes `token` and `setup_command` fields.

### 3. Exchange the setup token for a CLI token

```
curl -X POST http://localhost:8000/api/cli-setup/<token> \
  -H "Content-Type: application/json" \
  -d '{"machine_name": "Dev Machine", "machine_info": "darwin/arm64"}'
```

The response contains:
- `cli_token` — JWT for authenticating CLI requests
- `agent` — agent ID, name, environment info
- `platform_url` — backend URL (`http://localhost:8000`)

Save the `cli_token` value — you'll need it for the CLI config.

### 4. Write the CLI config manually

Create the agent workspace directory and config:

```
mkdir -p ~/my-agent/.cinna
```

Write `.cinna/config.json`:

```json
{
  "platform_url": "http://localhost:8000",
  "cli_token": "<jwt-from-step-3>",
  "agent_id": "<agent-uuid>",
  "agent_name": "my-agent",
  "environment_id": "<environment-uuid-or-null>",
  "template": "general-env",
  "container_name": "agent-dev-my-agent",
  "knowledge_sources": []
}
```

### 5. Use cinna commands

Once configured, all `cinna` commands work against the local backend:

- `cinna exec python scripts/main.py` — run script in local container
- `cinna push` — upload workspace to local backend's environment
- `cinna pull` — download workspace + refresh credentials and building context
- `cinna credentials` — re-pull credentials
- `cinna status` — check container and connection status

## Differences from Production

| Aspect | Production | Local Dev |
|--------|-----------|-----------|
| Installation | `curl \| python3` bootstrap from PyPI (`uv tool install cinna-cli`) | `uv tool install -e .` or `pip install -e .` from local repo |
| Setup token exchange | Automatic (bootstrap script) | Manual curl or `cinna setup <token>` |
| Config creation | Automatic | Manual `.cinna/config.json` |
| Platform URL | `https://app.example.com` | `http://localhost:8000` |
| Build context | Downloaded from platform | Same — downloaded from local backend |
| Workspace sync | Same | Same — proxied through local backend to local Docker env |

## Testing Cycle

1. Make changes to `cinna-cli` source code
2. Changes are immediately available (editable install)
3. Run `cinna` commands against the local platform
4. Verify API calls hit `http://localhost:8000` (check backend logs)
5. Test the full flow: setup token generation (UI) → exchange → build context → exec → push/pull

## Troubleshooting

### CLI token expired or revoked
- Generate a new setup token from the UI, exchange it, and update `.cinna/config.json`

### Backend not recognizing CLI JWT
- Ensure the backend's `SECRET_KEY` in `.env` matches between restarts — key changes invalidate all JWTs

### Docker container won't start
- Verify Docker is running: `docker info`
- Check build context was extracted correctly to `.cinna/build/`
- Try rebuilding: `cinna rebuild --no-cache`

### Connection refused on cinna commands
- Confirm backend is running on the expected port
- Check `platform_url` in `.cinna/config.json` matches the backend address
