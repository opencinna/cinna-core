---
description: Execute the implementation plan by implementing the workflow according to plan.md
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Outline

1. **Determine workflow to implement**:
   - If user provided workflow name in arguments, use it: `workflows/<workflow-name>/`
   - Otherwise, list available workflows in `workflows/` directory and ask user to select
   - Set paths based on `templates/blank_template_workflow` structure (all absolute):
     - `WORKFLOW_DIR` = `<repo-root>/workflows/<workflow-name>/`
     - `SPEC_FILE` = `<repo-root>/workflows/<workflow-name>/docs/spec.md`
     - `TECH_FILE` = `<repo-root>/workflows/<workflow-name>/docs/tech.md`
     - `PLAN_FILE` = `<repo-root>/workflows/<workflow-name>/docs/plan.md`
     - `AGENT_FILE` = `<repo-root>/workflows/<workflow-name>/AGENT.md`
     - `ENTRYPOINT_FILE` = `<repo-root>/workflows/<workflow-name>/ENTRYPOINT.md`
     - `SCRIPTS_DIR` = `<repo-root>/workflows/<workflow-name>/scripts/`
     - `DATABASES_DIR` = `<repo-root>/workflows/<workflow-name>/databases/`
     - `FILES_DIR` = `<repo-root>/workflows/<workflow-name>/files/`
     - `LOGS_DIR` = `<repo-root>/workflows/<workflow-name>/logs/`
   - Scan docs/ directory for available files: data-model.md, research.md
   - Verify required files exist (docs/spec.md, docs/tech.md, docs/plan.md, AGENT.md, ENTRYPOINT.md) before proceeding

2. **Check checklists status** (if WORKFLOW_DIR/checklists/ exists):
   - Scan all checklist files in the checklists/ directory
   - For each checklist, count:
     - Total items: All lines matching `- [ ]` or `- [X]` or `- [x]`
     - Completed items: Lines matching `- [X]` or `- [x]`
     - Incomplete items: Lines matching `- [ ]`
   - Create a status table:

     ```text
     | Checklist | Total | Completed | Incomplete | Status |
     |-----------|-------|-----------|------------|--------|
     | ux.md     | 12    | 12        | 0          | ✓ PASS |
     | test.md   | 8     | 5         | 3          | ✗ FAIL |
     | security.md | 6   | 6         | 0          | ✓ PASS |
     ```

   - Calculate overall status:
     - **PASS**: All checklists have 0 incomplete items
     - **FAIL**: One or more checklists have incomplete items

   - **If any checklist is incomplete**:
     - Display the table with incomplete item counts
     - **STOP** and ask: "Some checklists are incomplete. Do you want to proceed with implementation anyway? (yes/no)"
     - Wait for user response before continuing
     - If user says "no" or "wait" or "stop", halt execution
     - If user says "yes" or "proceed" or "continue", proceed to step 3

   - **If all checklists are complete**:
     - Display the table showing all checklists passed
     - Automatically proceed to step 3

3. Load and analyze the implementation context:
   - **REQUIRED**: Read docs/plan.md for tech stack, architecture, and file structure
   - **REQUIRED**: Read docs/tech.md for high-level technical architecture
   - **REQUIRED**: Read AGENT.md for the agent system prompt and expected workflow behavior
   - **REQUIRED**: Read ENTRYPOINT.md for entry point and expected response formats
   - **IF EXISTS**: Read docs/data-model.md for entities and relationships
   - **IF EXISTS**: Read docs/research.md for technical decisions and constraints
   - **Note**: Implementation will create Python code in `scripts/`, database files in `databases/`, etc.
   - **CRITICAL**: The implemented workflow MUST match what AGENT.md describes - the agent will use this system prompt to execute the workflow

4. **Project Setup Verification**:
   - **REQUIRED**: Create/verify ignore files based on actual project setup:

   **Detection & Creation Logic**:
   - Check if the following command succeeds to determine if the repository is a git repo (create/verify .gitignore if so):

     ```sh
     git rev-parse --git-dir 2>/dev/null
     ```

   - Check if Dockerfile* exists or Docker in plan.md → create/verify .dockerignore
   - Check if .eslintrc* exists → create/verify .eslintignore
   - Check if eslint.config.* exists → ensure the config's `ignores` entries cover required patterns
   - Check if .prettierrc* exists → create/verify .prettierignore
   - Check if .npmrc or package.json exists → create/verify .npmignore (if publishing)
   - Check if terraform files (*.tf) exist → create/verify .terraformignore
   - Check if .helmignore needed (helm charts present) → create/verify .helmignore

   **If ignore file already exists**: Verify it contains essential patterns, append missing critical patterns only
   **If ignore file missing**: Create with full pattern set for detected technology

   **Common Patterns by Technology** (from plan.md tech stack):
   - **Node.js/JavaScript/TypeScript**: `node_modules/`, `dist/`, `build/`, `*.log`, `.env*`
   - **Python**: `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `dist/`, `*.egg-info/`
   - **Java**: `target/`, `*.class`, `*.jar`, `.gradle/`, `build/`
   - **C#/.NET**: `bin/`, `obj/`, `*.user`, `*.suo`, `packages/`
   - **Go**: `*.exe`, `*.test`, `vendor/`, `*.out`
   - **Ruby**: `.bundle/`, `log/`, `tmp/`, `*.gem`, `vendor/bundle/`
   - **PHP**: `vendor/`, `*.log`, `*.cache`, `*.env`
   - **Rust**: `target/`, `debug/`, `release/`, `*.rs.bk`, `*.rlib`, `*.prof*`, `.idea/`, `*.log`, `.env*`
   - **Kotlin**: `build/`, `out/`, `.gradle/`, `.idea/`, `*.class`, `*.jar`, `*.iml`, `*.log`, `.env*`
   - **C++**: `build/`, `bin/`, `obj/`, `out/`, `*.o`, `*.so`, `*.a`, `*.exe`, `*.dll`, `.idea/`, `*.log`, `.env*`
   - **C**: `build/`, `bin/`, `obj/`, `out/`, `*.o`, `*.a`, `*.so`, `*.exe`, `Makefile`, `config.log`, `.idea/`, `*.log`, `.env*`
   - **Swift**: `.build/`, `DerivedData/`, `*.swiftpm/`, `Packages/`
   - **R**: `.Rproj.user/`, `.Rhistory`, `.RData`, `.Ruserdata`, `*.Rproj`, `packrat/`, `renv/`
   - **Universal**: `.DS_Store`, `Thumbs.db`, `*.tmp`, `*.swp`, `.vscode/`, `.idea/`

   **Tool-Specific Patterns**:
   - **Docker**: `node_modules/`, `.git/`, `Dockerfile*`, `.dockerignore`, `*.log*`, `.env*`, `coverage/`
   - **ESLint**: `node_modules/`, `dist/`, `build/`, `coverage/`, `*.min.js`
   - **Prettier**: `node_modules/`, `dist/`, `build/`, `coverage/`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`
   - **Terraform**: `.terraform/`, `*.tfstate*`, `*.tfvars`, `.terraform.lock.hcl`
   - **Kubernetes/k8s**: `*.secret.yaml`, `secrets/`, `.kube/`, `kubeconfig*`, `*.key`, `*.crt`

5. Parse plan.md structure and extract:
   - **Implementation phases**: Setup, Core, Integration, Testing
   - **Dependencies**: Python packages, external services
   - **File structure**: What files/modules to create
   - **Data models**: Entities and schemas
   - **Execution flow**: Order and dependency requirements

6. Execute implementation following the plan:
   - **Phase-by-phase execution**: Complete each phase before moving to the next
   - **Respect dependencies**: Install dependencies first, then implement
   - **Follow best practices**: Use Python conventions, type hints, proper error handling
   - **File-based coordination**: Organize code according to plan structure
   - **Validation checkpoints**: Verify each phase completion before proceeding

7. Implementation execution rules:
   - **Setup first**: Initialize project structure, dependencies, configuration files
   - **Core development**: Implement data models, processing logic, integration clients
   - **Integration work**: Database connections, API clients, authentication, logging
   - **Testing**: Unit tests, integration tests, validation

8. Progress tracking and error handling:
   - Report progress after each completed phase
   - Halt execution if any critical task fails
   - Provide clear error messages with context for debugging
   - Suggest next steps if implementation cannot proceed
   - Track completed work in implementation log

9. Completion validation:
   - Verify all required components are implemented
   - Check that implemented workflow matches the original specification
   - Validate that basic tests pass
   - Confirm the implementation follows the technical plan and design
   - Report final status with summary of completed work

10. **Agent System Prompt Validation**:
    After implementation is complete, validate that AGENT.md accurately describes the implemented workflow:

    a. **Verify Scripts Section**:
       - Check that all scripts mentioned in AGENT.md exist in `scripts/`
       - Verify script descriptions match their actual functionality
       - Update AGENT.md if any script names or purposes changed during implementation

    b. **Verify Files Section**:
       - Confirm all input files listed in AGENT.md exist in `files/` or are expected inputs
       - Verify all output files listed match what scripts actually generate
       - Update AGENT.md if file formats or locations changed

    c. **Verify Execution Flow**:
       - Ensure the execution flow in AGENT.md matches the actual workflow logic
       - Check that the order of steps is accurate
       - Update AGENT.md if implementation deviated from planned flow

    d. **Verify Error Handling**:
       - Confirm error handling procedures in AGENT.md match implemented error handling
       - Check that retry logic, logging, and error recovery match what's described
       - Update AGENT.md if error handling changed during implementation

    e. **Verify Tools and Capabilities**:
       - Ensure all Python packages listed in AGENT.md are actually used
       - Verify external integrations match implemented API clients
       - Check that authentication methods described are what's implemented
       - Update AGENT.md if dependencies or integrations changed

    f. **Save Updated AGENT.md**:
       - If any discrepancies found, update AGENT.md to reflect actual implementation
       - Ensure AGENT.md is a precise, accurate system prompt for the implemented workflow
       - The agent executing this workflow will rely on AGENT.md to understand its capabilities

Note: This command requires plan.md to be complete. If planning is incomplete, run `/workflowkit.plan` first.
