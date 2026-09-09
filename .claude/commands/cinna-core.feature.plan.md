---
description: Create comprehensive architectural implementation plan for a new feature in the cinna-core project.
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty). This is the feature description that you'll create an implementation plan for.

## Context

You are working with a **Full Stack FastAPI + React application** (cinna-core). Feature implementation plans are comprehensive architectural documents that describe HOW to implement features.

### Document Organization
- **Location**: `drafts/` for implementation plans
- **Format**: Single markdown file with complete implementation architecture
- **Naming**: `[feature-name-kebab-case]_plan.md`
- **Purpose**: Blueprint for developers/LLMs to implement the feature step-by-step

## Prerequisites

Before running this command, you must:
1. **Have a clear feature description** from user input
2. **Understand the feature scope** - what's included in MVP vs future enhancements

## Required Documentation Reading

Before creating the implementation plan, you MUST read the following documentation files to understand the existing architecture:

### Always Read (Core Architecture)
1. **`CLAUDE.md`** - Project overview, technology stack, development patterns
2. **`docs/README.md`** - Feature registry, domain map, glossary — use this to identify which features are relevant to the plan

### Conditionally Read (Based on Feature Requirements)

After reading `docs/README.md`, identify which existing features the new feature touches and read their documentation. Follow the business-logic-first approach from CLAUDE.md:

- Read the **business logic file** (`feature_name.md`) first to understand what each related feature does
- Read the **tech file** (`feature_name_tech.md`) only when you need implementation details

Common domains to check:
- **`docs/application/user_workspaces/`** - If feature involves workspaces or multi-tenancy
- **`docs/agents/agent_credentials/`** - If feature involves sensitive data or encryption
- **`docs/agents/agent_environment_core/`** - If feature involves agent tools or MCP servers
- **`docs/application/agent_sessions/`** - If feature involves sessions or chat
- **`docs/application/realtime_events/`** - If feature involves WebSocket or streaming
- **`docs/development/backend/backend_development_llm.md`** - For backend implementation patterns
- **`docs/development/frontend/frontend_development_llm.md`** - For frontend implementation patterns
- Any other relevant documentation identified from the feature registry

**Important**: Only read conditionally required docs if they're relevant. Don't waste tokens on unrelated documentation.

## Execution Flow

The text the user typed after the command in the triggering message **is** the feature description.

Given that feature description, follow these steps:

### 1. Understand the Feature Request

a. **Parse user description** from User Input:
   - If empty or unclear: Ask for clarification with specific questions
   - Extract key requirements, constraints, and desired behavior
   - Identify which existing systems this feature touches

b. **Generate feature name** (kebab-case):
   - Analyze the feature description and extract the most meaningful keywords
   - Create a 2-5 word kebab-case name that captures the essence
   - Examples:
     - "Notification system for agent events" → "agent-event-notifications"
     - "User workspace billing integration" → "workspace-billing-integration"
     - "Real-time agent logs streaming" → "agent-logs-streaming"

c. **Determine scope**:
   - What's included in the initial implementation (MVP)?
   - What should be excluded for future enhancements?
   - Are there any technology preferences or constraints?

### 2. Read Required Documentation

a. **Always read**:
   - `CLAUDE.md` - For project structure and patterns
   - `docs/README.md` - For feature registry and domain map

b. **Conditionally read** based on feature requirements:
   - Identify related features from the domain map in `docs/README.md`
   - Read business logic docs first, then tech docs only if needed
   - Check `docs/development/` for backend/frontend implementation patterns

c. **Extract patterns**:
   - Note existing approaches you should follow
   - Identify similar features to reference
   - Understand security patterns (encryption, access control)
   - Note database patterns (UUID keys, foreign keys, cascade behavior)
   - Understand API patterns (dependency injection, error handling)

### 3. Set Output Path

a. **Determine output location**:
   ```
   OUTPUT_PATH = docs/drafts/[feature-name]_plan.md
   ```

b. **Check for existing plan**:
   - If file already exists, you're updating an existing plan
   - If not, you're creating a new plan

### 4. Design the Architecture

Now create the comprehensive implementation plan. This is a **technical architecture document**, NOT business requirements.

a. **Think like a software architect**:
   - Focus on HOW to implement, not WHAT or WHY
   - Design data models, API endpoints, services, UI components
   - Consider security, performance, extensibility
   - Reference existing patterns from the codebase
   - Plan for error handling and edge cases

b. **Look for similar existing features** (if relevant):
   - Check the feature registry in `docs/README.md` for related features
   - Browse relevant domain directories (`docs/agents/`, `docs/application/`) for documentation
   - Identify patterns to follow for consistency
   - Don't automatically read other docs - ask user if they want integration analysis

c. **Make architectural decisions**:
   - Make informed decisions based on FastAPI + React best practices
   - Follow existing patterns from the codebase (from CLAUDE.md and business_logic.md)
   - Document any assumptions made

### 5. Write the Architectural Plan

Write a comprehensive markdown document to `OUTPUT_PATH` that includes ALL of the following sections:

#### Required Sections in the Document

1. **Overview** (2-3 sentences):
   - Brief summary of the feature
   - Core capabilities (bullet points)
   - High-level flow diagram (ASCII or mermaid)

2. **Architecture Overview**:
   - System components diagram
   - Data flow illustration
   - Integration points with existing systems

3. **Data Models**:
   For each new database table, specify:
   - Table name and purpose
   - All fields with types, constraints, defaults
   - Relationships (foreign keys, cascade behavior: CASCADE, SET NULL, etc.)
   - Indexes for performance (specify which columns)
   - Security considerations (encryption fields, access control rules)
   - Lifecycle states if applicable (e.g., pending → active → error)

4. **Security Architecture**:
   - Encryption approach (reference existing `backend/app/core/security.py` patterns)
   - Access control rules (ownership-based, permission-based, admin access)
   - Input validation and sanitization requirements
   - Rate limiting considerations
   - Sensitive data handling (what gets logged, what gets exposed in APIs)

5. **Backend Implementation**:

   **API Routes**:
   - Endpoint paths and HTTP methods (GET, POST, PUT, DELETE)
   - Request/response schemas (describe field names and types)
   - Dependencies (SessionDep, CurrentUser, admin guards)
   - Authorization checks (who can access what)
   - Background task triggers (if any)

   **Service Layer**:
   - Service class names and file locations (e.g., `backend/app/services/notification_service.py`) <!-- nocheck -->
   - Key methods with signatures and descriptions
   - Business logic encapsulation
   - Integration with existing services
   - Error handling patterns

   **Background Tasks** (if needed):
   - Task descriptions
   - Trigger mechanisms
   - Idempotency considerations
   - Error recovery strategies

6. **Frontend Surfaces** (inventory, not design):

   Composition — which user story a surface serves, whether it is a card, dialog, sheet or route, how dense it is, which house pattern it reuses — is decided later by `cinna-core.ui.design`, which appends a `## UI Specification` to this plan. Do **not** prescribe card layouts, section lists, or control placement here; a plan that says "one card with sections A, B, C" gets built exactly like that.

   For each surface the feature adds or changes, list only:
   - Name, host (which route / tab), and whether it exists today (file path)
   - The user's intent in one sentence ("the admin wants to see who is invited and resend one")
   - The data it shows and the actions it offers (as a list of verbs, not as buttons)
   - The API it consumes (endpoints + generated service names) and any contract gap (e.g. a total count is needed for "Show all (N)")
   - Files in `docs/development/frontend/ui_ux_guidelines.md` §4 (anti-patterns) that the feature will touch

   **State Management**:
   - React Query usage (query keys, mutations, cache invalidation)
   - Context providers if needed
   - localStorage or sessionStorage usage

7. **Database Migrations**:
   - Migration file naming convention (e.g., `add_notifications_table.py`)
   - Tables to create/modify
   - Indexes to add (specify columns and type: btree, unique, etc.)
   - Foreign key constraints (specify ON DELETE behavior)
   - Downgrade strategy (how to rollback)

8. **Knowledge Repository Format** (if applicable):
   - File structure requirements
   - Configuration file schemas
   - Validation rules

9. **Error Handling & Edge Cases**:
   - Common failure scenarios
   - Error messages and recovery flows
   - Validation failures (what to check)
   - Network/API failures
   - Data integrity issues
   - User permission issues

10. **UI/UX Considerations**:
    - Only what the UI designer cannot infer from the data: status vocabulary and its colours, copy that carries business meaning, onboarding sequencing constraints, accessibility requirements specific to the feature
    - Everything else (density, placement, disclosure, patterns) belongs to the `## UI Specification` produced by `cinna-core.ui.design`

11. **Integration Points**:
    - How feature connects to existing systems
    - API client regeneration steps (remind about `scripts/generate-client.sh`)
    - Agent-env tool extensions (if applicable)
    - Workspace integration (if applicable)

12. **Future Enhancements (Out of Scope)**:
    - Features intentionally excluded from initial implementation
    - Extensibility points for future work

13. **Summary Checklist**:
    Organize as actionable tasks:
    - **Backend tasks**: Specific, actionable items (e.g., "Create notifications table with fields...", "Add API endpoint POST /api/v1/notifications")
    - **Frontend tasks**: Specific, actionable items (e.g., "Create NotificationBell component", "Add notification preferences to Settings page")
    - **Agent-env tasks**: If applicable (e.g., "Update agent-env to include notification_api_key")
    - **Testing & validation tasks**: What to test, not how (e.g., "Verify notifications are created when agent completes", "Test notification preferences save correctly")

### 6. Quality Validation

After writing the plan, validate it:

a. **Review against checklist**:
   - ✅ All required sections present
   - ✅ Data models have detailed field specifications
   - ✅ Security considerations addressed
   - ✅ API endpoints clearly defined
   - ✅ Frontend surfaces inventoried (intent, data, actions, API) — composition left to `cinna-core.ui.design`
   - ✅ Database migrations planned
   - ✅ Error handling covered
   - ✅ Implementation checklist is actionable
   - ❌ Does NOT contain actual implementation code
   - ❌ Does NOT contain generated tests
   - ❌ Does NOT include performance benchmarks

b. **Ensure references to existing code**:
   - References similar files in the codebase
   - Follows established patterns from CLAUDE.md
   - Integrates with existing authentication/encryption/services
   - Considers workspace permissions (if applicable)

c. **Check for completeness**:
   - Another developer could implement from this plan
   - All integration points identified
   - Security considered at every layer
   - Extensibility points noted for future enhancements

### 7. Report Completion

Report completion with:

**Summary**:
- Feature name: `[feature-name]`
- Document path: `[OUTPUT_PATH]`
- Status: ✓ Complete

**What's Included**:
- Complete architecture overview
- Detailed data models with all fields
- API endpoint specifications
- Frontend component structure
- Security architecture
- Implementation checklist

**Next Steps**:
- Review the architectural plan at `[OUTPUT_PATH]`
- Clarify any unclear requirements if needed
- If the plan lists frontend surfaces, run `cinna-core.ui.design` on it before implementation
- Use this plan as a blueprint for implementation
- Remember to regenerate API client after backend changes: `bash scripts/generate-client.sh`

**Key Integration Points**:
- [List key files/systems this feature integrates with]
- [Mention if workspace permissions need consideration]
- [Note if encryption/credentials are involved]

## Critical Guidelines

### DO:
- ✅ Read and reference existing documentation files
- ✅ Follow existing architectural patterns from the codebase
- ✅ Create detailed data models with all fields, types, and constraints
- ✅ Design API endpoints with request/response schemas
- ✅ Plan security architecture (encryption, access control, validation)
- ✅ Describe UI components and user flows
- ✅ Reference existing files and patterns (e.g., "similar to `frontend/src/routes/_layout/admin.tsx`") <!-- nocheck -->
- ✅ Consider workspace permissions and multi-tenancy if applicable
- ✅ Plan database migrations with indexes and foreign keys
- ✅ Create implementation checklists for backend, frontend, and testing
- ✅ Think about error handling and edge cases
- ✅ Design for extensibility and future enhancements

### DON'T:
- ❌ Write actual code (only describe what code should do)
- ❌ Generate tests (only outline what should be tested)
- ❌ Include performance benchmarks (mention optimization strategies only)
- ❌ Skip security considerations
- ❌ Ignore existing patterns (always reuse established approaches)
- ❌ Create documentation for the feature itself (focus on implementation plan)

### Focus on Implementation, Not Business

This is a **technical architecture document**, NOT a business specification:

- Focus on **HOW** to implement, not WHAT or WHY
- Include technical details: data models, API endpoints, services, components
- Describe implementation approach, file locations, method signatures
- Reference existing code patterns and infrastructure
- Written for developers/LLMs who will implement the feature
- Think like a software architect, not a business analyst

### FastAPI + React Patterns

**Backend Patterns**:
- UUID primary keys for all tables
- SQLModel for database models (inherits from SQLModel base, `table=True`)
- Alembic for database migrations
- Dependency injection: SessionDep, CurrentUser
- Services in `backend/app/services/` for business logic
- CRUD operations follow established patterns
- Error handling with HTTPException
- Background tasks for async operations

**Frontend Patterns**:
- TanStack Router for file-based routing
- TanStack React Query for state management
- Protected routes use `_layout/` prefix
- Auto-generated API client from OpenAPI spec (never manually edit)
- Forms use react-hook-form + zod validation
- Tailwind CSS + shadcn/ui components for styling

**Security Patterns**:
- Encryption using Fernet (symmetric) via `backend/app/core/security.py`
- JWT authentication with CurrentUser dependency
- Workspace-based permissions (if applicable)
- Input validation on all API endpoints
- Sensitive data encrypted at rest

**Database Patterns**:
- Foreign keys with CASCADE or SET NULL
- Indexes on frequently queried columns
- created_at, updated_at timestamps
- Soft deletes with is_active/is_deleted flags (if applicable)

## Example Reference

For examples of well-structured feature documentation, browse the feature directories under:
- `docs/agents/` — agent-domain features (e.g., `agent_environments/`, `agent_credentials/`)
- `docs/application/` — platform features (e.g., `agent_sessions/`, `email_integration/`)

Each feature directory contains business logic and tech files that demonstrate the expected level of detail.

## Notes

- This command creates a **complete architectural implementation plan**
- The plan should be comprehensive enough for another developer/LLM to implement
- Focus on technical details, not business justification
- Always consider security, workspace permissions, and extensibility
- Reference existing patterns and infrastructure extensively
