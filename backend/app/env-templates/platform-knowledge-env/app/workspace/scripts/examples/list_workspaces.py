"""
List all workspaces belonging to the current user.

API endpoint: GET /api/v1/workspaces/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_get


def list_workspaces() -> list[dict]:
    """Fetch and return all workspaces."""
    result = api_get("/api/v1/workspaces/")
    return result.get("data", [])


def main() -> None:
    workspaces = list_workspaces()

    if not workspaces:
        print("No workspaces found.")
        return

    print(f"Found {len(workspaces)} workspace(s):\n")
    for ws in workspaces:
        print(f"  ID:   {ws['id']}")
        print(f"  Name: {ws['name']}")
        print(f"  Icon: {ws.get('icon', 'none')}")
        print()


if __name__ == "__main__":
    main()
