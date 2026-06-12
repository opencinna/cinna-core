"""
Create a new workspace.

API endpoint: POST /api/v1/workspaces/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_post


def create_workspace(name: str, icon: str = "folder-kanban") -> dict:
    """Create a workspace and return the created object."""
    workspace = api_post("/api/v1/workspaces/", json={"name": name, "icon": icon})
    return workspace


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python create_workspace.py <name> [icon]")
        print("Example: python create_workspace.py 'Client Projects' briefcase")
        print()
        print("Common icon values: folder-kanban, briefcase, database, globe,")
        print("                    layers, settings, star, zap, cpu, mail")
        sys.exit(1)

    name = sys.argv[1]
    icon = sys.argv[2] if len(sys.argv) > 2 else "folder-kanban"

    print(f"Creating workspace '{name}' with icon '{icon}'...")
    workspace = create_workspace(name=name, icon=icon)

    print(f"\nWorkspace created successfully!")
    print(f"  ID:   {workspace['id']}")
    print(f"  Name: {workspace['name']}")
    print(f"  Icon: {workspace['icon']}")


if __name__ == "__main__":
    main()
