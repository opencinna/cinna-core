"""Conformance endpoint: the wrong shape for a list.

Usage: echo '{}' | python dashboards/<name>/api/items.py
"""
import json

print(json.dumps({"columns": [{"key": "title"}], "rows": []}))
