---
description: Create or update the technical specification for a workflow based on the completed business specification.
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty). User input can be used to:
- Provide technical guidance or preferences
- Resolve technical clarifications
- Refine existing technical specification

## Context

You are working with a **workflow generation project** for AI-driven automation in a cloud environment. This command creates the **high-level technical specification** (`tech.md`) based on the completed business specification (`spec.md`).

**Environment capabilities**:
- **Runtime**: Isolated Docker containers
- **Language**: Python-only environment
- **Services**: Databases, credentials management, scheduler
- **Knowledge base**: Reference to integrations, approaches, tested solutions

**Workflow structure** (based on `templates/blank_template_workflow`):
- Workflows: `workflows/<workflow-name>/`
- Business spec: `workflows/<workflow-name>/docs/spec.md` (must exist and be complete)
- **Technical spec**: `workflows/<workflow-name>/docs/tech.md` (to be created or updated) - **HIGH-LEVEL ARCHITECTURE**
- Implementation plan: `workflows/<workflow-name>/docs/plan.md` (created later with `/workflowkit.plan`) - DETAILED SPECIFICATIONS
- Note: tech.md describes high-level architecture only; does NOT specify exact file structures or API contracts (those go in plan.md)

## Prerequisites

Before running this command:
1. **Business spec must exist**: `spec.md` must be present
2. **Business spec must be complete**: No [NEEDS CLARIFICATION] markers remaining
3. **Business requirements are finalized**: No pending business decisions

## Outline

This command transforms business requirements into **high-level technical architecture** that describes WHAT to build and WHY, without detailed implementation specifics. The detailed HOW goes into plan.md (created later).

### Execution Steps

#### 1. Locate and Validate Business Specification

a. **Determine the workflow to techify**:
   - If user provided workflow name in arguments, use it: `workflows/<workflow-name>/`
   - Otherwise, list available workflows in `workflows/` directory and ask user to select

b. **Set paths**:
   ```
   WORKFLOW_DIR = workflows/<workflow-name>
   SPEC_FILE = workflows/<workflow-name>/docs/spec.md
   TECH_FILE = workflows/<workflow-name>/docs/tech.md
   AGENT_FILE = workflows/<workflow-name>/AGENT.md
   ENTRYPOINT_FILE = workflows/<workflow-name>/ENTRYPOINT.md
   ```

c. **Validate business spec**:
   - If `SPEC_FILE` doesn't exist: ERROR "Run `/workflowkit.specify` first to create business specification"
   - Load `SPEC_FILE` and scan for [NEEDS CLARIFICATION] markers
   - If [NEEDS CLARIFICATION] found: ERROR "Business spec has unresolved clarifications. Run `/workflowkit.clarify` first"
   - Check if mandatory business sections are complete

d. **Check for existing tech spec**:
   - If `TECH_FILE` exists: You're updating/refining an existing technical spec
   - If not: You're creating a new technical spec from scratch

#### 2. Load Templates and Business Context

a. **Load technical template**:
   - Read `.workflowkit/templates/tech-template.md`
   - Understand the structure and required sections

b. **Extract business requirements**:
   - Read and parse `SPEC_FILE` completely
   - Extract key information:
     - Problem statement and expected results
     - User scenarios and workflows
     - Functional requirements
     - Success criteria
     - Key entities (if data is involved)

c. **Load knowledge base** (if available):
   - Search for relevant integration examples
   - Reference tested solutions for similar problems
   - Identify reusable patterns

#### 3. Design Technical Architecture (HIGH-LEVEL)

Based on business requirements, design the **high-level technical approach** (detailed specs go into plan.md later):

a. **Determine workflow components**:
   - **Data models**: What entities need to be managed (created, updated, queried)
   - **External integrations**: Which APIs, services, or systems to connect
   - **Processing steps**: High-level pipeline stages (extract → transform → load, etc.)
   - **Scheduling**: When and how often the workflow runs

b. **Define data structure approach**:
   - Entity schemas (field names and types at high level)
   - Relationships between entities
   - Data validation strategy
   - Storage requirements (database type, file storage, etc.)

c. **Plan integration points**:
   - **External APIs**: Which services to integrate (email, ERP, etc.)
   - **Authentication**: How to handle credentials
   - **Rate limits**: Strategy for API quotas
   - **Error handling**: Approach for failed external calls

d. **Design workflow execution**:
   - **Trigger mechanism**: Manual, scheduled (cron), or event-driven
   - **State management**: How to track workflow progress
   - **Retry strategy**: How to handle failures
   - **Logging approach**: What to log for monitoring

e. **Performance considerations**:
   - Expected data volume
   - Processing time constraints
   - Concurrency needs
   - Resource limits (memory, CPU)

#### 4. Document Technical Assumptions and Clarifications

As you design the technical solution, track:

- **Technical decisions**: Choices made about implementation (to be shown in summary)
  - Example: "Using PostgreSQL for structured data storage (vs. file-based storage)"
  - Example: "REST API for ERP integration (vs. direct database access)"
  - Example: "Daily scheduled execution (vs. real-time event-driven)"

- **Technical clarifications needed**: Mark with [TECH CLARIFICATION: question] if:
  - Multiple valid technical approaches exist with different tradeoffs
  - The choice significantly impacts performance, maintainability, or cost
  - Integration approach is unclear
  - **LIMIT: Maximum 5 [TECH CLARIFICATION] markers total**

**Examples of valid technical clarifications**:
- "Should we use PostgreSQL or MongoDB? (structured vs. flexible schema tradeoff)"
- "Should we process emails in batches or one-by-one? (performance vs. memory usage)"
- "Should we use IMAP or Gmail API? (complexity vs. reliability tradeoff)"

**Examples of invalid technical clarifications** (should be decided based on best practices):
- "Should we use Python? (already specified in environment)"
- "Should we handle errors? (always yes)"
- "Should we log execution? (always yes for monitoring)"

#### 5. Resolve User Input (if provided)

If user provided arguments:
- Check if they're answering pending [TECH CLARIFICATION] markers
- Check if they're providing technical guidance/preferences
- Check if they're requesting specific technical approaches
- Incorporate this input into the technical design

#### 6. Generate High-Level Technical Specification

Write the **high-level** technical specification to `TECH_FILE` using the template structure:

a. **Fill YAML front matter**:
   ```yaml
   ---
   name: [workflow-name-in-kebab-case]
   description: [One-sentence description from business spec]
   runtime: docker-python
   integrations: [email, erp, api-name]
   schedule: [manual|cron|event-driven]
   data_storage: [postgres|mongodb|files|none]
   ---
   ```

b. **Complete all technical sections at HIGH-LEVEL**:
   - Metadata (workflow name, version, reference to plan.md)
   - Architecture Overview (diagram/description of components)
   - Data Models (entities and relationships - not exact schemas)
   - External Integrations (which systems, authentication strategy)
   - Workflow Execution (trigger, steps, error handling strategy)
   - State Management (how to track progress)
   - Dependencies (Python packages, external services)
   - Performance Considerations (volume, limits, optimization strategy)
   - Security Considerations (credential handling, data protection)
   - Testing Considerations (what to test - not exact test code)

c. **Maintain traceability**:
   - Each technical element should map back to a business requirement
   - Reference business spec sections where relevant
   - Explain technical decisions in context of business needs
   - Focus on **WHY** choices were made, not **HOW** to implement them

d. **Follow Python/workflow best practices**:
   - Use standard Python naming conventions
   - Reference proven patterns (ETL, event-driven, etc.)
   - Note that exact code, class names, and detailed configs go in plan.md

e. **Update AGENT.md with architectural details**:

   Read the current `AGENT_FILE` and update the following sections with information from tech.md:

   - **Data (`databases/`)**: Update with database type and high-level entity descriptions
     ```markdown
     ### Data (`databases/`)
     **Database Type**: [PostgreSQL/MongoDB/SQLite/Files/None]

     **Entities Managed**:
     - [EntityName]: [Purpose and what data it stores]
     - [EntityName]: [Purpose and what data it stores]
     ```

   - **Tools and Capabilities**: Add Python packages and external integrations
     ```markdown
     ## Tools and Capabilities

     **Python Packages**:
     - [package-name]: [Purpose - e.g., "for email parsing"]
     - [package-name]: [Purpose - e.g., "for ERP API integration"]

     **External Integrations**:
     - [Service Name] API: [What it provides - e.g., "mailbox access"]
     - [Service Name] API: [What it provides - e.g., "ERP bill updates"]

     **Authentication**:
     - [How credentials are managed - e.g., "credentials stored in secure vault"]
     ```

   - **Success Criteria**: Add measurable workflow execution criteria
     ```markdown
     ## Success Criteria

     A successful workflow execution means:
     - [Criterion 1 from tech.md - e.g., "All emails processed without errors"]
     - [Criterion 2 from tech.md - e.g., "ERP updated within 5 minutes of email receipt"]
     - [Criterion 3 from tech.md - e.g., "Logs contain complete execution trace"]
     ```

f. **Update ENTRYPOINT.md with execution context**:

   Update the `ENTRYPOINT_FILE` with information about what context is provided when the workflow runs:

   ```markdown
   ## Context Provided on Execution

   **User context**:
   - [What user-specific information is available - e.g., "User email credentials from vault"]
   - [Other user context - e.g., "User timezone preferences"]

   **Temporal context**:
   - [Time-based context - e.g., "Last execution timestamp"]
   - [Date range - e.g., "Process emails from last 24 hours"]

   **Data context**:
   - [What data is available - e.g., "Existing bills database for deduplication"]
   - [Input files - e.g., "Configuration file with ERP connection details"]
   ```

g. **Save updated files**:
   - Write TECH_FILE (tech.md)
   - Write AGENT_FILE (AGENT.md)
   - Write ENTRYPOINT_FILE (ENTRYPOINT.md)

#### 7. Technical Specification Quality Validation (High-Level)

After writing the technical spec, validate it:

a. **Completeness check**:
   - All data models identified with their purpose
   - All external integrations specified
   - Execution trigger defined
   - Error handling strategy outlined
   - Dependencies listed with rationale

b. **High-level consistency check**:
   - Architecture aligns with environment constraints (Python, Docker)
   - Standard patterns referenced (not reinventing wheels)
   - Resource limits considered

c. **Traceability check**:
   - All business requirements addressed at architectural level
   - User scenarios have corresponding technical components
   - Success criteria can be validated with design

d. **Architectural decisions check**:
   - Key technical choices are explained with **WHY**
   - Trade-offs are documented
   - References to detailed implementation say "see plan.md"

#### 8. Handle Technical Clarifications

If [TECH CLARIFICATION] markers remain:

a. **Extract all markers** (max 5)

b. **For each clarification**, present options to user:

   ```markdown
   ## Technical Question [N]: [Topic]

   **Context**: [Relevant business requirement this supports]

   **What we need to decide**: [Specific technical question]

   **Recommended Approach**: Option [X] - [Reasoning based on best practices, performance, maintainability]

   **Options**:

   | Option | Approach | Pros | Cons | Impact |
   |--------|----------|------|------|--------|
   | A | [Technical approach A] | [Benefits] | [Drawbacks] | [Cost/Performance/Complexity] |
   | B | [Technical approach B] | [Benefits] | [Drawbacks] | [Cost/Performance/Complexity] |
   | C | [Technical approach C] | [Benefits] | [Drawbacks] | [Cost/Performance/Complexity] |
   | Custom | Specify your preference | [Explain] | [Explain] | [Depends on choice] |

   **Your choice**: _[Wait for user response]_
   ```

c. **Present all questions together** before waiting for responses

d. **Wait for user responses** (e.g., "Q1: A, Q2: B, Q3: Custom - use PostgreSQL")

e. **Update tech spec** by replacing [TECH CLARIFICATION] markers with chosen approaches

f. **Re-validate** after all clarifications resolved

#### 9. Report Completion

Report completion with:

**Summary**:
- Workflow name: `[workflow-name]`
- Business spec: `[WORKFLOW_DIR]/docs/spec.md` ✓
- Technical spec: `[WORKFLOW_DIR]/docs/tech.md` ✓ Created/Updated
- Agent system prompt: `[WORKFLOW_DIR]/AGENT.md` ✓ Updated
- Entry point: `[WORKFLOW_DIR]/ENTRYPOINT.md` ✓ Updated
- Validation status: ✓ Passed / ⚠ Warnings / ✗ Failed

**Technical Decisions Made**:

If any significant technical decisions were made, document them:

```markdown
Technical Implementation Decisions:

The following technical approaches were chosen based on best practices:

1. **[Decision category]**: [Approach chosen]
   - Reasoning: [Why this approach]
   - Tradeoff: [What was considered vs. what was chosen]
   - Impact: [Performance/cost/complexity impact]

2. **[Decision category]**: [Approach chosen]
   - Reasoning: [Why this approach]
   - Tradeoff: [What was considered vs. what was chosen]
   - Impact: [Performance/cost/complexity impact]

[Continue for all significant technical decisions]
```

**Key Technical Details**:
- Data models: [count]
- External integrations: [list services]
- Execution trigger: [manual/cron/event-driven]
- Dependencies: [list Python packages]

**Implementation Readiness**:
- ✓ Architecture defined
- ✓ Data models specified
- ✓ Integrations identified
- ✓ Execution strategy outlined
- ✓ Performance considerations documented
- ✓ Testing approach defined

**Next Steps**:
- Review technical architecture and decisions above
- **Create plan.md**: Run `/workflowkit.plan` to generate detailed implementation plan
- Implementation will follow the detailed plan.md

## Guidelines

### Technical Specification Focus (High-Level)

The technical spec (tech.md) should be:

1. **Architectural**: Focus on WHAT and WHY, not HOW
2. **High-level**: Component names, purposes, interactions - not every implementation detail
3. **Strategic**: Explain technical choices and trade-offs
4. **Traceable**: Maps business requirements to technical components
5. **Following patterns**: Reference standard Python/workflow patterns

**Relationship to plan.md**:
- tech.md = High-level architecture (this document)
- plan.md = Detailed implementation specifications (created next with `/workflowkit.plan`)

### What to Include in tech.md

**DO include in tech.md**:
- Data model names and their **purpose** (`BillEntity` - for tracking dunning bills)
- Field names and types (`email_subject: str` - **why needed**)
- Integration points and their **purpose** (`ERP API` - to update bill status)
- Execution **trigger** (scheduled daily at 6 AM)
- Error handling **strategy** (retry with exponential backoff)
- Storage **approach** (PostgreSQL for structured bill data)
- Performance **considerations** (process emails in batches of 100)
- Testing **scope** (what scenarios to cover)

**DO NOT include in tech.md** (these go in plan.md):
- Exact class names (`class BillEntity(BaseModel)` - too detailed)
- Exact database schemas (`bill_id UUID PRIMARY KEY` - implementation detail)
- Exact API endpoints (`POST /api/v1/bills` - implementation detail)
- Function signatures, method parameters - implementation details
- Exact test method code
- Precise file structure (module organization)

**DO NOT include anywhere** (neither tech.md nor plan.md):
- Actual executable Python code (that's implementation, not specification)

### Technical Clarification Guidelines

Ask for technical clarification ONLY when:
1. **Multiple valid approaches** exist with different tradeoffs
2. **Significant impact** on performance, cost, or maintainability
3. **Integration approach** is unclear
4. **Cannot be resolved** by Python/workflow best practices

**Examples of when to ask**:
- Database choice when both relational and document store are viable
- Batch vs. streaming processing when both could work
- API authentication method when multiple options exist

**Examples of when NOT to ask**:
- Python naming conventions (follow PEP 8)
- Whether to use try/except (always handle errors)
- Whether to log (always log for monitoring)
- Standard patterns (use established patterns)

## Error Handling

**If business spec is incomplete**:
```
❌ Cannot create technical specification

Reason: Business specification has unresolved clarifications

Action required:
1. Open: [WORKFLOW_DIR]/spec.md
2. Review [NEEDS CLARIFICATION] markers
3. Run: /workflowkit.clarify to resolve them
4. Then run: /workflowkit.techify again
```

**If business spec is missing**:
```
❌ Cannot create technical specification

Reason: Business specification not found

Action required:
1. Run: /workflowkit.specify [workflow description]
2. Complete business specification
3. Then run: /workflowkit.techify
```

**If workflow directory is unclear**:
```
❓ Cannot determine workflow directory

Please specify the workflow name as an argument (e.g., /workflowkit.techify email-dunning-checker)
```

## Notes

- This command creates the **high-level technical architecture** (`tech.md`), not the detailed implementation plan or code
- tech.md describes WHAT to build and WHY architectural choices were made
- tech.md is followed by plan.md (created with `/workflowkit.plan`) which contains detailed specifications
- The workflow is: `spec.md` (business) → `tech.md` (architecture) → `plan.md` (detailed specs) → implementation
- All technical decisions should be traceable to business requirements
- Follow Python and workflow automation best practices
- Keep tech.md high-level - resist the urge to include implementation details
- tech.md should be understandable by technical leads reviewing architectural approach
- plan.md (created next) will contain the detailed specs for developers to implement
