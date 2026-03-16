---
description: Create or update documentation file following project practices.
---

## User Input

```text
$ARGUMENTS
```

Feature name, description, or existing doc path to create/refactor.

## Task

Create or refactor feature documentation in `docs/` following the layered documentation structure.

**If given an existing doc path** - refactor it into the correct structure (split into business logic / tech / widget files as needed, move to correct domain/feature folder).
**If given a feature name** - create new documentation by exploring the codebase first.

## Documentation Architecture (3 Layers)

### Layer 1: Project Index (`docs/README.md`)

Super-concise entry point for understanding the project. Contains:
- **Project Purpose** - 2-3 sentences about what the project does
- **Glossary** - Key terms and concepts (Agent, Environment, Session, Task, etc.)
- **Domain Map** - List of domains with 1-line descriptions and links to their features
- **Feature Registry** - Table/list of all features grouped by domain, each with a 1-line summary and link to its `{feature}.md`

When creating/updating a feature doc, also update `docs/README.md` to include the feature in the registry. If `docs/README.md` doesn't exist yet, create it.

### Layer 2: Domain & Feature Folders

```
docs/
├── README.md                              # Layer 1 - Project index
├── {domain}/                              # Domain folder (e.g., agents, tasks, mcp_integration)
│   └── {feature}/                         # Feature folder
│       ├── {feature}.md                   # Business logic (Layer 3a) - always required
│       ├── {feature}_tech.md              # Technical details (Layer 3b, optional)
│       ├── {feature}_widget.md            # Widget docs (Layer 3c, optional)
│       ├── {aspect}.md                    # Other aspect docs (optional)
│       └── {aspect}_tech.md              # Aspect technical details (optional)
├── development/                           # Development guides (LLM-targeted)
│   └── backend/
│       └── {topic}.md                     # LLM reference doc (concise, project-specific only)
```

**Domain examples:** `agents`, `tasks`, `file_management`, `mcp_integration`, `email_integration`, `agent_environments`, `credentials`, `knowledge`, `realtime`, `a2a`, `development`

**Feature folder naming:** Use snake_case, descriptive names (e.g., `agent_sessions`, `file_upload`, `mcp_connector`, `smart_schedule`)

### Layer 3: Feature Documentation Files

#### 3a. Business Logic: `{feature}.md`

Primary file. Explains WHAT the feature does and WHY, from a product/business perspective. An agent reading this should understand the feature's purpose, user stories, and behavioral rules without needing to look at code.

**Required sections:**
1. **Purpose** - 1-2 sentences: what the feature does for the user
2. **Core Concepts** - Key terms/entities specific to this feature
3. **User Stories / Flows** - Numbered steps showing how users interact with the feature
4. **Business Rules** - Constraints, state machines, lifecycle rules, validation logic
5. **Architecture Overview** - Simple text diagram showing component flow
   ```
   User → Frontend → Backend API → Service → External System
   ```
6. **Integration Points** - How this feature connects to other features (with links to their docs)

**Style:** Concise bullets, no code blocks, focus on behavior and rules.

#### 3b. Technical Details: `{feature}_tech.md`

Deep-dive for developers. Explains HOW the feature is implemented with file references.

**Required sections:**
1. **File Locations** - All files related to this feature, grouped by layer:
   - Backend: models, routes, services, utils
   - Frontend: components, hooks, routes, utils
   - Migrations, configs, tests
2. **Database Schema** - Table names, key fields, relationships (reference migration files, no SQL code)
3. **API Endpoints** - Route file paths + endpoint signatures
4. **Services & Key Methods** - Service file paths + method names with brief purpose
5. **Frontend Components** - Component paths + what they render/manage
6. **Configuration** - Settings keys, env vars relevant to this feature
7. **Security** - Access control, validation, encryption relevant to this feature

**Style:** Heavy use of file path references like `backend/app/services/file_service.py:upload_files()`. NO code blocks - only file/method references.

#### 3c. Widget Documentation: `{feature}_widget.md` (optional)

Documents specific UI widgets/wizards related to the feature. Only create if the feature has non-trivial widgets worth documenting separately.

**Sections:**
1. **Widget Purpose** - What the widget does
2. **User Flow** - Step-by-step interaction
3. **Component Structure** - Component file paths and hierarchy
4. **State Management** - What state the widget manages, hooks used
5. **API Interactions** - Which endpoints the widget calls

#### 3d. Aspect Documents: `{aspect_name}.md` (optional)

For complex features that need additional documentation on specific aspects (e.g., `recovery.md`, `scheduling.md`, `docker_setup.md`). These are supplementary to the main business logic and tech files.

Aspects can also have their own tech files following the same pattern: `{aspect_name}_tech.md`. Use this when an aspect has enough technical depth to warrant separating business logic from implementation details, just like the main feature files.

#### 3e. LLM-Targeted Documentation: `{name}_llm.md` (optional)

Documentation files suffixed with `_llm.md` are intended **exclusively for LLM consumption**, not for humans. These files serve as concise reference sheets that help LLMs work with project-specific patterns, conventions, and structures.

**Key principles:**
- **Concise, not explanatory** - No verbose explanations, tutorials, or background context. LLMs already know common programming patterns, frameworks, and libraries.
- **Project-specific data only** - Focus exclusively on information the LLM cannot infer: project conventions, custom patterns, file locations, naming rules, config quirks, domain-specific mappings.
- **Skip common knowledge** - Don't explain what FastAPI is, how SQLModel works, or standard REST conventions. Only document where this project deviates from or extends common patterns.
- **Dense reference format** - Bullet points, tables, path lists. Optimize for token efficiency.

**Example:** `docs/development/backend/ai_functions_development.md` could document project-specific patterns for implementing AI-related backend functions — custom decorators, service conventions, model naming — without explaining Python or FastAPI basics.

### Minimal Documentation (Simple Features)

Not every feature needs the full 3-layer treatment. For simple features or standalone topics, a single `{feature}.md` description file is sufficient. The `_tech.md` file is **optional** — only create it when the feature has enough technical depth (multiple files, complex flows, non-obvious implementation details) to warrant a separate technical reference. Similarly, some documentation may just be a descriptive document without the full required sections structure.

**Guideline:** Start with a single `{feature}.md`. Add `_tech.md` only if technical details would clutter the main doc or if the feature spans many files.

## Documentation Style Rules

**DO:**
- Use file path references: `backend/app/services/file_service.py:upload_files_to_agent_env()`
- Use endpoint references: `POST /api/v1/files/upload` - Upload file (creates temporary record)
- Use component references: `frontend/src/components/Chat/FileUploadModal.tsx`
- Link to related feature docs: `See [Agent Sessions](../agents/agent_sessions/agent_sessions.md)` <!-- nocheck -->
- Use concise bullet points
- Use simple text architecture diagrams

**DON'T:**
- Include actual code snippets
- Write tutorial-style instructions
- Duplicate information between business and tech files
- Over-explain obvious things

## Process

1. **Determine scope**: Is this a new feature doc, or refactoring an existing one?
2. **Identify domain and feature name**: Map to the correct `docs/{domain}/{feature}/` path
3. **Explore codebase**: Read relevant models, services, routes, components to understand the feature
4. **Write/split documentation**:
   - Always create `{feature}.md` (business logic / description)
   - Create `{feature}_tech.md` only if the feature has enough technical depth (multiple files, complex flows, non-obvious implementation)
   - Create `{feature}_widget.md` only if there are notable widgets
   - Create aspect files only if needed for complex sub-topics
   - Create `{name}_llm.md` when the target audience is LLMs, not humans (e.g., development guides, coding conventions)
5. **Update `docs/README.md`**: Add/update the feature entry in the registry
6. **If refactoring**: After creating new files, note which old files were replaced (don't delete them automatically - report to user)
7. **Verify references**: Run the docs reference checker on all created/updated files:
   ```
   python3 .runnerkit/scripts/check_docs_references.py --files docs/{domain}/{feature}/{feature}.md docs/{domain}/{feature}/{feature}_tech.md
   ```
   If broken references are found, fix them before finishing. Common issues:
   - Typos in file paths (verify paths exist with glob/grep)
   - References to files that were renamed or moved
   - Links to other feature docs that don't exist yet (use placeholder comment: `<!-- TODO: create {feature} docs -->` next to the link)

   **Direction/pattern references** — some references intentionally point to non-existent files used as naming conventions or architectural directions (e.g. "store your service in `backend/app/services/my_service.py`"). Handle these two ways: <!-- nocheck -->
   - **Automatic**: path segments starting with `your_`, `$`, or `entit` (entity/entities/EntityCard etc.) are skipped automatically — no annotation needed.
   - **Manual**: for other direction references (e.g. `backend/app/crud.py` as a deprecated convention), append `<!-- nocheck -->` to that line so the checker ignores it.

## General Assistant Knowledge Sync

After creating or updating documentation, run the GA knowledge sync to keep the General Assistant environment template in sync:

```
python3 .runnerkit/scripts/sync_ga_knowledge.py
```

This script:
1. Copies all non-`_tech` `.md` files from `docs/application/` and `docs/agents/` into the GA template's `knowledge/platform/` directory
2. Auto-generates REST API reference files from `frontend/openapi.json` (grouped by tag) into `knowledge/platform/api_reference/`

**Do NOT manually write `_api.md` files** — API reference is auto-generated from the OpenAPI spec. If API routes change, run `make gen-client` first (to update `openapi.json`), then `make sync-ga-knowledge`.

## Output

Write documentation to `docs/{domain}/{feature}/` following the structure above.
Report what was created/updated, any old files that can be removed, and the reference check results.
Remind the user to run `python3 .runnerkit/scripts/sync_ga_knowledge.py` if documentation was created or updated.
