"""Dashboard endpoint: weekly revenue for one month, for the html view.

Usage: echo '{"month": "2026-11"}' | uv run python dashboards/<name>/api/data.py

stdin is the params object; stdout is one JSON value, which the page receives
through `cinna.onData`. cwd is the agent folder. Read data something else
prepared under app-data/dashboards/<name>/; never crawl on every call.
"""

import json
import sys
from datetime import date
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent.name
DATA_FILE = Path("app-data") / "dashboards" / COMPONENT / "weeks.json"
SAMPLE_VALUES = [18400, 22100, 15800, 24900]


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    month = params.get("month") or date.today().strftime("%Y-%m")
    if DATA_FILE.is_file():
        weeks = json.loads(DATA_FILE.read_text(encoding="utf-8")).get(month, [])
        notice = None
    else:
        weeks = []
        for i, value in enumerate(SAMPLE_VALUES):
            year, week, _ = date(int(month[:4]), int(month[5:7]), 1 + 7 * i).isocalendar()
            weeks.append({"id": f"{year}-W{week:02d}", "label": f"W{week:02d}", "value": value})
        notice = "Sample data: nothing has been written yet."
    result = {"month": month, "weeks": weeks}
    if notice:
        result["notice"] = notice
    print(json.dumps(result))


if __name__ == "__main__":
    main()
