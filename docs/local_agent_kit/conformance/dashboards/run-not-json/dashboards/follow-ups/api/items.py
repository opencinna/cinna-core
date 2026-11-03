"""Conformance endpoint: logs on stdout.

Usage: echo '{}' | python dashboards/<name>/api/items.py
"""
import json

print("fetching items...")
print(json.dumps({"items": []}))
