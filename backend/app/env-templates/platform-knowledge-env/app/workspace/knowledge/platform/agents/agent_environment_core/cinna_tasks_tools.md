# Cinna Task Tools (Agent-Env Side)

## Purpose

The eight `tasks_*` tools of the **Cinna Task Protocol (CTP) v1** on the agent environment's MCP server named **`cinna`**. They let an agent report how its task went, keep its own queue, hand work to another agent and receive the result, answer that agent's questions, comment, and read tasks. Available only in **conversation mode** — building mode sessions get no task tools.

The protocol is defined once, in the Local Agent Kit (contract 1.6.0): [`docs/local_agent_kit/protocols/tasks.schema.json`](../../local_agent_kit/protocols/tasks.schema.json) holds the tool names, descriptions and input schemas, the category and report-status words, the result packet and the turn context block; the platform skill [`cinna-tasks`](../../local_agent_kit/skills/cinna-tasks/SKILL.md) teaches it to agents. Cinna Desktop implements the same file, so an agent sees the same vocabulary on either host.

This replaces the former `agent_task` server (`create_task`, `create_subtask`, `add_comment`, `update_status`, `get_details`, `list_tasks`, `handover_report`), which is no longer registered. Its tool names survive only, marked deprecated, in the pre-approval lists so an environment built before the change keeps working until it is rebuilt.

## The tools

All tools act for the calling conversation: every call carries its `source_session_id`, which the backend checks against the environment's owner and agent; every task must belong to that owner. A `ref` is a task short code (`TASK-5`) or UUID, copied from a tool result or the context block. Where `ref` is optional it defaults to the conversation's current task (`Session.source_task_id`).

| Tool | What it does |
|------|--------------|
| `tasks_targets` | Who this conversation may hand work to. When the current task belongs to an agentic team: the team's nodes other than the caller (`node:<uuid>`); otherwise the owner's other **active** agents (`agent:<uuid>`). Each with name and description. |
| `tasks_list` | Tasks that concern the agent, newest first, at most 50. `relation`: `assigned`, `created` (in this agent's conversations), `delegated` (created and assigned to another agent) or `all` (default). `category` filters by category; otherwise closed tasks are hidden unless `include_closed`. |
| `tasks_get` | One task in full (the old details view: description, recent comments, subtasks) plus the protocol summary and the stored structured result. The task's files are uploaded into the caller's workspace under `uploads/task_<SHORT_CODE>/`. |
| `tasks_create` | Without `assignee`: a task in the caller's **own queue** — assigned to the calling agent, `new` (or `open` with `category: ready`), `auto_execute=false`, no session started; a person runs it from the UI. With `assignee`: a delegation that starts at once (`auto_execute=true`, auto-refined, its own session). With `parent`: a subtask (see below). |
| `tasks_update` | Edit `title`, `description`, `priority`, or make a person's `category` move on a task nothing is working on. |
| `tasks_comment` | A comment, optionally with workspace files attached. Never changes status. |
| `tasks_report` | The executor's statement on its task: `in_progress`, `blocked` (with `question`), `done` or `failed`, with `summary`, optional `body` and `artifacts`. |
| `tasks_reply` | Answer work this conversation created: a `blocked` question, or a follow-up message into the executor's conversation. |

### Categories

Agents read and write category words, never raw statuses:

| Category | Statuses |
|----------|----------|
| `backlog` | `new`, `refining` |
| `ready` | `open` |
| `active` | `in_progress` |
| `waiting` | `blocked` |
| `done` | `completed`, `error`, `cancelled` (and `archived`, which `tasks_list` never returns) |

### tasks_create

- **Targets.** `assignee` takes a `tasks_targets` string verbatim (`agent:<uuid>` or `node:<uuid>`). A bare name is still accepted as a fallback, resolved as before (team node name first, then the owner's agent name), but only when exactly one target has that name; an ambiguous name is refused with a pointer to `tasks_targets`. An `agent:` target must be one of the owner's active agents; a `node:` target must be in the current task's team. Delegating to oneself is refused.
- **Idempotency.** On a delegation, `id` is the caller's key: it is stored as `InputTask.external_ref` (`ctp-<uuid5 of session + id>`, so the key is scoped to the conversation). A repeated `tasks_create` with the same `id` returns the existing task with `created: false` and starts nothing; a concurrent duplicate is matched inside the create, and an escaping `IntegrityError` returns the row that won.
- **Ignored with a note.** `execution` and `group` are not supported on cinna-core and are ignored with a `notes` entry; on an own-queue create, `id`, `execution` and `group` are ignored with a note, and on a delegation `category` is ignored with a note.
- **Refused.** `custom_fields` (cinna-core has none), `category: draft` (only for folder targets, which cinna-core has none of), and a title plus description over **10,000 characters** (the refusal says to move long material into a file or a comment).
- **Subtasks.** `parent` must be the current task or one of the caller's own tasks. A delegated subtask needs a member of the parent's team as `assignee`; it goes through `InputTaskService.create_subtask`, which still enforces the team's connection topology. A subtask without `assignee` goes to the caller's own team node (the `own_queue` flag on `AgentSubtaskCreate`, so the team-lead fallback never assigns it to the lead), not started.
- **Own queue.** `own_queue=True` on `AgentTaskCreate` / `AgentSubtaskCreate` marks the create; the source session gets the system message "Task <code> added to <agent>'s queue". The task counts in the context block's `ready tasks` once `open`, and accepts `tasks_report` and `tasks_update`. Because it is agent-created (`agent_initiated`, `source_session_id`), the report rule applies when a person later runs it.
- **Chain limits**, checked after the idempotency check, so a repeated `id` is answered even at a limit:
  - **Depth.** The new task's depth is 1 + the current task's depth (`InputTaskService.chain_depth`: the deeper of `delegation_metadata.depth` and the recorded `input_task.chain_depth`, or, when both are NULL, a walk of at most 5 hops through `parent_task_id` / the creating conversation's task). Above `MAX_CHAIN_DEPTH` = 2 the create is refused, telling the agent to do the work itself or report what it needs. A created task without delegation metadata records its depth in `chain_depth`.
  - **Per conversation.** At most `MAX_TASKS_PER_SESSION` = 20 tasks per source session, counted by `source_session_id`.
  - The context block's `chain depth` comes from the same helper.
- The result is the task summary with `created`, any `notes`, and a `message` (a delegation says: end your turn, the result arrives later as a message here).

### tasks_update

Allowed only on tasks assigned to the agent or created in its conversations. Refused on an `in_progress` or `blocked` task ("work on it is under way … use tasks_report"). `custom_fields` are refused; a `description` is limited to 10,000 characters and may not be empty. A `category` move is a person's move validated by the ordinary transition table: `backlog` → `new`, `ready` → `open`, `done` → `completed`, `cancelled` → `cancelled`. Closing a task that never started (`done` or `cancelled` from `new` or `open`) walks the shortest legal status path one audited step at a time (`InputTaskService.status_path`); any other move is the single allowed transition or a refusal.

### tasks_comment

Allowed only on tasks related to the caller (assigned to it, or created in its conversations) — the same visibility as `tasks_list` and `tasks_update`, not owner-wide. Files are attached through `TaskCommentService.add_comment_from_agent` with the caller as author; the result counts attached and failed files.

### tasks_report

Works on **any task assigned to the calling agent** — a delegation, an own-queue task, a team subtask, a scheduled or triggered run — not only on tasks with `delegation_metadata` (it calls `DelegationService.report` with `require_delegation=False`, keeping the delegation contract's idempotency and status rules). A report on the conversation's own task is bound to its session, so a report from an earlier execution is refused.

- `audience` defaults to `requester` when the task has `delegation_metadata` or was created from another conversation (`source_session_id`), otherwise `user`.
- **Artifacts.** An `http(s)` reference is stored as a link. A working-folder path (a bare string, or a `ref` that is not a URL) cannot be reached by the requester, so it is recorded **by name only** (`{kind: "file", name}`, no `ref`) and the result carries a note to attach the file with `tasks_comment`. A report is never refused over an artifact.

### tasks_reply

Only for a task this conversation created (`task.source_session_id`).

- If its result is a `blocked` question not yet answered (or the same answer is being retried), the reply goes through the delegation-reply path (`DelegationService.reply`, `require_delegation=False`), limited to 10,000 characters; the result says whether it was delivered or is uncertain.
- Otherwise it is a follow-up: the message is sent into the executor's linked conversation (`SessionService.send_session_message`). Its `client_message_id` is derived from the task, the message and the requester's latest user message, so a retry in the same turn is deduplicated while the same words in a later turn are a new message. A task with no conversation yet is refused.

## The report rule

From protocol version 1, agent- or automation-given work must end with `tasks_report`. When an environment's `task_protocol_version` is at least 1 and the task came from `delegation_metadata`, `agent_initiated`, `source_session_id`, `created_by_node_id` or a trigger row (not `auto_execute` alone), a session that would auto-complete the task without a final report leaves it **blocked** with "Ended without reporting". See [Input Tasks — Report rule](../../application/input_tasks/input_tasks.md#report-rule-task-protocol-v1).

The environment advertises its version on every stream's final `done` event (`protocol_version`, from `cinna_tasks_protocol.PROTOCOL_VERSION`); environment creation and rebuild set it as soon as the schema file was installed.

## Prompts

What the prompt carries is data; how to use the tools is the `cinna-tasks` skill's business.

- **Context block.** For a conversation-mode session with a current task, open delegations or ready tasks, `prompt_generator.build_task_context_section()` renders the schema's `context_block`: where the agent runs, the current task (`ref`, title, `report required: yes|no`), chain depth, open delegations (with how many wait on the user), ready tasks, and `tasks skill: cinna-tasks v1`. A subtask received over a team connection adds that connection's prompt under "Why you received this:". The backend supplies the facts as `session_context["task_protocol"]` (`message_service._task_protocol_context`); an older backend that sends only `task_short_code`/`task_title` yields the current-task line with `report required: no`.
- **Report footer.** When the executor's environment is at protocol ≥ 1, `InputTaskService.execute_task()` appends `REPORT_REQUIRED_FOOTER` (call `tasks_report` once, `blocked` with a question if stuck, otherwise "Ended without reporting") to the first message of every task the report rule applies to (`InputTaskService.is_automated()`, no final report yet). A delegation-contract task (`delegation_metadata`) keeps its own footer, `how_to_report(version)`.
- **Configured handovers.** The handover prompt lists each configured target as `agent:<uuid>` with its user-authored trigger — see [Agent Handover](../agent_handover/agent_handover.md).
- **Result packet.** A delegated task's outcome arrives in the requester's conversation as `Task result from <agent> on <ref>:` followed by status, summary, body (cut at 8,000 characters with a pointer to `tasks_get`), artifacts and any question — see [Input Tasks](../../application/input_tasks/input_tasks.md).
- **Platform skill.** `cinna-tasks` is projected into the engine's skill root and listed in the fallback Agent Skills block — see [Agent Skills](../agent_skills/agent_skills.md).

Every one of these is version-gated: an environment at protocol 0 (built before the change, with only the old `agent_task` tools) keeps the legacy handover prompt, the legacy delegated-task footer naming `handover_report`, and the legacy `[Sub-task completed] …` feedback lines.

## Registration

- **Schema at runtime.** `platform_protocol_files.install_platform_protocol()` copies `protocols/tasks.schema.json` and `skills/cinna-tasks/` from the kit snapshot under the platform-knowledge-env template into the environment's core at `/app/core/protocols/` and `/app/core/skills/`, at creation and at every rebuild. The tools read names, descriptions and input schemas from that file (`load_tool_specs()`, with `$ref`s inlined); nothing is hard-coded.
- **Claude Code.** `claude_code_sdk_adapter.py` registers the in-process SDK server `cinna` built by `cinna_tasks_claude.build_cinna_server()` in conversation mode and allows `mcp__cinna__tasks_*`.
- **OpenCode.** `opencode_sdk_adapter.py` adds the stdio bridge under the key `cinna` (`mcp_bridge/cinna_server.py`), so tool names are `cinna_tasks_*`.
- Both forms are in `tool_name_registry.PRE_APPROVED_TOOLS` and the backend's `PRE_ALLOWED_TOOLS`; the old `agent_task` names stay there, deprecated, for one release.
- `GET /api/v1/tasks/delegation-capabilities` returns `protocol_version: 1` and `server: "cinna"`.
- The chat renders `tasks_create` with the handover block and `tasks_report` with the status block (`ToolCallBlock.tsx`, which also still recognises the old names).

## Authentication

Same scoped env token as every agent-env → backend call: `Authorization: Bearer <AGENT_AUTH_TOKEN>` plus `X-Agent-Env-Id`, resolved by `AgentEnvContextDep` to `(environment, agent, owner)`. See [Agent Environment Core](agent_environment_core_tech.md#jwt-authentication-scoped-env-token).

## File References

- **Contract**: `docs/local_agent_kit/protocols/tasks.schema.json`, `docs/local_agent_kit/skills/cinna-tasks/`
- **Env-side tools**:
  - `backend/app/env-templates/app_core_base/core/server/tools/cinna_tasks_protocol.py` — `SERVER_NAME`, `PROTOCOL_VERSION`, `load_tool_specs()`, `CinnaTasksClient` (forwards each call to the backend)
  - `backend/app/env-templates/app_core_base/core/server/tools/cinna_tasks_claude.py` — `build_cinna_server()` (Claude Code)
  - `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/cinna_server.py` — stdio bridge (OpenCode)
  - `backend/app/env-templates/app_core_base/core/server/adapters/tool_name_registry.py` — `CINNA_TASK_TOOLS` and both name forms
  - `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` — `build_task_context_section()`, `build_handover_prompt()`
  - `backend/app/env-templates/app_core_base/core/server/routes.py` — `protocol_version` on the `done` event
- **Install**: `backend/app/services/environments/platform_protocol_files.py`, called from `backend/app/services/environments/environment_lifecycle.py`
- **Backend**:
  - `backend/app/api/routes/task_protocol_api.py` — `GET /api/v1/agent/tasks/protocol/targets`, `GET /api/v1/agent/tasks/protocol/list`, `GET /api/v1/agent/tasks/protocol/get`, `POST /api/v1/agent/tasks/protocol/create`, `POST /api/v1/agent/tasks/protocol/update`, `POST /api/v1/agent/tasks/protocol/comment`, `POST /api/v1/agent/tasks/protocol/report`, `POST /api/v1/agent/tasks/protocol/reply` (registered before `task_agent_api`, whose `{task_id}` patterns would capture them)
  - `backend/app/models/tasks/task_protocol.py` — request models, `PROTOCOL_VERSION`, `SERVER_NAME`
  - `backend/app/services/tasks/task_protocol_service.py` — `TaskProtocolService`
  - `backend/app/api/routes/task_agent_api.py` — the older `/api/v1/agent/tasks/*` routes, kept for environments built before the protocol
- **Tests**: `backend/tests/unit/test_cinna_tasks_parity.py` (registered tools equal the schema), `backend/tests/api/input_tasks/test_task_protocol_api.py`, `test_task_protocol_scenarios.py`, `test_task_protocol_version_gating.py`, `test_task_report_rule.py`

## Known gaps

- The 20-per-conversation cap counts existing rows by `source_session_id`, so a deleted task no longer counts and frees a slot — an accepted approximation.
- The report rule replaces only auto-completion: a session that ends in an **error** still sets the task to `error`, report or not.

## Related Docs

- [Input Tasks](../../application/input_tasks/input_tasks.md) — task lifecycle, the delegation contract, the report rule
- [Agent Handover](../agent_handover/agent_handover.md) — configured handover pairs and their triggers
- [Agent Skills](../agent_skills/agent_skills.md) — the platform skill's projection
- [Agentic Teams](../agentic_teams/agentic_teams.md) — team targets and subtask topology
- [Agent Environment Core](agent_environment_core.md) — parent feature

---

*Aspect of: Agent Environment Core. Related feature: [Input Tasks](../../application/input_tasks/input_tasks.md)*
