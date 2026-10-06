---
name: cinna-tasks
description: Work with Cinna tasks, the shared work items people and other agents see. Report how an assigned task went with tasks_report, add work to your own queue, hand work to another agent and receive its result, comment on a task. Use before the first tasks_* tool call in a session and whenever the turn context names a current task.
when_to_use: The turn context lists a current task or ready tasks; a tool named tasks_* is about to be called; the user asks to delegate, hand over, assign or queue work for an agent; a message starts with "Task result from"; a .cinna/tasks or .cinna/handovers file is involved.
user-invocable: false
version: "1"
compatibility: Cinna Desktop or a cinna-core agent environment that exposes the `cinna` tool server (tasks_* tools). Without that server the file forms in references/file-protocol.md apply.
metadata:
  cinna_protocol: tasks/1
---

# Cinna tasks

A Cinna task is shared work: a person sees it on a board, other agents can be assigned it, its status drives what runs next. Keep your own plan or todo tool for the steps of the current turn; those are not Cinna tasks. Delegated work is not a subagent call: it runs later, in another conversation, and its result comes back to you as a message.

All task tools are on the `cinna` tool server and start with `tasks_`. A `ref` (`t-3f9a2b1c`, `TASK-5`) is always copied from a tool result or the turn context, never constructed.

## Two rules

1. **Report before ending a turn on an assigned task.** When the turn context shows a current task with `report required: yes`, call `tasks_report` with `done` or `failed` and a one-line `summary` before stopping, or `blocked` with a `question` when you cannot continue. Without it the task is left Waiting with "Ended without reporting" and the queue behind it pauses.
2. **Never wait inside a turn.** After `tasks_create` with an `assignee`, end the turn. The result arrives later in this conversation as a message beginning `Task result from <agent> on <ref>:`. Do not poll `tasks_list` or `tasks_get` for it.

## Verbs

| To | Call |
|---|---|
| Say how your task went | `tasks_report {status, summary, question?, body?, artifacts?}` |
| Leave a note or an intermediate finding | `tasks_comment {body, files?}` |
| Put work in your own queue | `tasks_create {title, description, category?: ready}` |
| Hand work to another agent | `tasks_targets`, then `tasks_create {assignee, id, title, description}`, then end the turn |
| Answer a question from work you delegated | `tasks_reply {ref, message}` |
| See what concerns you | `tasks_list {relation?, category?}` |
| Read one task in full | `tasks_get {ref}` |
| Edit fields or move an unstarted task | `tasks_update {ref, …}` |

## Lifecycle

Backlog, Ready, Active, then Waiting or Done. The host moves a task to Active when a turn starts on it. You move it out: `tasks_report done` or `failed` closes it, `blocked` parks it in Waiting with your question, `in_progress` records a progress line. `tasks_update {category}` is a person's move between Backlog, Ready, Done and Cancelled for a task nothing is working on; it is refused on anything with a run, a question or a delegation behind it.

## Delegating

1. Call `tasks_targets`. Use the `target` string exactly as returned; note where it runs and whether it files structured reports.
2. Call `tasks_create` with that `assignee`, an `id` of your choosing (`YYYYMMDD-HHMM-<slug>`), a title, and a `description` that is the whole brief. The executor may not see this conversation. Repeating the same `id` returns the same work.
3. End the turn. A person may approve the run first; `execution: ask` forces that, otherwise the receiving side's setting decides.
4. Read the result message. Answer a `blocked` question addressed to you with `tasks_reply`; a question for the user is theirs.
5. Give several requests one `group` to receive one message when all are over.

Chains stop at depth 2 and at 20 created tasks per conversation. If you delegated onward and must end the turn, report `in_progress` with what you are waiting on; you are not finished until that work returns.

## Comments and reports

`tasks_comment` never changes status: findings, partial results, anything a person on the task page should see. `tasks_report` is your statement on the outcome and is what the requester reads. On a task you were given, a comment is not a report.

## Without a tool server

A bare project folder adopted in Cinna is also a task inbox: `.cinna/tasks/*.md` is its queue, `.cinna/handovers/<id>/brief.md` a request for work. The same verbs exist as files; see [references/file-protocol.md](references/file-protocol.md). Prefer the tools when you have them.
