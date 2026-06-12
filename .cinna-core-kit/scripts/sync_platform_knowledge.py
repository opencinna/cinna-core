"""
Sync documentation and auto-generated API reference into the platform-knowledge
environment template's knowledge/platform/ directory.

This snapshot is the only copy of the platform docs + API reference present
inside the backend container at runtime; the account-CLI context-package
endpoint serves it to local orchestrator agents.

Two data sources:
  1. docs/application/ and docs/agents/ — business-logic docs (excluding *_tech* files)
  2. frontend/openapi.json — auto-generated REST API reference grouped by tag

Usage:
    python3 .cinna-core-kit/scripts/sync_platform_knowledge.py

Run from the project root whenever you update documentation or API routes.
Prerequisite: run `make gen-client` first so openapi.json is up to date.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Reuse the backend's shared API-reference generation logic so it is defined
# in exactly one place (app.services.cli.platform_knowledge_assets). The
# generation functions are settings-free, so importing them here does not
# require the backend's full runtime config.
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from app.services.cli.platform_knowledge_assets import (  # noqa: E402
    api_reference_index,
    generate_api_reference,
)

DOC_SOURCES = {
    "application": PROJECT_ROOT / "docs" / "application",
    "agents": PROJECT_ROOT / "docs" / "agents",
}
DOCS_README = PROJECT_ROOT / "docs" / "README.md"
OPENAPI_PATH = PROJECT_ROOT / "frontend" / "openapi.json"
TARGET = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "env-templates"
    / "platform-knowledge-env"
    / "app"
    / "workspace"
    / "knowledge"
    / "platform"
)

# API-reference generation logic (generate_api_reference, api_reference_index,
# _tag_title, SKIP_TAGS) is imported from
# app.services.cli.platform_knowledge_assets above so it lives in exactly one
# place.


# ---------------------------------------------------------------------------
# Documentation sync
# ---------------------------------------------------------------------------

def sync_docs() -> int:
    """Copy business-logic docs (excluding _tech files). Returns file count."""
    total = 0
    for name, src in DOC_SOURCES.items():
        if not src.is_dir():
            raise SystemExit(f"ERROR: docs/{name}/ not found at {src}")
        count = 0
        for md_file in src.rglob("*.md"):
            if "_tech" in md_file.name:
                continue
            rel = md_file.relative_to(src)
            dest = TARGET / name / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md_file, dest)
            count += 1
        print(f"  Copied {count} doc files → platform/{name}/")
        total += count

    if DOCS_README.is_file():
        shutil.copy2(DOCS_README, TARGET / "README.md")
        total += 1
        print(f"  Copied docs/README.md → platform/README.md")

    return total


def sync_api_reference() -> int:
    """Generate API reference from OpenAPI spec. Returns file count."""
    if not OPENAPI_PATH.is_file():
        print(f"  WARNING: {OPENAPI_PATH.relative_to(PROJECT_ROOT)} not found — skipping API reference")
        print(f"           Run `make gen-client` first to generate the OpenAPI spec")
        return 0

    with open(OPENAPI_PATH) as f:
        spec = json.load(f)

    api_dir = TARGET / "api_reference"
    api_dir.mkdir(parents=True, exist_ok=True)

    references = generate_api_reference(spec)

    # Write per-tag files
    for tag, content in references.items():
        filename = tag.replace("-", "_") + ".md"
        (api_dir / filename).write_text(content)

    # Write index (shared builder keeps the format identical to the endpoint)
    (api_dir / "README.md").write_text(api_reference_index(spec, references))

    total = len(references) + 1  # +1 for README
    print(f"  Generated {len(references)} API reference files → platform/api_reference/")
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Syncing platform knowledge...")
    print(f"  Target: {TARGET.relative_to(PROJECT_ROOT)}")
    print()

    # Clear and recreate
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    print("[1/2] Copying feature documentation (excluding _tech files)...")
    doc_count = sync_docs()

    print()
    print("[2/2] Generating API reference from OpenAPI spec...")
    api_count = sync_api_reference()

    print()
    print(f"Sync complete. Total files: {doc_count + api_count}")


if __name__ == "__main__":
    main()
