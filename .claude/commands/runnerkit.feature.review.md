---
description: Comprehensive feature review — architecture, completeness, correctness, usability, and risks.
---

## User Input

```text
$ARGUMENTS
```

Optional: Feature name, file paths, or topic area to review. If empty, review the feature reflected in current git changes.

## Context Detection

Determine review scope in this order:

1. **Explicit Arguments** — If user provides a feature name or paths, review that feature
2. **Conversation History** — If recent implementation work exists, review that feature
3. **Git Changes** — If no context, run `git status` and `git diff --name-only` to identify the feature from changed files

Once the feature is identified, gather **all** related files — don't limit to just the diff. The diff is the entry point; the review covers the full feature.

## Required Reading Before Review

1. **`docs/README.md`** — Locate the feature in the registry, identify its docs and integration points
2. **Feature business logic doc** (`{feature}.md`) — Understand intended behavior and business rules
3. **Feature tech doc** (`{feature}_tech.md`) — Understand intended file locations, schema, endpoints, services
4. **All implementation files** listed in the tech doc — Models, routes, services, agent-env tools, migrations, tests
5. **Integration point files** — Read the specific hooks/calls in adjacent services (not the full files — just the collaboration-touching code)

If feature docs don't exist yet, infer the feature scope from the changed files and related code.

## Review Dimensions

Evaluate the feature across all of the following dimensions. Be critical and specific — cite file paths and line numbers.

### 1. Architecture & Design

- Does the feature follow project patterns? (route/service separation, model hierarchy, dependency injection)
- Is the layering clean? (routes as thin controllers, business logic in services, no leaky abstractions)
- Are integration points well-chosen? (hooks in the right places, minimal coupling)
- Is the data model appropriate? (field types, relationships, cascade behavior, indexes)
- Are there any architectural red flags? (circular dependencies, god services, wrong abstraction level)

### 2. Correctness & Robustness

- **Concurrency issues** — Race conditions on shared state, missing locks, read-modify-write without protection
- **Transaction boundaries** — Are commits in the right places? Could partial failures leave inconsistent state?
- **Error handling** — Are errors caught, propagated, and surfaced correctly? Silent failures? Swallowed exceptions?
- **Edge cases** — Empty inputs, null fields, deleted foreign keys, duplicate calls, reentrant operations
- **Status/state machines** — Are all transitions valid? Are there unreachable or stuck states? Missing terminal transitions?
- **Data integrity** — Could operations produce orphaned records, dangling references, or inconsistent aggregates?

### 3. Security

- **Authorization** — Owner checks, cross-user isolation, permission escalation paths
- **Input validation** — Untrusted input sanitized before DB queries or tool dispatch?
- **Information leakage** — Do error messages expose internal state? Are IDs guessable?
- **Injection risks** — SQL injection (unlikely with ORM but check raw queries), prompt injection via user-controlled fields passed to LLM prompts

### 4. Completeness

Assess what percentage of the feature is implemented across each layer:

| Layer | Status | Notes |
|-------|--------|-------|
| Data model & migration | | |
| API endpoints | | |
| Service logic | | |
| Agent-env tools (if applicable) | | |
| Prompt injection (if applicable) | | |
| Integration hooks | | |
| Backend tests | | |
| Frontend UI | | |
| Documentation | | |

Flag any layers that are entirely missing vs. partially done vs. complete.

### 5. Test Coverage

- Are the critical paths tested? (happy path, error paths, edge cases)
- Are the **most important business logic paths** covered? (the core value proposition of the feature)
- Are there gaps where mocking hides real behavior? (mocks that skip the exact code path being tested)
- Are integration points tested? (hooks into adjacent services)
- Is cross-user isolation tested?
- Are there any test anti-patterns? (testing implementation details, fragile assertions, missing cleanup)

### 6. Usability & Operability

- Can users (human or agent) actually use this feature end-to-end?
- Are there missing UI surfaces that make the feature invisible or unmanageable?
- Are there monitoring/observability gaps? (logging, status visibility, error surfacing)
- Can the feature be debugged when something goes wrong? (are states inspectable, are errors traceable?)
- Is there a cancellation/rollback path for long-running operations?

### 7. Documentation Consistency

- Does the documentation match the implementation? (endpoints, field names, status values, business rules)
- Are there documented features that aren't implemented, or implemented features that aren't documented?
- Are integration points documented bidirectionally? (feature A's doc mentions feature B, and vice versa)

## Output Format

### Feature Summary

2-3 sentences: what the feature does, its current state, and overall assessment.

### Verdict

One of:
- **Production-ready** — No blocking issues, complete across all required layers
- **Near-complete** — Minor issues or gaps, usable with known limitations
- **Significant gaps** — Core functionality works but important pieces missing
- **Not ready** — Blocking issues that prevent real usage

Include a percentage estimate (e.g., "~80% complete for backend-first delivery").

### Critical Issues

Issues that **must** be fixed before the feature can be relied upon. For each:

```
**Issue:** [Brief description]
**Severity:** Critical | High | Medium
**Location:** [file:line_number or component]
**Problem:** [What's wrong — be specific]
**Impact:** [What breaks or could break]
**Fix:** [Concrete recommendation]
```

### Observations

Non-blocking findings worth noting — code quality, potential improvements, minor inconsistencies. Keep these concise (1-2 lines each).

### Completeness Table

The layer-by-layer status table from dimension 4.

### Test Coverage Gaps

Specific test scenarios that are missing, ordered by importance.

### Recommendations

Prioritized list of next steps, grouped by urgency:
1. **Must fix** — Blocking issues
2. **Should fix** — Important for reliability/usability
3. **Nice to have** — Polish, optimization, future-proofing

## Execution Steps

1. **Identify feature scope** — From arguments, conversation, or git changes
2. **Read documentation** — `docs/README.md` → feature docs → tech docs
3. **Read all implementation files** — Models, migration, routes, services, agent-env tools
4. **Read integration points** — Grep for the feature name in adjacent services
5. **Read tests** — Test files and test utilities
6. **Analyze each dimension** — Work through all 7 review dimensions systematically
7. **Generate report** — Structured output following the format above

Do NOT make changes automatically. Present the review report and wait for user direction before implementing any fixes.
