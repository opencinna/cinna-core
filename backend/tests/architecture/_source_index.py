"""One memoized view of the backend source tree, shared by the architecture tests.

Every architecture test reads ``app/`` as source, and each used to walk, read
and parse it on its own — some of them once per test function. The tree is
read through the container's bind mount, where a file open costs milliseconds,
and ``app/env-templates/`` can carry a full agent-runtime virtualenv (~10k
files) that a plain ``rglob`` descends into before the caller filters it out.
Together that made the architecture suite a minute of I/O and re-parsing for
88 tests.

What is shared here is only the expensive, policy-free part:

* :func:`python_files` walks a root **once**, pruning vendored directories at
  the directory level (every scanner already excluded them; none of them can
  hold first-party code). Each caller still applies its own exclusions —
  ``alembic``, ``env-templates`` and so on differ per invariant and stay in the
  test that owns the invariant.
* :func:`read_source` / :func:`parse_source` memoize per path. Callers treat the
  returned trees as read-only (none of the architecture tests mutates a tree;
  one that wants to transform a node must re-parse it, as
  ``credential_minting_gate_test`` already does via ``ast.unparse``).

The cache lives for the test session. The source tree does not change under a
running suite, so there is nothing to invalidate.
"""

from __future__ import annotations

import ast
import functools
import os
import pathlib
from collections.abc import Iterable

#: Directory names that only ever hold vendored or generated code. Pruned
#: during the walk, so their subtrees are never listed at all.
VENDORED_DIR_NAMES = frozenset({".venv", "site-packages", "node_modules", "__pycache__"})


@functools.cache
def _walk(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    files: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VENDORED_DIR_NAMES]
        files.extend(pathlib.Path(dirpath) / name for name in filenames if name.endswith(".py"))
    return tuple(sorted(files))


def python_files(root: pathlib.Path | str, exclude: Iterable[str] = ()) -> list[pathlib.Path]:
    """Sorted ``.py`` files under ``root``, minus any whose relative path has a
    component in ``exclude``. Paths are ``root / <relative>``, so a caller's own
    ``path.relative_to(root)`` keeps working."""
    root = pathlib.Path(root)
    excluded = frozenset(exclude)
    files = _walk(root)
    if not excluded:
        return list(files)
    return [path for path in files if not excluded.intersection(path.relative_to(root).parts)]


@functools.cache
def read_source(path: pathlib.Path | str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


@functools.cache
def parse_source(path: pathlib.Path | str) -> ast.Module:
    """The parsed module for ``path``. Shared between tests — do not mutate."""
    return ast.parse(read_source(path), filename=str(path))
