"""Write the agent's self-reported status file atomically.

Produces ``app-data/storage/STATUS.md`` with YAML frontmatter followed by a markdown
body, using a temp-file + ``os.replace`` so a reader never sees a partial write.
This is the same file and the same convention the platform reads in the cloud, so a
locally built agent's status surfaces on its agent card unchanged after import.

Usage: uv run scripts/update_status.py --status {ok,info,warning,error} [--summary TEXT] [--details TEXT]

Examples::

    uv run scripts/update_status.py --status ok --summary "All clear"
    uv run scripts/update_status.py --status warning --summary "Queue backing up" \
        --details "Queue depth: 142"
    uv run scripts/update_status.py --status error --summary "Source unreachable"

In the cloud the same calls run as ``python scripts/update_status.py ...``.

Frontmatter keys are exactly ``status``, ``summary`` and ``timestamp``. Severity is
one of ok | info | warning | error; anything else is rejected here because the
platform would normalise it to ``unknown``.

STATUS.md is a public artefact: it is rendered in the UI, returned by the REST API
and shared over A2A. Never put a credential value, a token or a personal identifier
in ``--summary`` or ``--details``.

Standard library only. Python 3.11+.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = AGENT_ROOT / "app-data" / "storage" / "STATUS.md"
TEMP_FILE = AGENT_ROOT / "app-data" / "storage" / ".STATUS.md.tmp"


def build_content(status: str, summary: str | None, details: str | None) -> str:
    """Build the full STATUS.md text."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = ["---", f"status: {status}"]
    if summary:
        # Keep the YAML scalar valid whatever the caller passed in.
        safe_summary = summary.replace('"', "'").replace("\n", " ").strip()
        lines.append(f'summary: "{safe_summary}"')
    lines.append(f"timestamp: {timestamp}")
    lines.append("---")
    lines.append("")
    lines.append("# Agent Status")
    lines.append("")
    lines.append(details if details else f"Status updated to **{status}**.")
    lines.append("")
    return "\n".join(lines)


def write_atomic(content: str, target: Path, tmp: Path) -> None:
    """Write to a temp file in the same directory, then rename over the target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def build_parser() -> argparse.ArgumentParser:
    """The script's options, readable without running it."""
    parser = argparse.ArgumentParser(description="Update the agent's STATUS.md atomically.")
    parser.add_argument(
        "--status",
        required=True,
        choices=["ok", "info", "warning", "error"],
        help="Severity: ok | info | warning | error",
    )
    parser.add_argument("--summary", default=None, help="Short one-line description")
    parser.add_argument("--details", default=None, help="Markdown body appended below the heading")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        write_atomic(build_content(args.status, args.summary, args.details), STATUS_FILE, TEMP_FILE)
    except OSError as exc:
        print(f"Error writing STATUS.md: {exc}", file=sys.stderr)
        return 1

    suffix = f" - {args.summary}" if args.summary else ""
    print(f"Status updated: {args.status}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
