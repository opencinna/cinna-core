#!/usr/bin/env python3
"""
Documentation reference consistency checker.

Scans all markdown files in docs/, CLAUDE.md, backend/tests/,
.claude/agents/, and .claude/commands/ and verifies that file path
references to backend/, frontend/, and docs/ actually exist in the project.

Usage:
    python3 .cinna-core-kit/scripts/check_docs_references.py [--verbose]
    python3 .cinna-core-kit/scripts/check_docs_references.py --files docs/agents/foo.md docs/agents/bar.md

Options:
    --verbose, -v        Show each broken reference as it's found
    --files FILE...      Check only specific files (paths relative to project root)
    --include-incomplete Also scan docs/drafts/ (excluded by default)

Suppressing false positives:
    1. Pattern-based (automatic) — path segments that look like template/direction
       placeholders are skipped automatically:
         - starts with 'your_'  → your_entity.py, your_model.py
         - starts with '$'      → $entityId.tsx  (TanStack Router dynamic params)
         - starts with 'entit'  → entity.py, entities.tsx, EntityCard.tsx (case-insensitive)
         - contains '...'       → abbreviated paths
         - contains '[' or ']'  → generic placeholder syntax like [domain].py

    2. Inline annotation — add <!-- nocheck --> anywhere on a line to skip all
       reference checks on that line:
         ### CRUD (`backend/app/crud.py`) <!-- nocheck -->

Exit codes:
    0 - all references valid
    1 - broken references found
"""

import os
import re
import sys


def find_project_root():
    """Find project root by looking for docs/ directory."""
    # Try current directory first, then walk up
    path = os.path.abspath(os.getcwd())
    for _ in range(10):
        if os.path.isdir(os.path.join(path, "docs")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    # Fallback: script location based
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # .cinna-core-kit/scripts/ -> project root
    return os.path.abspath(os.path.join(script_dir, "..", ".."))


def find_markdown_files(docs_dir, exclude_dirs=None):
    """Recursively find all .md files under docs/, optionally excluding subdirectories."""
    exclude_dirs = set(exclude_dirs or [])
    md_files = []
    for root, dirs, files in os.walk(docs_dir):
        # Prune excluded directories in-place to prevent os.walk from descending
        dirs[:] = [
            d for d in dirs
            if os.path.join(root, d) not in exclude_dirs
        ]
        for f in files:
            if f.endswith(".md"):
                md_files.append(os.path.join(root, f))
    md_files.sort()
    return md_files


# Prefixes that indicate a project file reference
REFERENCE_PREFIXES = ("backend/", "frontend/", "docs/")


# Matches path segments that are clearly template/placeholder names, not real files.
# Covers: your_entity, $entityId (TanStack dynamic params), entity/entities/EntityCard/etc.
_PLACEHOLDER_SEGMENT_RE = re.compile(r"^your_|^\$|^entit", re.IGNORECASE)


def is_skippable_path(path):
    """
    Return True for paths that should be skipped (not real file references).

    Skips:
    - Paths with '...' ellipsis (abbreviated paths like backend/app/env-templates/.../routes.py)
    - Paths with '[' brackets (placeholder syntax like backend/app/api/routes/[domain].py)
    - Paths where any segment is a template/direction placeholder:
        - Starts with 'your_'  (e.g. your_entity.py)
        - Starts with '$'      (e.g. $entityId.tsx — TanStack Router dynamic params)
        - Starts with 'entit'  (e.g. entity.py, entities.tsx, EntityCard.tsx — case-insensitive)

    Lines can also be individually suppressed with an inline <!-- nocheck --> comment.
    """
    if "..." in path:
        return True
    if "[" in path or "]" in path:
        return True
    for segment in path.replace("\\", "/").split("/"):
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        if _PLACEHOLDER_SEGMENT_RE.match(stem):
            return True
    return False


def extract_backtick_references(line):
    """
    Extract file path references from backtick-enclosed content.

    Matches patterns like:
      `backend/app/models/agent.py`
      `backend/app/models/agent.py:46-50`
      `backend/app/services/file_service.py:upload_files()`
      `frontend/src/components/Foo.tsx`
      `docs/agents/agent_sessions/agent_sessions.md`
    """
    refs = []
    for match in re.finditer(r"`([^`\n]+)`", line):
        content = match.group(1)
        # Must start with a known prefix
        if not any(content.startswith(p) for p in REFERENCE_PREFIXES):
            continue
        # Strip line number / method suffixes: :123, :46-50, :method_name()
        path = re.split(r":[:\d]|:\w+\(", content)[0]
        # Also handle colon at end or colon followed by line number
        path = re.split(r":(\d+|[A-Za-z_]\w*\()", path)[0]
        # Skip if it looks like a generic pattern, not a real file
        if "{" in path or "}" in path or "*" in path:
            continue
        if is_skippable_path(path):
            continue
        if path:
            refs.append(path)
    return refs


def extract_markdown_link_references(line, source_file, project_root):
    """
    Extract file path references from markdown links.

    Matches: [text](relative/path.md) or [text](backend/app/foo.py)
    Resolves relative paths based on the source file location.
    """
    refs = []
    for match in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", line):
        target = match.group(2)
        # Skip URLs, anchors, and images
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        # Remove anchor fragments
        target = target.split("#")[0]
        if not target:
            continue
        # Skip if it looks like a generic pattern
        if "{" in target or "}" in target or "*" in target:
            continue
        if is_skippable_path(target):
            continue

        # If starts with known prefix, treat as project-root relative
        if any(target.startswith(p) for p in REFERENCE_PREFIXES):
            refs.append(target)
        else:
            # Resolve relative to the source file's directory
            source_dir = os.path.dirname(source_file)
            rel_from_root = os.path.relpath(source_dir, project_root)
            resolved = os.path.normpath(os.path.join(rel_from_root, target))
            # Only track if it resolves to something under docs/
            if not resolved.startswith(".."):
                refs.append(resolved)
    return refs


def extract_references(line, source_file, project_root):
    """Extract all file references from a single line."""
    refs = []
    refs.extend(extract_backtick_references(line))
    refs.extend(extract_markdown_link_references(line, source_file, project_root))
    return refs


def check_reference(ref, project_root):
    """
    Check if a reference points to an existing file or directory.
    Returns True if valid, False if broken.
    """
    full_path = os.path.join(project_root, ref)
    return os.path.exists(full_path)


def scan_file(filepath, project_root, verbose=False):
    """Scan a single markdown file for broken references."""
    issues = []
    rel_path = os.path.relpath(filepath, project_root)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (IOError, UnicodeDecodeError) as e:
        issues.append({
            "file": rel_path,
            "line": 0,
            "reference": "",
            "error": "Could not read file: %s" % str(e),
        })
        return issues

    for line_num, line in enumerate(lines, start=1):
        # Allow per-line suppression via inline <!-- nocheck --> comment
        if "<!-- nocheck -->" in line:
            continue
        refs = extract_references(line, filepath, project_root)
        for ref in refs:
            if not check_reference(ref, project_root):
                issues.append({
                    "file": rel_path,
                    "line": line_num,
                    "reference": ref,
                    "error": "File or directory not found",
                })
                if verbose:
                    print(
                        "  BROKEN: %s:%d -> %s" % (rel_path, line_num, ref)
                    )

    return issues


def parse_args(argv):
    """Simple argument parser (Python 3.9 compatible, no argparse needed)."""
    verbose = False
    include_incomplete = False
    specific_files = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg == "--include-incomplete":
            include_incomplete = True
        elif arg == "--files":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                specific_files.append(argv[i])
                i += 1
            continue
        i += 1
    return verbose, include_incomplete, specific_files


def main():
    verbose, include_incomplete, specific_files = parse_args(sys.argv)

    project_root = find_project_root()
    docs_dir = os.path.join(project_root, "docs")

    if not os.path.isdir(docs_dir):
        print("ERROR: docs/ directory not found at %s" % docs_dir)
        sys.exit(1)

    if specific_files:
        md_files = []
        for f in specific_files:
            full = os.path.join(project_root, f)
            if os.path.isfile(full) and f.endswith(".md"):
                md_files.append(full)
            else:
                print("WARNING: Skipping %s (not found or not .md)" % f)
        if not md_files:
            print("No valid markdown files provided.")
            sys.exit(0)
    else:
        exclude_dirs = []
        if not include_incomplete:
            drafts_dir = os.path.join(docs_dir, "drafts")
            if os.path.isdir(drafts_dir):
                exclude_dirs.append(drafts_dir)
        md_files = find_markdown_files(docs_dir, exclude_dirs=exclude_dirs)

        # Also include CLAUDE.md at project root
        claude_md = os.path.join(project_root, "CLAUDE.md")
        if os.path.isfile(claude_md):
            md_files.append(claude_md)

        # Also include CAPABILITIES_AGENT.md at project root (the LLM-facing
        # capability guide that references docs/ feature pages). Cross-repo links
        # (e.g. cinna-desktop) must be written as absolute http(s) URLs so the
        # checker skips them — only repo-relative docs/ paths are verified.
        capabilities_md = os.path.join(project_root, "CAPABILITIES_AGENT.md")
        if os.path.isfile(capabilities_md):
            md_files.append(capabilities_md)

        # Also include all .md files under backend/tests/
        tests_dir = os.path.join(project_root, "backend", "tests")
        if os.path.isdir(tests_dir):
            md_files.extend(find_markdown_files(tests_dir))

        # Also include all .md files under .claude/agents/ and .claude/commands/
        for claude_subdir in ("agents", "commands"):
            claude_dir = os.path.join(project_root, ".claude", claude_subdir)
            if os.path.isdir(claude_dir):
                md_files.extend(find_markdown_files(claude_dir))

        if not md_files:
            print("No markdown files found")
            sys.exit(0)

    print("Scanning %d markdown files ..." % len(md_files))
    if verbose:
        print("Project root: %s" % project_root)
        if not specific_files and not include_incomplete:
            print("Note: docs/drafts/ excluded (use --include-incomplete to scan it)")
        print()

    all_issues = []
    files_with_issues = set()

    for filepath in md_files:
        issues = scan_file(filepath, project_root, verbose=verbose)
        if issues:
            all_issues.extend(issues)
            files_with_issues.add(os.path.relpath(filepath, project_root))

    # Print summary
    print()
    if not all_issues:
        print("OK: All references are valid across %d files." % len(md_files))
        sys.exit(0)

    print("=" * 70)
    print("BROKEN REFERENCES FOUND: %d issues in %d files" % (
        len(all_issues), len(files_with_issues)
    ))
    print("=" * 70)
    print()

    # Group by file
    by_file = {}
    for issue in all_issues:
        key = issue["file"]
        if key not in by_file:
            by_file[key] = []
        by_file[key].append(issue)

    for filepath in sorted(by_file.keys()):
        issues = by_file[filepath]
        print("%s (%d broken):" % (filepath, len(issues)))
        for issue in issues:
            if issue["reference"]:
                print("  Line %d: %s - %s" % (
                    issue["line"], issue["reference"], issue["error"]
                ))
            else:
                print("  %s" % issue["error"])
        print()

    sys.exit(1)


if __name__ == "__main__":
    main()
