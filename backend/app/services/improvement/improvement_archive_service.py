"""ImprovementArchiveService — build the downloadable ZIP, in memory.

The archive is a **pure function of stored data**: ``(snapshot, context, the
request row, the requester/target projections)``. Nothing here reads the source
``Session`` — that is the no-live-read-through invariant (plan §2.2). Because it
is pure and the snapshot is 2 MB-capped, the result is not cached; on-demand
generation is cheap.

**No filesystem writes anywhere.** The ZIP is assembled in a ``BytesIO``. That
is a deliberate design choice: a new write path would need a docker-compose
volume mount to survive deploys, which is exactly the "silently unmounted write
path" class of defect this feature avoids by construction (plan §11).

Layout::

    improvement-<short-id>.zip
    ├── README.md
    ├── metadata.json
    ├── context.json
    ├── prompts/
    │   ├── README.md
    │   ├── WORKFLOW_PROMPT.md
    │   ├── ENTRYPOINT_PROMPT.md
    │   ├── REFINER_PROMPT.md
    │   └── ROUTER_TRIGGER_PROMPT.md
    ├── memory/                       (only when memory was captured)
    │   └── <the install's app-data/memory/*.md>
    └── session/
        ├── messages.md
        └── messages.json

``prompts/`` and ``memory/`` are what make the archive *reproducible* rather
than merely descriptive: they are the two halves of the system prompt the run
actually used. For a bundle install the publisher has never seen either — the
consumer's prompt edits live in their own install, and the memory area is
excluded from bundle snapshots by design — so without them a publisher debugs a
prompt that was not the one running.
"""
import json
import logging
import zipfile
from io import BytesIO
from typing import Any

from app.models.improvement.agent_improvement_request import (
    AgentImprovementRequest,
)

logger = logging.getLogger(__name__)

ARCHIVE_SCHEMA_VERSION = 2

# Fixed ZipInfo timestamp so the same row always produces byte-identical output
# (the real timestamps live in metadata.json / the snapshot).
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_NO_COMMENT = "_No comment provided._"

# ``context.prompts`` field key → archive member filename. The filenames match
# the workspace documents these prompts are synced from, so a publisher can
# diff a member straight against ``docs/`` in their own install.
_PROMPT_MEMBER_NAMES: dict[str, str] = {
    "workflow": "WORKFLOW_PROMPT.md",
    "entrypoint": "ENTRYPOINT_PROMPT.md",
    "refiner": "REFINER_PROMPT.md",
    "router_trigger": "ROUTER_TRIGGER_PROMPT.md",
}

# Human copy for ``context.memory.unavailable_reason``. Mirrors the
# ``MEMORY_REASON_*`` vocabulary in ``SessionSnapshotService`` — the archive
# must explain an absent block, because "no memory section" and "memory was
# empty" lead a reader to very different conclusions.
_MEMORY_REASON_COPY: dict[str, str] = {
    "declined_by_requester": (
        "The requester chose not to include their personal memory notes."
    ),
    "no_environment": (
        "The install had no environment at capture time, so there was nothing "
        "to read."
    ),
    "env_not_running": (
        "The environment was not running at capture time. Submitting a report "
        "deliberately does not start a container, so the memory area could not "
        "be read."
    ),
    "read_failed": (
        "The memory area could not be read from the container. Assume memory "
        "content may still have been in play."
    ),
    "empty": "The install had no personal memory notes.",
}
_SNAPSHOT_UNAVAILABLE = (
    "The frozen transcript for this request is unavailable — the stored "
    "snapshot is empty. The request metadata and runtime context below are "
    "still accurate."
)


def archive_filename(request: AgentImprovementRequest) -> str:
    """Server-derived archive filename. Never built from user input (plan §4.5)."""
    return f"improvement-{str(request.id)[:8]}.zip"


class ImprovementArchiveService:
    """Renders an improvement request into a self-describing ZIP."""

    @staticmethod
    def build(
        request: AgentImprovementRequest,
        requester_projection: dict,
        target_projection: dict,
    ) -> bytes:
        """Build the archive bytes.

        Args:
            request: the frozen row.
            requester_projection: ``{"display": str | None, "email": str | None}``.
            target_projection: ``{"agent_name": str | None,
                "owner_display": str | None, "session_title": str | None}``.

        Returns:
            The complete ZIP as bytes.
        """
        snapshot = request.snapshot or {}
        context = request.context or {}

        members = {
            "README.md": ImprovementArchiveService.render_readme(
                request, requester_projection, target_projection
            ),
            "metadata.json": _dump_json(
                ImprovementArchiveService._metadata(
                    request, requester_projection, target_projection
                )
            ),
            "context.json": _dump_json(context),
            "session/messages.json": _dump_json(snapshot),
            "session/messages.md": ImprovementArchiveService.render_transcript(
                snapshot
            ),
            **ImprovementArchiveService._prompt_members(context),
            **ImprovementArchiveService._memory_members(context),
        }

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        return buffer.getvalue()

    # ── Members ──────────────────────────────────────────────────────

    @staticmethod
    def _metadata(
        request: AgentImprovementRequest,
        requester_projection: dict,
        target_projection: dict,
    ) -> dict:
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "request_id": str(request.id),
            "status": request.status,
            "source": request.source,
            "comment": request.comment,
            "resolution_note": request.resolution_note,
            "created_at": _iso(request.created_at),
            "status_changed_at": _iso(request.status_changed_at),
            "target_agent_id": str(request.target_agent_id),
            "target_agent_name": target_projection.get("agent_name"),
            "source_agent_id": (
                str(request.source_agent_id) if request.source_agent_id else None
            ),
            "bundle_uuid": str(request.bundle_uuid) if request.bundle_uuid else None,
            "session_id": str(request.session_id) if request.session_id else None,
            "session_title": target_projection.get("session_title"),
            "requester_display": requester_projection.get("display"),
            "requester_email": requester_projection.get("email"),
            "snapshot_message_count": request.snapshot_message_count,
            "snapshot_truncated": request.snapshot_truncated,
        }

    @staticmethod
    def _readme_prompts_section(context: dict) -> list[str]:
        """The README's "Prompts and memory" section.

        Answers the one question the runtime-context table cannot: *what was
        the system prompt for this run*. It is assembled from two places — the
        prompt documents (database-backed, and possibly edited by the consumer
        after install) and the personal memory area (container-only, never
        synced anywhere) — so a publisher reading their own install would get
        neither right.
        """
        prompts = context.get("prompts") or {}
        memory = context.get("memory") or {}
        lines = ["## Prompts and memory", ""]

        if not prompts:
            lines += [
                "This request was captured before prompt and memory capture "
                "existed, so neither is available for it.",
                "",
            ]
            return lines

        diverged = prompts.get("diverged")
        if diverged is True:
            changed = [
                _PROMPT_MEMBER_NAMES[key]
                for key in _PROMPT_MEMBER_NAMES
                if (prompts.get(key) or {}).get("diverged_from_installed_revision")
            ]
            lines += [
                "> **The prompts on this install differ from the revision it "
                f"was installed from:** {', '.join(f'`{c}`' for c in changed)}. "
                "Reproduce the report against the text in `prompts/`, not "
                "against your own install.",
                "",
            ]
        elif diverged is False:
            lines += [
                "The prompts on this install match the bundle revision it was "
                "installed from — what you published is what ran.",
                "",
            ]
        else:
            lines += [
                "There is no bundle revision behind this install, so the "
                "prompts in `prompts/` are simply the ones that ran.",
                "",
            ]

        if memory.get("available"):
            lines += [
                f"The install also had {memory.get('file_count', 0)} personal "
                "memory file(s) "
                f"({memory.get('total_chars', 0)} characters), injected into "
                "every system prompt. They are in `memory/`.",
                "",
            ]
        else:
            reason = _MEMORY_REASON_COPY.get(
                memory.get("unavailable_reason") or "",
                "No personal memory notes were captured.",
            )
            lines += [f"**Personal memory:** {reason}", ""]
        return lines

    # ── Prompts + memory members ─────────────────────────────────────

    @staticmethod
    def _prompt_members(context: dict) -> dict[str, str]:
        """Write each captured prompt document as its own file.

        Named after the workspace documents they mirror
        (``docs/WORKFLOW_PROMPT.md`` and friends) so a publisher can diff a
        member straight against the file in their own install. A field with no
        text at all is skipped rather than written empty — an empty
        ``REFINER_PROMPT.md`` would read as "the consumer blanked it" when the
        truth is "this agent never had one".
        """
        prompts = context.get("prompts") or {}
        members: dict[str, str] = {}
        for key, filename in _PROMPT_MEMBER_NAMES.items():
            field = prompts.get(key) or {}
            text = field.get("text")
            if not text:
                continue
            members[f"prompts/{filename}"] = _ensure_trailing_newline(text)
        if not members:
            return {}
        members["prompts/README.md"] = ImprovementArchiveService.render_prompts_readme(
            prompts
        )
        return members

    @staticmethod
    def render_prompts_readme(prompts: dict) -> str:
        """The divergence table — the reason the prompt files are here.

        For a bundle install the interesting question is not "what does the
        prompt say" but "is this still my text". That is answered per field
        against the installed revision, and stated as *unknown* rather than
        *no* when there was no baseline to compare against.
        """
        baseline = prompts.get("baseline")
        lines = [
            "# Prompts as they ran",
            "",
            "The agent's prompt documents, captured from the **source install** "
            "at the moment the request was submitted.",
            "",
        ]
        if baseline == "installed_revision":
            version = prompts.get("baseline_version")
            lines += [
                "Divergence below is measured against the bundle revision this "
                f"install was materialised from{f' (v{version})' if version else ''}. "
                "A diverged field means the person running the agent is **not** "
                "running your published text — fix the reported behaviour against "
                "what is in this folder, not against your own install.",
                "",
            ]
        else:
            lines += [
                "This install has no bundle revision behind it, so there is no "
                "baseline to diff against and divergence is reported as unknown.",
                "",
            ]

        lines += ["| Document | Characters | Diverged from installed revision | Last changed |",
                  "| --- | --- | --- | --- |"]
        for key, filename in _PROMPT_MEMBER_NAMES.items():
            field = prompts.get(key) or {}
            diverged = field.get("diverged_from_installed_revision")
            # A field with no text has no file in this folder — say so in the
            # table rather than leaving the reader to notice the absence.
            label = f"`{filename}`" if field.get("text") else f"`{filename}` (not set)"
            lines.append(
                f"| {label} | {field.get('chars', 0)} | "
                f"{'unknown' if diverged is None else _yes_no(diverged)} | "
                f"{field.get('updated_at') or '—'} |"
            )
        lines.append("")

        allowed = prompts.get("allowed_tools") or []
        sdk_tools = prompts.get("sdk_tools") or []
        lines += [
            "## Tool configuration",
            "",
            f"- **Tools requested by the agent:** {_join_or_dash(sdk_tools)}",
            f"- **Tools auto-approved by the owner:** {_join_or_dash(allowed)}",
            "",
            "A tool that appears in the first list but not the second prompted "
            "the user for permission on every use — a common cause of a run "
            "that looks stuck.",
            "",
        ]
        if any((prompts.get(key) or {}).get("truncated") for key in _PROMPT_MEMBER_NAMES):
            lines += [
                "> **Note.** At least one document exceeded the capture cap and "
                "was tail-truncated.",
                "",
            ]
        lines += [
            "> The `sha256` recorded in `context.json` is of the document **as "
            "the agent ran it**, before secret masking. A file here that "
            "contains `***REDACTED***` will not re-hash to it.",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _memory_members(context: dict) -> dict[str, str]:
        """Write the captured personal-memory files, one per member.

        Filenames were sanitised to bare basenames at capture time
        (``_safe_member_name``), so nothing here can write outside ``memory/``
        when the recipient extracts the archive. Collisions after sanitising
        are still possible, so they are disambiguated rather than overwritten.
        """
        memory = context.get("memory") or {}
        files = memory.get("files") or []
        if not memory.get("available") or not files:
            return {}

        members: dict[str, str] = {
            "memory/README.md": ImprovementArchiveService.render_memory_readme(memory)
        }
        # Seeded with the folder's own README so a memory file that happens to
        # be called README.md is renamed rather than silently replacing it.
        seen: set[str] = {"README.md"}
        for index, entry in enumerate(files):
            name = str(entry.get("filename") or f"memory-{index + 1}.md")
            name = name.replace("/", "_").replace("\\", "_") or f"memory-{index + 1}.md"
            candidate = name
            suffix = 2
            while candidate in seen:
                candidate = f"{suffix}-{name}"
                suffix += 1
            seen.add(candidate)
            members[f"memory/{candidate}"] = _ensure_trailing_newline(
                str(entry.get("text") or "")
            )
        return members

    @staticmethod
    def render_memory_readme(memory: dict) -> str:
        """Front matter for ``memory/`` — what it is and how to treat it."""
        lines = [
            "# Personal memory as it ran",
            "",
            "These files are the source install's `app-data/memory/*.md`. The "
            "runtime reads them fresh on every request and injects them into "
            "the system prompt under a `## Personalization / User Memory` "
            "heading, in the filename order below — so they are part of the "
            "prompt the agent actually saw, and behaviour that looks "
            "inexplicable from the prompts alone is often explained here.",
            "",
            f"Captured at {memory.get('captured_at')} — "
            f"{memory.get('file_count', 0)} file(s), "
            f"{memory.get('total_chars', 0)} characters.",
            "",
        ]
        if memory.get("truncated"):
            lines += [
                "> **Truncated.** The memory area exceeded the capture cap; the "
                "tail was dropped.",
                "",
            ]
        lines += [
            "This is the requester's **personal** content — how they want to be "
            "addressed, their defaults, small facts about them. They chose to "
            "include it. Read it to understand the run and nothing else: do not "
            "copy it into your own agent's workspace, do not commit it, and "
            "delete your local copy when you are done.",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def render_transcript(snapshot: dict) -> str:
        """Human-readable transcript rendered from the frozen snapshot."""
        messages = snapshot.get("messages") or []
        if not messages:
            return f"# Session transcript\n\n{_SNAPSHOT_UNAVAILABLE}\n"

        session_block = snapshot.get("session") or {}
        lines = [
            f"# Session transcript — {session_block.get('title') or 'Untitled session'}",
            "",
            f"Captured at {snapshot.get('captured_at')}. "
            f"{len(messages)} of {session_block.get('total_message_count', len(messages))} "
            "message(s) included.",
            "",
        ]
        if snapshot.get("truncated"):
            lines += [
                f"> **Truncated.** {snapshot.get('omitted_message_count', 0)} older "
                "message(s) were dropped to fit the snapshot size cap.",
                "",
            ]

        for message in messages:
            role = str(message.get("role", "unknown")).capitalize()
            header = f"## {role} · #{message.get('sequence_number')}"
            if message.get("command_name"):
                header += f" · `{message['command_name']}`"
            lines += [header, "", f"_{message.get('timestamp')}_", ""]
            lines += [message.get("content") or "_(empty)_", ""]

            attachments = message.get("attachments") or []
            if attachments:
                lines.append("**Attachments** (descriptors only, no file contents):")
                lines += [
                    f"- `{a.get('filename')}` — {a.get('mime_type')}, "
                    f"{a.get('size')} bytes"
                    for a in attachments
                ]
                lines.append("")

            digest = message.get("tool_digest") or []
            if digest:
                lines.append("<details><summary>Tool activity</summary>")
                lines.append("")
                lines += [
                    f"- `{d.get('type')}` **{d.get('tool_name') or '—'}** "
                    f"(seq {d.get('seq')}): {_one_line(d.get('brief'))}"
                    for d in digest
                ]
                lines += ["", "</details>", ""]

        return "\n".join(lines) + "\n"

    # ── README ───────────────────────────────────────────────────────

    @staticmethod
    def render_readme(
        request: AgentImprovementRequest,
        requester_projection: dict,
        target_projection: dict,
    ) -> str:
        """Render the archive's front page (plan §5.4, sections 1–7)."""
        context = request.context or {}
        agent_ctx = context.get("agent") or {}
        env_ctx = context.get("environment") or {}
        sdk_ctx = context.get("sdk") or {}
        plugins = context.get("plugins") or []
        agent_name = (
            target_projection.get("agent_name") or agent_ctx.get("name") or "this agent"
        )

        lines: list[str] = [f"# Improvement request for {agent_name}", ""]

        # 2. What was reported
        lines += ["## What was reported", ""]
        comment = (request.comment or "").strip()
        lines += [comment if comment else _NO_COMMENT, ""]

        # 3. Who and when
        lines += ["## Who and when", ""]
        lines += [
            f"- **Submitted by:** {requester_projection.get('display') or 'Unknown'}"
            + (
                f" ({requester_projection['email']})"
                if requester_projection.get("email")
                else ""
            ),
            f"- **Submitted at:** {_iso(request.created_at)}",
            f"- **Request id:** `{request.id}`",
            f"- **Status:** {request.status}",
            f"- **Submitted from:** {request.source}",
            "",
        ]

        # 4. Which agent
        lines += ["## Which agent", ""]
        lines.append(f"- **Agent:** {agent_name}")
        if agent_ctx.get("is_bundle_install"):
            lines += [
                f"- **Bundle id:** `{agent_ctx.get('bundle_id') or '—'}`",
                f"- **Installed version:** {agent_ctx.get('installed_version') or '—'} "
                f"(revision {agent_ctx.get('installed_revision_number') or '—'})",
                f"- **Latest published version:** "
                f"{agent_ctx.get('latest_version') or '—'} "
                f"(revision {agent_ctx.get('latest_revision_number') or '—'})",
                f"- **Update pending at capture:** "
                f"{_yes_no(agent_ctx.get('update_pending'))}",
                f"- **Publisher install:** "
                f"{_yes_no(agent_ctx.get('is_publisher_install'))}",
            ]
        else:
            lines.append("- **Bundle:** not a bundle install (standalone agent)")
        lines.append("")

        # 5. Runtime context
        lines += ["## Runtime context", "", "| Field | Value |", "| --- | --- |"]
        lines += [
            _row("Session mode", sdk_ctx.get("session_mode")),
            _row("SDK engine", sdk_ctx.get("effective_engine")),
            _row("Effective model", sdk_ctx.get("effective_model")),
            _row("Model override (conversation)", sdk_ctx.get("model_override_conversation")),
            _row("Model override (building)", sdk_ctx.get("model_override_building")),
            _row("Environment", env_ctx.get("env_name")),
            _row("Environment version", env_ctx.get("env_version")),
            _row("Instance", env_ctx.get("instance_name")),
            _row("Image tag", env_ctx.get("current_image_tag")),
            _row("Expected image tag", env_ctx.get("expected_image_tag")),
            _row("Image stale", env_ctx.get("image_stale")),
            _row("Environment status at capture", env_ctx.get("status_at_capture")),
            _row("Critical state", env_ctx.get("critical_state")),
            _row("Critical cause", env_ctx.get("critical_cause")),
            _row(
                "Plugins",
                ", ".join(
                    f"{p.get('name')} ({p.get('source')})" for p in plugins
                )
                if plugins
                else None,
            ),
            "",
        ]

        # 6. The prompt + memory summary — the "what was the system prompt"
        # answer, stated up front because it is the first thing a publisher
        # needs and the one thing they cannot get from their own install.
        lines += ImprovementArchiveService._readme_prompts_section(context)

        # 7. What is in this archive
        prompts_ctx = context.get("prompts") or {}
        memory_ctx = context.get("memory") or {}
        lines += [
            "## What is in this archive",
            "",
            "- `README.md` — this file.",
            "- `metadata.json` — the request row: ids, status, timestamps, requester.",
            "- `context.json` — the full frozen runtime context (agent, bundle, "
            "environment, SDK, plugins, prompts, memory, recipient).",
            "- `session/messages.md` — the conversation, human-readable.",
            "- `session/messages.json` — the same transcript as structured data, "
            "including the per-message tool digest.",
        ]
        if any(
            (prompts_ctx.get(key) or {}).get("text") for key in _PROMPT_MEMBER_NAMES
        ):
            lines.append(
                "- `prompts/` — the agent's prompt documents as they ran, with a "
                "per-document divergence table in `prompts/README.md`."
            )
        if memory_ctx.get("available"):
            lines.append(
                "- `memory/` — the install's personal memory files, which the "
                "runtime injects into every system prompt."
            )
        lines += [
            "",
            "Deliberately **not** included:",
            "",
            "- Container logs — they are per-environment and would leak the "
            "requester's other sessions.",
            "- Uploaded file contents — attachments appear as descriptors "
            "(filename, type, size) only.",
            "- Workspace files and scripts — only the prompt documents above.",
            "- Credential values — the transcript, the prompts and the memory "
            "files were all passed through a secret scrubber before they were "
            "stored; matches read `***REDACTED***`.",
            "",
        ]
        if request.snapshot_truncated:
            lines += [
                "> **Truncated snapshot.** The conversation exceeded the size cap, "
                "so the oldest messages were dropped. The most recent messages — "
                "where the reported defect usually is — are all present.",
                "",
            ]
        if not (request.snapshot or {}).get("messages"):
            lines += [f"> **Note.** {_SNAPSHOT_UNAVAILABLE}", ""]

        # 8. How to act on this
        lines += [
            "## How to act on this",
            "",
            "Read `context/guides/handling-improvement-requests.md` in your account "
            "CLI workspace — it walks through triage, ownership, and how much to "
            "change without asking.",
            "",
        ]
        if agent_ctx.get("is_bundle_install") and not agent_ctx.get(
            "is_publisher_install"
        ):
            lines += [
                "This report came from a **consumer install**. Do not edit that "
                "install — fix the **publisher install** and publish a new version.",
                "",
            ]
        else:
            lines += [
                "**Golden rule for bundle publishers:** fix the **publisher "
                "install**, then publish a new version. Never edit a consumer's "
                "install — automatic-mode installs converge on the new revision on "
                "their own; manual-mode installs need their owner to click Update.",
                "",
            ]
        lines += [
            "This archive is another person's conversation. Do not copy it into an "
            "agent's workspace, do not commit it, and delete your local copy when "
            "you are done.",
            "",
        ]
        return "\n".join(lines)


# ── Small render helpers ─────────────────────────────────────────────


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _yes_no(value: Any) -> str:
    if value is None:
        return "—"
    return "yes" if value else "no"


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _join_or_dash(values: Any) -> str:
    items = [str(v) for v in (values or [])]
    return ", ".join(f"`{i}`" for i in items) if items else "—"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _row(label: str, value: Any) -> str:
    if isinstance(value, bool):
        rendered = _yes_no(value)
    else:
        rendered = str(value) if value not in (None, "") else "—"
    return f"| {label} | {rendered} |"
