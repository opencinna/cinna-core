"""
Update the agent's self-reported STATUS.md file atomically.

Writes YAML frontmatter (status, summary, timestamp) followed by a markdown body.
Uses a temp-file + rename pattern so readers never see a partial write.

Usage:
  python update_status.py --status ok --summary "All tasks complete"
  python update_status.py --status warning --summary "Queue backing up" --details "Queue depth: 142\nLast processed: 5 min ago"
  python update_status.py --status error --summary "Database unreachable"
"""
import argparse
import os
import sys
from datetime import datetime

STATUS_FILE = "/app/workspace/STATUS.md"
TEMP_FILE = "/app/workspace/.STATUS.md.tmp"

VALID_STATUSES = ("ok", "info", "warning", "error")


def build_content(status: str, summary: str | None, details: str | None) -> str:
    """Build the full STATUS.md content string."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = ["---", f"status: {status}"]
    if summary:
        # Escape any colons at the start of the summary to keep YAML valid
        safe_summary = summary.replace('"', "'")
        lines.append(f'summary: "{safe_summary}"')
    lines.append(f"timestamp: {timestamp}")
    lines.append("---")
    lines.append("")
    lines.append("# Agent Status")
    lines.append("")

    if details:
        lines.append(details)
    else:
        lines.append(f"Status updated to **{status}**.")

    lines.append("")
    return "\n".join(lines)


def write_atomic(content: str, target: str, tmp: str) -> None:
    """Write content to tmp file then rename to target (atomic on POSIX)."""
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.rename(tmp, target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update agent STATUS.md atomically.")
    parser.add_argument(
        "--status",
        required=True,
        choices=VALID_STATUSES,
        help="Status severity: ok | info | warning | error",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="Short one-line status description (optional)",
    )
    parser.add_argument(
        "--details",
        default=None,
        help="Multiline details appended to the body (optional)",
    )
    args = parser.parse_args()

    content = build_content(args.status, args.summary, args.details)

    try:
        write_atomic(content, STATUS_FILE, TEMP_FILE)
    except OSError as e:
        print(f"Error writing STATUS.md: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Status updated: {args.status}" + (f" — {args.summary}" if args.summary else ""))


if __name__ == "__main__":
    main()
