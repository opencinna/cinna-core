# My agents

This folder is an agent workshop. Each folder under `Local/` is one self-contained
AI agent: prompts, scripts, configuration and data in a fixed layout.

You are not expected to edit anything here by hand. Open your coding assistant in
this folder and ask it for what you want — it reads `AGENTS.md` and the conventions
in `.cinna-kit/` and does the work.

## What is where

| Folder | What it holds |
|--------|---------------|
| `Local/` | Your agents. One folder each. Everything runs on this machine. |
| `Cloud/` | Empty until you decide to run an agent on the platform 24/7. |
| `.cinna-kit/` | The conventions your assistant follows. Re-downloadable; do not edit. |

## Running an agent

Open your assistant inside `Local/<agent>` and talk to it normally. It reads the
agent's `AGENTS.md`, which points at the agent's real instructions.

Most agents also expose plain commands:

```bash
cd Local/<agent>
make help
```

## Secrets

Credentials live in `Local/<agent>/credentials/.env`, which is git-ignored and never
copied anywhere. Your assistant is instructed never to print their values. If you
ever see a secret echoed back at you, stop and tell it to fix that first.

## Moving an agent to the cloud

Ask your assistant: *"move this agent to the cloud"*. It follows a documented
playbook — you will need a free account on the platform, and the assistant will
tell you exactly which links to open.

## Updating the conventions

Ask your assistant to *"refresh the kit"*, or run:

```bash
uv run .cinna-kit/tools/kit.py refresh
```
