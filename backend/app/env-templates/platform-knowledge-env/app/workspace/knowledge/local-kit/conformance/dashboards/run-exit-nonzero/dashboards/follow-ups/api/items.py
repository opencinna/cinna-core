"""Conformance endpoint: fails.

Usage: echo '{}' | python dashboards/<name>/api/items.py
"""
import sys

sys.exit("crm token expired")
