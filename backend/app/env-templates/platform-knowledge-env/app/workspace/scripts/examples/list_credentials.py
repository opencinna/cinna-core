"""
List available credentials (names, types, and IDs only — never values).

API endpoint: GET /api/v1/credentials/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_get


def list_credentials(workspace_id: str | None = None, limit: int = 200) -> list[dict]:
    """Fetch and return all credentials. Credential values are never returned by the API."""
    params: dict = {"limit": limit}
    if workspace_id:
        params["user_workspace_id"] = workspace_id

    result = api_get("/api/v1/credentials/", params=params)
    return result.get("data", [])


def main() -> None:
    workspace_id = sys.argv[1] if len(sys.argv) > 1 else None

    credentials = list_credentials(workspace_id=workspace_id)

    if not credentials:
        print("No credentials found.")
        return

    print(f"Found {len(credentials)} credential(s):\n")
    for cred in credentials:
        workspace = cred.get("user_workspace_id") or "no workspace"
        print(f"  ID:        {cred['id']}")
        print(f"  Name:      {cred['name']}")
        print(f"  Type:      {cred['type']}")
        print(f"  Notes:     {cred.get('notes', '')[:60]}")
        print(f"  Workspace: {workspace}")
        print()


if __name__ == "__main__":
    main()
