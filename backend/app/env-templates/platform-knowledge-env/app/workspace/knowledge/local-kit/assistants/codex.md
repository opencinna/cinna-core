# Codex and other sandboxed CLI assistants

Notes for assistants that run in a restricted sandbox — limited or no network,
approval-gated writes, a working directory they must not leave. Everything in
`START.md`, the kit `README.md` and the guides applies unchanged.

## Instructions file

`AGENTS.md` is the file to read: at the workshop root for the orchestrator role, and
inside `Local/<slug>` for that agent. The root also has a `CLAUDE.md`; it only
imports `AGENTS.md`, so you can ignore it.

## Network: download once, work offline

Assume the network is unavailable, and get everything in one request at the start:

```bash
cd ~/Documents/MyAgents
curl -sL {{KIT_BASE_URL}}/kit.tar.gz | tar xz
rm -rf .cinna-kit && mv cinna-kit .cinna-kit
```

After that the entire kit — all twelve guides, the schema, the templates and the
tool — is on disk and needs no further requests. Read from `.cinna-kit/`, never from
the network.

If you cannot run network commands at all, ask the user to run those three lines and
tell you when they are done. Do **not** fall back to fetching guides one at a time:
the instance rate-limits per IP and thirty requests will be throttled where one
tarball would not.

The single-file fallback exists for genuine one-offs: `{{KIT_BASE_URL}}/kit/<path>`,
for example `{{KIT_BASE_URL}}/kit/guides/04-credentials.md`.

## Dependencies

`uv` handles Python environments and is required for the local build loop. If it is
missing and you cannot install it, say so plainly and stop rather than falling back
to a system `python` with hand-installed packages — the resulting agent will not
match the cloud runtime.

The two shipped helper scripts (`scripts/cinna_credentials.py` and
`scripts/update_status.py`) and the kit tool `.cinna-kit/tools/kit.py` are all
standard-library-only on purpose — no packages, no network. They do need Python
3.10+, which the macOS system `python3` (3.9) is not, so run them through
`uv run …`: the kit tool carries inline script metadata and uv provisions the
interpreter automatically.

## Approvals

Group file writes so the user approves a coherent change rather than twenty
fragments. State what you are about to do in one line before asking.

Commands worth pre-approving with the user, if your harness supports it:
`uv run`, `make`, `uv run .cinna-kit/tools/kit.py`. Keep `curl`, `git push` and
`cinna` as explicit per-use decisions.

## Working directory

Work from the agent root (`Local/<slug>`) and use relative paths everywhere. This is
not just a sandbox constraint: in the cloud the agent runs with the workspace root as
its working directory, so absolute paths break at import. See
`guides/03-scripts-and-data.md`.

## Secrets

Never open `credentials/.env` or `credentials/credentials.json`, and never echo their
contents. Use `.env.example` when you need to know the shape. If a command's output
would contain a secret, do not run it.

## The cloud step

`guides/11-go-cloud.md` needs real network access and a browser for the login
consent. If you cannot do either, prepare everything up to and including
`kit.py validate`, then hand the user the exact command list to run themselves.
