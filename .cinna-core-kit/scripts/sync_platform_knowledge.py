"""
Sync documentation and auto-generated API reference into the platform-knowledge
environment template's knowledge/platform/ directory.

This snapshot is the only copy of the platform docs + API reference present
inside the backend container at runtime; the account-CLI context-package
endpoint serves it to local orchestrator agents.

Three data sources:
  1. docs/application/ and docs/agents/ — business-logic docs (excluding *_tech* files)
  2. frontend/openapi.json — auto-generated REST API reference grouped by tag
  3. docs/local_agent_kit/ — the Local Agent Kit served by the public /agent-start surface

Each source owns exactly one target subtree and only that subtree is cleared:
knowledge/platform/ for (1)+(2), knowledge/local-kit/ for (3). knowledge/guides/
is hand-authored in place and is never touched by this script.

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
_KNOWLEDGE_ROOT = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "env-templates"
    / "platform-knowledge-env"
    / "app"
    / "workspace"
    / "knowledge"
)
TARGET = _KNOWLEDGE_ROOT / "platform"

# Local Agent Kit: hand-authored under docs/, served (rendered) by the public
# /agent-start surface from this snapshot — docs/ is not shipped in the backend image,
# so this copy is the only one available at runtime.
KIT_SOURCE = PROJECT_ROOT / "docs" / "local_agent_kit"
KIT_TARGET = _KNOWLEDGE_ROOT / "local-kit"

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
# Local Agent Kit sync
# ---------------------------------------------------------------------------

# Directory names never copied into the snapshot (build artefacts, not content).
KIT_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules"}

# Filenames never copied, whatever they are doing in the tree. Everything under
# docs/local_agent_kit/ is published verbatim to ANONYMOUS callers at
# /agent-start/kit/<path>, so anything a developer leaves next to
# credentials/.env.example — a real .env, a key, an editor scratch file — would
# be world-readable. The kit's own .env.example is the one env file that ships.
KIT_DENY_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".crt", ".swp", ".orig")
KIT_DENY_NAMES = {".DS_Store", "Thumbs.db"}


def _is_publishable(rel: Path) -> bool:
    """Whether a kit file may be published to anonymous callers."""
    name = rel.name
    if name in KIT_DENY_NAMES:
        return False
    if name.lower().endswith(KIT_DENY_SUFFIXES):
        return False
    # `.env.example` is documentation; any other env file is a secret.
    if ".env" in name and not name.endswith(".example"):
        return False
    return True


def sync_local_agent_kit() -> int:
    """Copy docs/local_agent_kit/ verbatim into knowledge/local-kit/.

    Every file type is copied, dotfiles included
    (``templates/agent/.claude/settings.local.json``,
    ``templates/agent/credentials/.gitignore``,
    ``credentials/.env.example``) and ``tools/kit.py`` — the kit is served
    byte-for-byte (after placeholder rendering) by the public ``/agent-start``
    surface, so a filtered copy would ship a broken scaffold.

    The scaffold's own path-excluding ignore rules ship under dotless
    ``gitignore`` names (``kit.py new`` restores the dot); do not "fix" that
    here or in the templates — a live ``.gitignore`` inside this tree hides
    scaffold files from *this* repository, so a fresh clone would sync a
    smaller kit than the one that was tested. Returns the file count.
    """
    if not KIT_SOURCE.is_dir():
        raise SystemExit(f"ERROR: docs/local_agent_kit/ not found at {KIT_SOURCE}")

    if KIT_TARGET.exists():
        shutil.rmtree(KIT_TARGET)
    KIT_TARGET.mkdir(parents=True)

    count = 0
    skipped: list[str] = []
    for src_file in sorted(KIT_SOURCE.rglob("*")):
        rel = src_file.relative_to(KIT_SOURCE)
        if KIT_SKIP_DIRS & set(rel.parts):
            continue
        # Symlinks are never followed. `shutil.copy2` would dereference one and
        # copy its target's bytes into a publicly served tree, so a link out of
        # the kit would become a published file. The runtime service skips
        # symlinks for the same reason; the two must agree.
        if src_file.is_symlink():
            skipped.append(f"{rel} (symlink)")
            continue
        if src_file.is_dir():
            continue
        if not src_file.is_file():  # socket, fifo, broken link — skip
            continue
        if not _is_publishable(rel):
            skipped.append(f"{rel} (not publishable)")
            continue
        dest = KIT_TARGET / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        count += 1

    for entry in skipped:
        print(f"  SKIPPED {entry}")

    if count == 0:
        raise SystemExit(f"ERROR: docs/local_agent_kit/ is empty at {KIT_SOURCE}")

    print(f"  Copied {count} kit files → local-kit/")
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Syncing platform knowledge...")
    print(f"  Docs target: {TARGET.relative_to(PROJECT_ROOT)}")
    print(f"  Kit target:  {KIT_TARGET.relative_to(PROJECT_ROOT)}")
    print()

    # Clear and recreate — knowledge/platform/ only. knowledge/guides/ is
    # hand-authored in place and knowledge/local-kit/ is cleared by its own
    # step, so neither is affected.
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    print("[1/3] Copying feature documentation (excluding _tech files)...")
    doc_count = sync_docs()

    print()
    print("[2/3] Generating API reference from OpenAPI spec...")
    api_count = sync_api_reference()

    print()
    print("[3/3] Syncing local agent kit...")
    kit_count = sync_local_agent_kit()

    print()
    print(f"Sync complete. Total files: {doc_count + api_count + kit_count}")


if __name__ == "__main__":
    main()
