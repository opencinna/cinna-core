---
description: Review code architecture for proper route/service layer separation and abstractions.
---

## User Input

```text
$ARGUMENTS
```

Optional: Model name (e.g., "input_task"), file paths, or feature area to review.

## Context Detection

Determine review scope in this order:

1. **Explicit Arguments** - If user provides model name or paths, review those files
2. **Conversation History** - If recent implementation work exists, review those files
3. **Git Changes** - If no context, run `git diff --name-only` to find modified files

Focus on backend route and service files matching patterns:
- `backend/app/api/routes/*.py`
- `backend/app/services/*_service.py`

## Review Checklist

### 1. Route Layer Issues

**Business Logic in Routes** - Flag any of these patterns:
- Direct database queries (other than simple `session.get()`)
- Complex conditional logic for business rules
- Data transformation or computation
- Multiple service calls orchestrated together
- Status transitions or state management

**Code Duplication** - Look for repeated patterns across endpoints:
- Ownership verification: `if not is_superuser and entity.owner_id != user_id`
- Entity existence checks: `if not entity: raise HTTPException(404)`
- Permission checks for related entities (agents, workspaces)
- Response building with joins/aggregations
- Filter/parameter parsing logic

### 2. Service Layer Gaps

**Missing Abstractions** - Identify opportunities:
- Helper methods for common validations
- High-level orchestration methods combining multiple operations
- Extended retrieval methods (with related data)
- Filter parsing methods for complex query parameters

**Exception Handling** - Check for:
- Custom exception classes for domain errors
- Consistent error codes and messages
- Proper exception hierarchy

### 3. Responsibility Boundaries

**Route Layer Should:**
- Extract request parameters
- Call service methods
- Convert service exceptions to HTTP exceptions
- Return response models
- Handle superuser access bypass (if explicitly required)

**Service Layer Should:**
- Validate business rules
- Orchestrate database operations
- Handle entity relationships
- Manage state transitions
- Raise domain-specific exceptions
- Enforce owner-only access by default

### 4. Access Control Policy

**Default Behavior: Owner-Only Access**
- Service helper methods should check `entity.owner_id != user_id`
- This is the default for most models
- Do NOT add superuser bypass unless explicitly requested by user

**Superuser Access:**
- Only implement when user explicitly requests it
- Design case-by-case based on specific requirements
- Document the access rules clearly in the service method

**Sharing Logic:**
- If model has sharing (e.g., shared workspaces), implement separate helper
- Example: `get_with_access_check()` that checks owner OR shared access
- Document sharing rules in service docstrings

### 5. Deployment & Persistent Storage

**New On-Disk Write Paths** - Whenever a change introduces or starts relying on a directory/file path the backend writes to (a new `settings.*_DIR`, a new subdirectory under `/app/data/`, a new snapshot/cache/upload/export location, etc.), verify:
- The path is backed by a mounted volume in `docker-compose.yml` (and any override/prod compose files it should apply to) — NOT left on the container's ephemeral writable layer, where it is silently destroyed on the next container recreation/redeploy.
- Compare against the existing mount list (`uploads`, `app-data`, `agent-environments`) as precedent — a new path meant to persist should follow the same `HOST_*_DIR` env-var pattern (see `docker-compose.yml` backend `volumes:`/`environment:` blocks).
- The local bind-mount default directory (e.g. `./backend/data/<name>/`) is added to `.gitignore` alongside the existing `backend/data/uploads/`/`backend/data/agents/` entries — otherwise the folder's contents get accidentally tracked once it's created on disk.
- Treat a missing mount for data meant to survive redeploys as a **Critical** finding, not a nit — this exact gap (a bundle-snapshot directory never mounted) caused a silent false-negative in the git-versioning dirty check after every backend redeploy.

**Silent-Skip on Missing File/Directory** - Flag any code path where a file/directory that *should* exist (a referenced snapshot, a row's `snapshot_path`, a resolved-but-absent baseline) is missing and the code treats that as "nothing to compare / no changes / clean" instead of raising or self-healing. A missing-but-expected artifact is a corrupt/lost-state condition, not a legitimate empty/default state — silently defaulting to a negative/clean result there is a data-integrity bug, not a UX nicety.

### 6. Guard Asymmetry

**Mismatched Conditionals on Sibling Statements** - When two adjacent statements do the same job (two clears, two resets, two deletes, two assignments) and one is wrapped in a condition while the other is not, the asymmetry itself is the finding — either the new guard is unnecessary, or the unguarded line is missing one, and both cannot be true. Unlike most review heuristics this needs no judgement about intent: it is mechanically checkable, since it only requires noticing that two sibling statements disagree about whether they need a guard.

This earns a place on the checklist because it is easy to miss precisely when it matters most: in a multi-phase plan, the line that becomes wrong is often untouched by the diff that invalidates it. Example: a function clears two deprecated keys side by side, one guarded on the key being present, the other left unconditional because at the time every input carried that key — a later phase changes what produces the input so it no longer does, and the unconditional clear now injects the key into inputs that never had it, silently corrupting an ordering a downstream consumer depends on byte-for-byte. Neither diff looks wrong in isolation — the diff that invalidated the premise never touched the defective line, and the diff containing the defective line hadn't changed — only the pair does. Treat this as a habit applied to the code *surrounding* a change, not just the changed lines: when a phase adds a guard, check the neighboring statements doing similar work for whether they need the same guard.

**Look for:**
- A newly added conditional sitting beside an unconditional sibling doing similar work
- Two clears, resets, or deletes of related state where only one tests for presence
- A guard added in one branch of a copy-paste-derived pair but not the other

## Output Format

Generate a review report with:

### Summary
Brief overview of findings (2-3 sentences)

### Issues Found

For each issue:
```
**Issue:** [Brief description]
**Location:** [file:line_number]
**Pattern:** [What's wrong]
**Recommendation:** [How to fix]
```

### Recommended Refactoring

1. **New Service Methods** - List methods to add with signatures
2. **Exception Classes** - List custom exceptions to create
3. **Route Simplifications** - Describe how routes should change

### Code Impact
- Files to modify
- Estimated changes (lines added/removed)
- Breaking changes (if any)

## Reference Implementation

See the refactoring pattern in:
- **Routes (thin controller):** `backend/app/api/routes/input_tasks.py`
- **Service (business logic):** `backend/app/services/input_task_service.py` <!-- nocheck -->

Key patterns from reference:
- `_handle_service_error()` - Convert service exceptions to HTTP
- `InputTaskError` hierarchy - Domain-specific exceptions
- `verify_agent_access()` - Reusable validation helper
- `get_task_with_ownership_check()` - Ownership verification helper
- `get_task_extended()` - High-level retrieval with related data
- `refine_task()` - Orchestrated business operation

## Execution Steps

1. **Identify Files** - Determine scope from arguments, history, or git diff
2. **Read Route File** - Analyze for business logic and duplication
3. **Read Service File** - Check for missing abstractions
4. **Compare Patterns** - Match against reference implementation
5. **Check New Storage Paths** - If the diff introduces or reads/writes a new on-disk path (new config dir setting, new subdirectory under existing storage roots, etc.), check `docker-compose.yml`/override/prod compose for a matching volume mount (see §5 above)
5b. **Check Docs Impact** - Run `python3 .cinna-core-kit/scripts/docs_index.py impact`. If it lists docs that name changed files (or `affects` hops) and none of them is in the diff, report it as a finding: either the docs were updated, or the review states why no doc change is needed. A changed route path with a doc citation that `docs_index.py check` now flags is a Major finding.
6. **Generate Report** - List issues and recommendations
7. **Propose Changes** - Describe specific refactoring steps

Do NOT make changes automatically. Present the review report and wait for user approval before implementing.
