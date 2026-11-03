"""Dashboard endpoint: open follow-ups in the `list` shape (cinna_protocol dashboards/1).

Usage: echo '{"priority": "high"}' | uv run python dashboards/<name>/api/items.py

stdin is the params object; stdout is one JSON value. cwd is the agent folder.
Read data something else prepared (a script, a refresh command or a schedule wrote
it under app-data/dashboards/<name>/); never crawl a slow system on every call.
Logs go to stderr, never stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent.name
DATA_FILE = Path("app-data") / "dashboards" / COMPONENT / "items.json"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

SAMPLE = [
    {"id": "f-1", "title": "Reply to Acme about the renewal", "subtitle": "Acme Ltd",
     "meta": "Quote sent 3 days ago", "time": "2026-11-02T09:15:00Z", "priority": "high"},
    {"id": "f-2", "title": "Send the onboarding checklist", "subtitle": "Globex",
     "time": "2026-11-01T16:40:00Z", "priority": "medium", "badge": "new client"},
    {"id": "f-3", "title": "Confirm Friday's call", "subtitle": "Initech",
     "time": "2026-10-30T11:00:00Z", "priority": "low"},
]


def load_items() -> tuple[list[dict], str | None]:
    """The prepared items, or the sample with a notice while nothing has been written."""
    if DATA_FILE.is_file():
        return json.loads(DATA_FILE.read_text(encoding="utf-8")), None
    return SAMPLE, "Sample data: nothing has been written yet."


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    wanted = params.get("priority", "all")
    items, notice = load_items()
    if wanted != "all":
        items = [item for item in items if item.get("priority") == wanted]
    items.sort(key=lambda item: PRIORITY_ORDER.get(item.get("priority", "low"), 3))
    result = {"items": items}
    if notice:
        result["notice"] = notice
    print(json.dumps(result))


if __name__ == "__main__":
    main()
