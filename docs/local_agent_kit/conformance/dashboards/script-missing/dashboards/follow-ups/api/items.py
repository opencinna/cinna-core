"""Conformance endpoint: a valid `list` result.

Usage: echo '{}' | python dashboards/<name>/api/items.py
"""
import json
import sys

params = json.loads(sys.stdin.read() or "{}")
print(json.dumps({"items": [{"id": "f-1", "title": "Reply to Acme", "priority": "high"}],
                  "generated_at": "2026-11-03T09:00:00Z"}))
