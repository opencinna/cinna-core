@AGENTS.md

## Claude Code specifics

- Kit conventions for Claude Code — permissions, `AskUserQuestion` habits, the
  `CLAUDE.md` / `AGENTS.md` split — are in `.cinna-kit/assistants/claude-code.md`.
- **The nearest instruction file wins, per subfolder.** Each agent folder under
  `Local/` has its own `CLAUDE.md` and `AGENTS.md`; working inside one, that pair
  wins over this one. Each account workspace under `Cloud/<host>/` has a `CLAUDE.md`
  the cinna-cli generated; working inside one, **that** file wins — it describes the
  cloud workflow, which is not this one. This file governs the workshop root only.
