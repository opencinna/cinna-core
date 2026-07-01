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
6. **Generate Report** - List issues and recommendations
7. **Propose Changes** - Describe specific refactoring steps

Do NOT make changes automatically. Present the review report and wait for user approval before implementing.
