---
description: Execute the implementation planning workflow using the plan template to generate design artifacts.
handoffs:
  - label: Implement Workflow
    command: workflowkit.implement
    prompt: Execute the implementation plan
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Outline

1. **Setup**: Determine workflow to plan:
   - If user provided workflow name in arguments, use it: `workflows/<workflow-name>/`
   - Otherwise, list available workflows in `workflows/` directory and ask user to select
   - Set paths based on `templates/blank_template_workflow` structure:
     - `WORKFLOW_DIR` = `workflows/<workflow-name>/`
     - `WORKFLOW_SPEC` = `workflows/<workflow-name>/docs/spec.md`
     - `TECH_SPEC` = `workflows/<workflow-name>/docs/tech.md`
     - `IMPL_PLAN` = `workflows/<workflow-name>/docs/plan.md`
     - `AGENT_FILE` = `workflows/<workflow-name>/AGENT.md`
     - `ENTRYPOINT_FILE` = `workflows/<workflow-name>/ENTRYPOINT.md`
     - `SCRIPTS_DIR` = `workflows/<workflow-name>/scripts/` (for implementation code)
     - `DATABASES_DIR` = `workflows/<workflow-name>/databases/` (for database schemas/files)
     - `FILES_DIR` = `workflows/<workflow-name>/files/` (for input/output files)
     - `LOGS_DIR` = `workflows/<workflow-name>/logs/` (for execution logs)
   - Verify WORKFLOW_SPEC and TECH_SPEC exist before proceeding

2. **Load context**: Read WORKFLOW_SPEC, TECH_SPEC, and `.workflowkit/templates/plan-template.md`. Initialize IMPL_PLAN from template.

3. **Execute plan workflow**: Follow the structure in IMPL_PLAN template to:
   - Fill Technical Context (mark unknowns as "NEEDS CLARIFICATION")
   - Fill Constitution Check section (if `.workflowkit/memory/constitution.md` exists and references workflow structure)
   - Evaluate gates (ERROR if violations unjustified)
   - Phase 0: Generate research.md (resolve all NEEDS CLARIFICATION)
   - Phase 1: Plan implementation details for `scripts/`, `databases/`, `files/` directories
   - Re-evaluate Constitution Check post-design (if applicable)

4. **Stop and report**: Command ends after Phase 1 planning. Report workflow name, IMPL_PLAN path, and generated artifacts. Next step is `/workflowkit.implement`.

## Phases

### Phase 0: Outline & Research

1. **Extract unknowns from Technical Context** above:
   - For each NEEDS CLARIFICATION → research task
   - For each dependency → best practices task
   - For each integration → patterns task

2. **Generate and dispatch research agents**:

   ```text
   For each unknown in Technical Context:
     Task: "Research {unknown} for {feature context}"
   For each technology choice:
     Task: "Find best practices for {tech} in {domain}"
   ```

3. **Consolidate findings** in `research.md` using format:
   - Decision: [what was chosen]
   - Rationale: [why chosen]
   - Alternatives considered: [what else evaluated]

**Output**: research.md with all NEEDS CLARIFICATION resolved

### Phase 1: Implementation Planning

**Prerequisites:** `research.md` complete

1. **Extract entities from spec** → write to `docs/data-model.md` (if workflow manages data):
   - Entity name, fields, relationships
   - Validation rules from requirements
   - State transitions if applicable
   - Database schema design (for `databases/` directory)

2. **Plan script structure** → define Python modules for `scripts/` directory:
   - Main workflow execution script
   - Helper modules and utilities
   - Integration clients (email, ERP, APIs)
   - Data processing functions
   - **Note**: Script structure and file organization details go in plan.md, not actual code

3. **Define file requirements** → specify files for `files/` directory:
   - Input file formats and locations
   - Output file formats and destinations
   - Configuration files needed

4. **Plan database setup** → specify for `databases/` directory:
   - Database schemas or file-based storage structure
   - Migration scripts (if needed)
   - Seed data requirements

5. **Document execution** → update `README.md`:
   - Workflow purpose and overview
   - Setup instructions
   - How to run the workflow
   - Configuration options

6. **Complete AGENT.md with implementation details**:

   Read AGENT_FILE and update with detailed implementation information from plan.md:

   a. **Scripts (`scripts/`)**: List all Python scripts with descriptions
      ```markdown
      ### Scripts (`scripts/`)

      **Main Scripts**:
      - `main.py`: [Description - e.g., "Main workflow orchestration"]
      - `email_processor.py`: [Description - e.g., "Email parsing and dunning detection"]
      - `erp_client.py`: [Description - e.g., "ERP API integration client"]

      **Helper Modules**:
      - `utils/logger.py`: [Description - e.g., "Logging utilities"]
      - `utils/config.py`: [Description - e.g., "Configuration management"]
      ```

   b. **Files (`files/`)**: Specify input and output files
      ```markdown
      ### Files (`files/`)

      **Input Files**:
      - `config/credentials.json`: [Format and content - e.g., "ERP API credentials"]
      - `config/settings.yaml`: [Format and content - e.g., "Workflow configuration"]

      **Output Files**:
      - `output/processed_emails.csv`: [Format and content - e.g., "List of processed dunning notifications"]
      - `output/erp_updates.log`: [Format and content - e.g., "Log of ERP updates"]
      ```

   c. **Execution Flow**: Define step-by-step execution sequence
      ```markdown
      ## Execution Flow

      1. **Initialize**: Load configuration from `files/config/settings.yaml`
      2. **Authenticate**: Connect to email server using credentials from vault
      3. **Fetch emails**: Retrieve unread emails from specified folder
      4. **Process emails**: Parse each email and detect dunning notifications
      5. **Update ERP**: For each detected dunning, update bill status in ERP
      6. **Log results**: Write execution summary to `logs/execution_YYYYMMDD_HHMMSS.log`
      7. **Cleanup**: Close connections and archive processed emails
      ```

   d. **Error Handling**: Specify error handling procedures
      ```markdown
      ## Error Handling

      **Email Connection Failures**:
      - Retry with exponential backoff (3 attempts)
      - Log error details to `logs/errors.log`
      - Send alert notification if all retries fail

      **ERP API Errors**:
      - Log error with email details for manual review
      - Continue processing remaining emails
      - Generate error report in `output/failed_updates.csv`

      **Parsing Errors**:
      - Log malformed email to `logs/parsing_errors.log`
      - Skip email and continue processing
      - Include in summary report
      ```

   e. Save updated AGENT_FILE

7. **Update ENTRYPOINT.md with expected response formats**:

   Update the `ENTRYPOINT_FILE` with detailed response format specifications:

   ```markdown
   ## Expected Response

   **Interactive Mode**:
   ```
   Summary of mailbox scan:
   - Total emails processed: [count]
   - Dunning notifications found: [count]
   - ERP updates completed: [count]
   - Errors encountered: [count]

   [If dunnings found]:
   Dunning Notifications Detected:
   1. [Bill ID] - [Customer] - [Amount] - [Due Date]
   2. [Bill ID] - [Customer] - [Amount] - [Due Date]

   [If errors]:
   Errors:
   - [Error description]
   ```

   **Scheduled Mode**:
   ```
   {
     "execution_id": "exec_YYYYMMDD_HHMMSS",
     "timestamp": "YYYY-MM-DD HH:MM:SS",
     "status": "success|partial|failed",
     "metrics": {
       "emails_processed": [count],
       "dunnings_found": [count],
       "erp_updates": [count],
       "errors": [count]
     },
     "details": {
       "processed_emails": ["email_id1", "email_id2"],
       "updated_bills": ["bill_id1", "bill_id2"],
       "errors": ["error_description1"]
     },
     "logs": {
       "execution_log": "logs/execution_YYYYMMDD_HHMMSS.log",
       "error_log": "logs/errors.log"
     }
   }
   ```
   ```

   f. Save updated ENTRYPOINT_FILE

**Output**:
- `docs/data-model.md` (if data entities exist)
- Updated `docs/plan.md` with detailed implementation specifications
- Updated `README.md` with workflow documentation
- Updated `AGENT.md` with complete implementation details
- Updated `ENTRYPOINT.md` with response format specifications

## Key rules

- Use absolute paths
- ERROR on gate failures or unresolved clarifications
