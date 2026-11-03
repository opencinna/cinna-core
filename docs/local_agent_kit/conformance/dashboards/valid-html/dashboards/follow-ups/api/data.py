"""Conformance endpoint: any JSON for an html view.

Usage: echo '{}' | python dashboards/<name>/api/data.py
"""
import json
import sys

sys.stdin.read()
print(json.dumps({"weeks": [1, 2, 3]}))
