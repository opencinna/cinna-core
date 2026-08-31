"""Token-overlap (Jaccard) scoring shared by the routing surfaces that rank text.

**Why this module exists.** Two surfaces ask the same question — *"how much does
this piece of owner-authored routing text look like that one?"* — and they have
to answer it identically:

- **Install-time route-conflict detection** compares a freshly-installed agent's
  trigger prompt against the installer's other routes and surfaces
  near-duplicate intents ("Calendar Planner" vs "Vacation Planner") as a
  non-blocking toast.
- **Reachability near-miss ranking** (``routing_reachability_service``) scores a
  message that routed nowhere against the candidates that were on the ballot, so
  the tuning card can say "closest: X 0.31".

Both were served by two private static methods on ``AppAgentRouteService``, a
service that owns *neither* caller's surface — the reachability module reached
across a service boundary for an underscore-prefixed helper and said so in its
own docstring, flagged rather than laundered, with "if a third caller appears,
promote them". The third caller never appeared; the owning service is being
deleted instead, which promotes them just the same. They live here now because
a shared scoring rule is not private implementation detail of whoever happened
to need it first.

**Copying instead of importing is the failure mode this prevents.** Two copies
of a tokenizer agree exactly until one of them is tuned, and then a conflict
warning and a near-miss score disagree about what a token is — silently, on two
different screens, with no test that would go red. Callers *call* these; they do
not reimplement them.

**No stopword pruning.** Jaccard already discounts common tokens, because a word
appearing on both sides lands in the intersection *and* the union. The
short-token floor below does the small amount of filtering that is worth doing.
"""
from __future__ import annotations

import re

#: Default cutoff for "these two texts describe the same intent". Tuned
#: conservatively so unrelated agents do not generate noise; callers may pass
#: their own threshold where the cost of a false positive differs.
SIMILARITY_THRESHOLD: float = 0.45


def tokens_for_similarity(text: str | None) -> set[str]:
    """Tokenise ``text`` for Jaccard comparison.

    Lowercases, splits on non-alphanumerics, drops tokens shorter than
    3 chars (filters single letters / digits / very common short
    words). Returns a set of tokens.
    """
    if not text:
        return set()

    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3}


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Overlap of two token sets as ``|a ∩ b| / |a ∪ b|``, ``0.0`` if either is empty."""
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    if not union:
        return 0.0
    return len(intersection) / len(union)
