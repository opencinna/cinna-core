Worked playbooks for building agentic networks — packaged into the account workspace as `context/guides/`.

| Guide | Description |
|-------|-------------|
| [build-an-agentic-network.md](build-an-agentic-network.md) | End-to-end walkthrough: create agents, wire capabilities (agent-api + MCP), register a team delegation topology, and verify — all from the CLI. Uses a meeting-booking network as the worked example. |
| [authoring-agent-prompts.md](authoring-agent-prompts.md) | How to author an agent's prompts and description and assign them in one bulk write (`prompts.json` → `cinna api PUT agents/<id>`) that lands in the environment automatically. Covers the six prompt fields, when each fires, the rule that `example_prompts` are user-ready templates rather than a replay of your build data, and the finalize step: rewrite the description to match the finished agent. |
| [building-an-agent-api.md](building-an-agent-api.md) | Producer-side walkthrough for exposing an agent as a capability-narrowed REST API: handler files + `policy.yaml`, enabling the API, the `policy.yaml` guardrail keys, the per-user `scopes:` catalog (both authoring forms + edge enforcement), assigning grants, and verifying the harvested spec — all from the CLI. |
