"""
List all configured mail servers.

API endpoint: GET /api/v1/mail-servers/
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from platform_helper import api_get


def list_mail_servers() -> list[dict]:
    """Fetch and return all mail servers."""
    result = api_get("/api/v1/mail-servers/")
    return result.get("data", [])


def main() -> None:
    mail_servers = list_mail_servers()

    if not mail_servers:
        print("No mail servers found.")
        print()
        print("To create a mail server:")
        print("  1. Create an IMAP credential using the credentials API")
        print("  2. POST /api/v1/mail-servers/ with {name, credential_id}")
        return

    print(f"Found {len(mail_servers)} mail server(s):\n")
    for server in mail_servers:
        print(f"  ID:            {server['id']}")
        print(f"  Name:          {server['name']}")
        print(f"  Credential ID: {server.get('credential_id', 'unknown')}")
        print(f"  Created:       {server.get('created_at', 'unknown')}")
        print()


if __name__ == "__main__":
    main()
