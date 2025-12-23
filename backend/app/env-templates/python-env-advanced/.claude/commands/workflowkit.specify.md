---
description: Create or update the workflow specification from a natural language workflow description.
handoffs:
  - label: Techify Workflow
    command: workflowkit.techify
    prompt: Create technical spec for the workflow
  - label: Clarify Spec Requirements
    command: workflowkit.clarify
    prompt: Clarify specification requirements
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Outline

The text the user typed after `/workflowkit.specify` in the triggering message **is** the workflow description. Assume you always have it available in this conversation even if `$ARGUMENTS` appears literally below. Do not ask the user to repeat it unless they provided an empty command.

Given that workflow description, do this:

1. **Generate a concise workflow name** (2-4 words):
   - Analyze the workflow description and extract the most meaningful keywords
   - Create a 2-4 word name that captures the essence of the workflow
   - Use kebab-case format (e.g., "email-dunning-checker", "invoice-processor")
   - Preserve technical terms and acronyms (OAuth2, API, ERP, etc.)
   - Keep it concise but descriptive enough to understand the workflow at a glance
   - Examples:
     - "Check emails for dunning notifications" → "email-dunning-checker"
     - "Process invoices from ERP" → "invoice-processor"
     - "Sync customer data to database" → "customer-sync"

2. **Create workflow directory structure**:

   a. Determine workflow directory:
      - Workflow name: `<workflow-name>` (from step 1)
      - Workflow directory: `workflows/<workflow-name>/`
      - Docs directory: `workflows/<workflow-name>/docs/`
      - Spec file: `workflows/<workflow-name>/docs/spec.md`

   b. Copy the template structure:
      ```bash
      cp -r templates/blank_template_workflow workflows/<workflow-name>
      ```

      This creates the standard workflow structure from `templates/blank_template_workflow`:
      ```
      workflows/<workflow-name>/
      ├── databases/      # Database files and schemas
      ├── docs/
      │   ├── spec.md    # Business specification (to be written)
      │   ├── tech.md    # Technical specification (created later)
      │   └── plan.md    # Implementation plan (created later)
      ├── files/         # Input/output files for workflow
      ├── logs/          # Execution logs
      ├── scripts/       # Workflow implementation scripts
      └── README.md      # Workflow documentation
      ```

   c. Set paths for use in subsequent steps:
      - `WORKFLOW_NAME` = `<workflow-name>`
      - `WORKFLOW_DIR` = `workflows/<workflow-name>/`
      - `SPEC_FILE` = `workflows/<workflow-name>/docs/spec.md`
      - `AGENT_FILE` = `workflows/<workflow-name>/AGENT.md`
      - `ENTRYPOINT_FILE` = `workflows/<workflow-name>/ENTRYPOINT.md`

3. **Determine Workflow Execution Mode**:

   Analyze the workflow description to determine the execution mode:

   - **INTERACTIVE**: Responds to user requests in real-time
     - Examples: "tell me about my emails", "check my mailbox", "analyze recent invoices"
     - Indicators: user-facing language, reporting/querying verbs, conversational tone

   - **SCHEDULED**: Runs automatically on a schedule without user interaction
     - Examples: "process emails every hour", "update ERP daily", "sync data nightly"
     - Indicators: temporal keywords (daily, hourly, nightly), automated actions, batch processing

   - **HYBRID**: Can run both scheduled and on-demand
     - Examples: "process emails automatically, but also allow manual checks"
     - Indicators: both automated scheduling and user query capabilities

   If the execution mode is unclear from the description:
   - **Default to SCHEDULED** for automation-focused workflows (processing, syncing, updating)
   - **Default to INTERACTIVE** for query/reporting-focused workflows (checking, analyzing, telling)
   - Mark with `[NEEDS CLARIFICATION: Execution mode]` only if the choice significantly impacts workflow design
   - **Add to validation questions** if marked for clarification

4. **Create Initial Agent System Prompt (AGENT.md)**:

   Write initial content to `AGENT_FILE`:

   ```markdown
   # Workflow Agent System Prompt

   **Role**: You are an autonomous workflow execution agent responsible for [WORKFLOW_PURPOSE from description].

   **Execution Mode**: [INTERACTIVE/SCHEDULED/HYBRID from step 3]
   - **Interactive**: Responds to user requests in real-time
   - **Scheduled**: Runs automatically on a schedule without user interaction
   - **Hybrid**: Can run both scheduled and on-demand

   ## Your Responsibilities

   [High-level responsibilities extracted from workflow description]

   ## Available Resources

   ### Scripts (`scripts/`)
   [TO BE FILLED IN PLAN PHASE: List of Python scripts with descriptions]

   ### Data (`databases/`)
   [TO BE FILLED IN TECHIFY PHASE: Database schemas and tables]

   ### Files (`files/`)
   **Input Files**:
   [TO BE FILLED IN PLAN PHASE: Expected input files]

   **Output Files**:
   [TO BE FILLED IN PLAN PHASE: Output files to generate]

   ### Logs (`logs/`)
   - Write execution logs to track progress and errors
   - Use structured logging for easier debugging

   ## Execution Flow

   [TO BE FILLED IN PLAN PHASE: Step-by-step execution flow]

   ## Tools and Capabilities

   [TO BE FILLED IN TECHIFY PHASE: Python packages, APIs, integrations]

   ## Decision-Making Guidelines

   [TO BE FILLED IN CLARIFY PHASE: Rules for handling edge cases]

   ## Success Criteria

   [TO BE FILLED IN TECHIFY PHASE: What constitutes successful execution]

   ## Error Handling

   [TO BE FILLED IN PLAN PHASE: How to handle common errors]
   ```

5. **Create Initial Entry Point (ENTRYPOINT.md)**:

   Analyze the workflow description to generate an appropriate entry point prompt:

   a. **For INTERACTIVE mode**:
      - Extract the likely user query from the description
      - Example: "Check emails for dunning notifications" → Entry point: "Check my mailbox for dunning notifications and tell me what you found"

   b. **For SCHEDULED mode**:
      - Convert description to an automated action command
      - Example: "Process emails for dunning notifications" → Entry point: "Process all emails received in the last 24 hours and detect dunning notifications"

   c. **For HYBRID mode**:
      - Provide both interactive and scheduled entry points

   Write initial content to `ENTRYPOINT_FILE`:

   ```markdown
   # Workflow Entry Point

   **Trigger Type**: [INTERACTIVE/SCHEDULED from step 3]

   ## Entry Point Prompt

   [Generated entry point prompt based on execution mode]

   ### Examples

   **Interactive Mode** (if applicable):
   ```
   [Example user query from description]
   ```

   **Scheduled Mode** (if applicable):
   ```
   [Example automated trigger command]
   ```

   ## Context Provided on Execution

   [TO BE FILLED IN TECHIFY PHASE: What information is passed to agent]

   - **User context**: [e.g., user email, credentials, preferences]
   - **Temporal context**: [e.g., date range, time window]
   - **Data context**: [e.g., input files, database state]

   ## Expected Response

   **Interactive Mode** (if applicable):
   [TO BE FILLED IN PLAN PHASE: Expected response format for user-facing output]

   **Scheduled Mode** (if applicable):
   [TO BE FILLED IN PLAN PHASE: Expected output format for logging/monitoring]
   ```

6. Load `.workflowkit/templates/spec-template.md` to understand required sections.

7. Follow this execution flow:

    1. Parse user description from Input
       If empty: ERROR "No feature description provided"
    2. Extract key concepts from description
       Identify: actors, actions, data, constraints
    3. For unclear aspects:
       - Make informed guesses based on context and industry standards
       - Only mark with [NEEDS CLARIFICATION: specific question] if:
         - The choice significantly impacts feature scope or user experience
         - Multiple reasonable interpretations exist with different implications
         - No reasonable default exists
       - **LIMIT: Maximum 3 [NEEDS CLARIFICATION] markers total**
       - Prioritize clarifications by impact: scope > security/privacy > user experience > technical details
    4. Fill User Scenarios & Testing section
       If no clear user flow: ERROR "Cannot determine user scenarios"
    5. Generate Functional Requirements
       Each requirement must be testable
       Use reasonable defaults for unspecified details (document assumptions in Assumptions section)
    6. Define Success Criteria
       Create measurable, technology-agnostic outcomes
       Include both quantitative metrics (time, performance, volume) and qualitative measures (user satisfaction, task completion)
       Each criterion must be verifiable without implementation details
    7. Identify Key Entities (if data involved)
    8. Return: SUCCESS (spec ready for planning)

8. Write the specification to SPEC_FILE using the template structure, replacing placeholders with concrete details derived from the feature description (arguments) while preserving section order and headings.

9. **Specification Quality Validation**: After writing the initial spec, validate it against quality criteria:

   a. **Create Spec Quality Checklist**: Generate a checklist file at `FEATURE_DIR/checklists/requirements.md` using the checklist template structure with these validation items:

      ```markdown
      # Specification Quality Checklist: [FEATURE NAME]
      
      **Purpose**: Validate specification completeness and quality before proceeding to planning
      **Created**: [DATE]
      **Feature**: [Link to spec.md]
      
      ## Content Quality
      
      - [ ] No implementation details (languages, frameworks, APIs)
      - [ ] Focused on user value and business needs
      - [ ] Written for non-technical stakeholders
      - [ ] All mandatory sections completed
      
      ## Requirement Completeness
      
      - [ ] No [NEEDS CLARIFICATION] markers remain
      - [ ] Requirements are testable and unambiguous
      - [ ] Success criteria are measurable
      - [ ] Success criteria are technology-agnostic (no implementation details)
      - [ ] All acceptance scenarios are defined
      - [ ] Edge cases are identified
      - [ ] Scope is clearly bounded
      - [ ] Dependencies and assumptions identified
      
      ## Feature Readiness
      
      - [ ] All functional requirements have clear acceptance criteria
      - [ ] User scenarios cover primary flows
      - [ ] Feature meets measurable outcomes defined in Success Criteria
      - [ ] No implementation details leak into specification
      
      ## Notes

      - Items marked incomplete require spec updates before `/workflowkit.clarify` or `/workflowkit.techify`
      ```

   b. **Run Validation Check**: Review the spec against each checklist item:
      - For each item, determine if it passes or fails
      - Document specific issues found (quote relevant spec sections)

   c. **Handle Validation Results**:

      - **If all items pass**: Mark checklist complete and proceed to step 6

      - **If items fail (excluding [NEEDS CLARIFICATION])**:
        1. List the failing items and specific issues
        2. Update the spec to address each issue
        3. Re-run validation until all items pass (max 3 iterations)
        4. If still failing after 3 iterations, document remaining issues in checklist notes and warn user

      - **If [NEEDS CLARIFICATION] markers remain**:
        1. Extract all [NEEDS CLARIFICATION: ...] markers from the spec
        2. **LIMIT CHECK**: If more than 3 markers exist, keep only the 3 most critical (by scope/security/UX impact) and make informed guesses for the rest
        3. For each clarification needed (max 3), present options to user in this format:

           ```markdown
           ## Question [N]: [Topic]
           
           **Context**: [Quote relevant spec section]
           
           **What we need to know**: [Specific question from NEEDS CLARIFICATION marker]
           
           **Suggested Answers**:
           
           | Option | Answer | Implications |
           |--------|--------|--------------|
           | A      | [First suggested answer] | [What this means for the feature] |
           | B      | [Second suggested answer] | [What this means for the feature] |
           | C      | [Third suggested answer] | [What this means for the feature] |
           | Custom | Provide your own answer | [Explain how to provide custom input] |
           
           **Your choice**: _[Wait for user response]_
           ```

        4. **CRITICAL - Table Formatting**: Ensure markdown tables are properly formatted:
           - Use consistent spacing with pipes aligned
           - Each cell should have spaces around content: `| Content |` not `|Content|`
           - Header separator must have at least 3 dashes: `|--------|`
           - Test that the table renders correctly in markdown preview
        5. Number questions sequentially (Q1, Q2, Q3 - max 3 total)
        6. Present all questions together before waiting for responses
        7. Wait for user to respond with their choices for all questions (e.g., "Q1: A, Q2: Custom - [details], Q3: B")
        8. Update the spec by replacing each [NEEDS CLARIFICATION] marker with the user's selected or provided answer
        9. Re-run validation after all clarifications are resolved

   d. **Update Checklist**: After each validation iteration, update the checklist file with current pass/fail status

10. Report completion with:
    - Workflow name and directory path
    - Spec file path (`docs/spec.md`)
    - Agent system prompt path (`AGENT.md`)
    - Entry point file path (`ENTRYPOINT.md`)
    - Execution mode determined (INTERACTIVE/SCHEDULED/HYBRID)
    - Checklist results
    - Readiness for the next phase (`/workflowkit.clarify` or `/workflowkit.techify`)

## General Guidelines

## Quick Guidelines

- Focus on **WHAT** users need and **WHY**.
- Avoid HOW to implement (no tech stack, APIs, code structure).
- Written for business stakeholders, not developers.
- DO NOT create any checklists that are embedded in the spec. That will be a separate command.

### Section Requirements

- **Mandatory sections**: Must be completed for every feature
- **Optional sections**: Include only when relevant to the feature
- When a section doesn't apply, remove it entirely (don't leave as "N/A")

### For AI Generation

When creating this spec from a user prompt:

1. **Make informed guesses**: Use context, industry standards, and common patterns to fill gaps
2. **Document assumptions**: Record reasonable defaults in the Assumptions section
3. **Limit clarifications**: Maximum 3 [NEEDS CLARIFICATION] markers - use only for critical decisions that:
   - Significantly impact feature scope or user experience
   - Have multiple reasonable interpretations with different implications
   - Lack any reasonable default
4. **Prioritize clarifications**: scope > security/privacy > user experience > technical details
5. **Think like a tester**: Every vague requirement should fail the "testable and unambiguous" checklist item
6. **Common areas needing clarification** (only if no reasonable default exists):
   - Feature scope and boundaries (include/exclude specific use cases)
   - User types and permissions (if multiple conflicting interpretations possible)
   - Security/compliance requirements (when legally/financially significant)

**Examples of reasonable defaults** (don't ask about these):

- Data retention: Industry-standard practices for the domain
- Performance targets: Standard web/mobile app expectations unless specified
- Error handling: User-friendly messages with appropriate fallbacks
- Authentication method: Standard session-based or OAuth2 for web apps
- Integration patterns: RESTful APIs unless specified otherwise

### Success Criteria Guidelines

Success criteria must be:

1. **Measurable**: Include specific metrics (time, percentage, count, rate)
2. **Technology-agnostic**: No mention of frameworks, languages, databases, or tools
3. **User-focused**: Describe outcomes from user/business perspective, not system internals
4. **Verifiable**: Can be tested/validated without knowing implementation details

**Good examples**:

- "Users can complete checkout in under 3 minutes"
- "System supports 10,000 concurrent users"
- "95% of searches return results in under 1 second"
- "Task completion rate improves by 40%"

**Bad examples** (implementation-focused):

- "API response time is under 200ms" (too technical, use "Users see results instantly")
- "Database can handle 1000 TPS" (implementation detail, use user-facing metric)
- "React components render efficiently" (framework-specific)
- "Redis cache hit rate above 80%" (technology-specific)
