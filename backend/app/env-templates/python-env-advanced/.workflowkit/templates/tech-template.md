---
name: [workflow-name-in-kebab-case]
description: [One-sentence description of what this workflow does and why it exists]
runtime: docker-python
integrations: [email, erp, api-name]
schedule: [manual|cron|event-driven]
data_storage: [postgres|mongodb|files|none]
---

# Technical Specification: [WORKFLOW NAME]

> **PURPOSE**: This document provides a **high-level technical architecture** for developers to understand the overall approach, major components, and architectural decisions. It bridges business requirements (spec.md) and implementation details (plan.md).
>
> **SCOPE**: Focus on WHAT needs to be built and WHY (architectural decisions), not HOW to build it precisely (that's in plan.md).

## Metadata

**Workflow Name**: `[workflow-name-in-kebab-case]`
**Runtime Environment**: Docker container (Python-only)
**Related Business Spec**: `spec.md`
**Related Implementation Plan**: `plan.md` (to be created)

---

## Architecture Overview

[High-level description of how the workflow functions]

```text
┌─────────────┐      ┌──────────────┐      ┌─────────┐
│   Trigger   │─────▶│   Workflow   │─────▶│  Output │
│  (Cron/API) │      │   Execution  │      │  (API/  │
└─────────────┘      └──────────────┘      │   DB)   │
                            │               └─────────┘
                            ▼
                     ┌──────────────┐
                     │ External APIs│
                     │ (Email, ERP) │
                     └──────────────┘
```

**Key Components**:
- **Trigger**: [How the workflow is initiated]
- **Data Processing**: [Main processing pipeline stages]
- **External Integrations**: [Which external systems are involved]
- **Data Storage**: [Where and what data is persisted]

---

## Data Models

> **FOCUS**: Describe entities and their purpose, not exact schemas

### [EntityName]

**Purpose**: [Why this entity exists, what it represents]

**Core Fields**:

| Field Name | Type | Purpose |
|------------|------|---------|
| `id` | UUID/int | Unique identifier |
| `field_name` | str/int/datetime | [Why this field is needed] |
| `related_entity_id` | UUID | [Relationship purpose] |
| `created_at` | datetime | Tracking record creation |

**Relationships**:
- **Has many**: [Related entities and why]
- **Belongs to**: [Parent entities and why]

**Validation Rules**:
- [High-level validation: e.g., "email must be valid format"]
- [High-level validation: e.g., "amount must be positive"]

> **Note**: Exact database schema, indexes, and constraints in plan.md

---

## External Integrations

> **FOCUS**: Which systems to integrate and why, not exact API calls

### [Integration Name] (e.g., Email Service, ERP API)

**Purpose**: [Why this integration is needed]

**Integration Type**: REST API / IMAP / SMTP / Database / etc.

**Authentication Strategy**:
- **Method**: [OAuth2 / API Key / Basic Auth / etc.]
- **Credential Storage**: [Where credentials are managed]

**Data Exchange**:
- **Inbound**: [What data we receive from this system]
- **Outbound**: [What data we send to this system]

**Rate Limiting Strategy**:
- [Approach to handle API quotas: e.g., "batch requests, max 100/hour"]

**Error Handling Approach**:
- [How to handle failed calls: e.g., "retry with exponential backoff, max 3 attempts"]

> **Note**: Exact API endpoints, request/response formats in plan.md

---

## Workflow Execution

> **FOCUS**: How the workflow runs, not exact implementation

### Trigger Mechanism

**Type**: `[manual|cron|event-driven]`

**Schedule** (if cron):
- **Frequency**: [e.g., daily, hourly, weekly]
- **Time**: [e.g., 6 AM UTC]
- **Rationale**: [Why this schedule]

**Event** (if event-driven):
- **Event Source**: [What triggers the workflow]
- **Event Type**: [What kind of event]

### Processing Pipeline

**High-level steps**:

1. **[Step 1 Name]**: [What this step does and why]
   - Input: [What data it receives]
   - Processing: [High-level transformation]
   - Output: [What it produces]

2. **[Step 2 Name]**: [What this step does and why]
   - Input: [What data it receives]
   - Processing: [High-level transformation]
   - Output: [What it produces]

3. **[Step 3 Name]**: [What this step does and why]
   - Input: [What data it receives]
   - Processing: [High-level transformation]
   - Output: [What it produces]

**Pipeline Diagram**:
```text
[Input] → [Step 1] → [Step 2] → [Step 3] → [Output]
             ↓          ↓           ↓
         [Log]      [Validate]  [Store]
```

> **Note**: Exact function names, parameters in plan.md

### State Management

**Tracking Approach**: [How workflow progress is tracked]
- **State Storage**: [Where state is persisted: database, files, etc.]
- **State Fields**: [What information is tracked: status, progress, errors]

**States**:
```text
pending → running → completed
            ↓
        failed
```

**State Transitions**: [When and why states change]

### Error Handling & Retry Strategy

**Approach**: [High-level error handling strategy]

**Retry Logic**:
- **Strategy**: [e.g., exponential backoff, fixed delay]
- **Max Attempts**: [e.g., 3 retries]
- **Failure Action**: [What happens after max retries: alert, log, skip]

**Error Scenarios**:
- **External API failure**: [How to handle]
- **Data validation failure**: [How to handle]
- **Resource unavailable**: [How to handle]

### Logging Strategy

**Log Level**: [INFO / DEBUG / WARNING / ERROR]

**What to Log**:
- Workflow start/completion
- Each processing step
- External API calls (request/response summary)
- Errors and retries
- Performance metrics

**Log Storage**: [Where logs are sent: stdout, file, logging service]

> **Note**: Exact log formats, structured logging details in plan.md

---

## Dependencies

### Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `requests` | ≥2.28 | HTTP client for API calls |
| `psycopg2` | ≥2.9 | PostgreSQL database driver |
| `pydantic` | ≥2.0 | Data validation |
| [package] | [version] | [Why needed] |

### External Services

| Service | Purpose |
|---------|---------|
| PostgreSQL | Primary data storage |
| [Service name] | [Why needed] |

### Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `DATABASE_URL` | Database connection string | `postgresql://user:pass@host/db` |
| `API_KEY` | External service authentication | `sk_live_...` |
| [VAR_NAME] | [Purpose] | [Example value] |

> **Note**: Exact package versions, configuration details in plan.md

---

## Performance Considerations

> **FOCUS**: Performance strategy, not exact optimizations

### Expected Volume

**Data Scale**:
- **Records per run**: [e.g., 1000 emails]
- **Run frequency**: [e.g., daily]
- **Total records**: [e.g., 30K/month]

**Processing Time**:
- **Target**: [e.g., complete within 15 minutes]
- **Constraint**: [e.g., must finish before business hours]

### Optimization Strategy

**Batching Approach**: [How to process data in batches]
- **Batch Size**: [e.g., 100 records per batch - why]
- **Rationale**: [Balance between memory and performance]

**Concurrency**: [Approach to parallel processing]
- **Strategy**: [e.g., async processing, thread pool]
- **Limit**: [e.g., max 5 concurrent API calls]
- **Rationale**: [Why this limit: rate limits, memory, etc.]

**Database Performance**:
- **Indexing Strategy**: [Which fields to index and why]
- **Query Approach**: [Batch queries, avoid N+1, etc.]

**Resource Limits**:
- **Memory**: [Expected usage and limits]
- **CPU**: [Expected usage]
- **Disk**: [Storage requirements]

> **Note**: Exact optimization code, profiling details in plan.md

---

## Security Considerations

> **FOCUS**: Security approach, not exact implementations

### Credential Management

**Storage**: [How credentials are stored]
- **Approach**: [Environment variables / secret manager / encrypted config]
- **Access Control**: [Who/what can access credentials]

**Secrets**:
- API keys
- Database passwords
- OAuth tokens

### Data Protection

**Sensitive Data**: [What data is considered sensitive]
- **Encryption**: [At rest / in transit approach]
- **Access Control**: [Who can access]

**Data Retention**:
- **Strategy**: [How long data is kept]
- **Cleanup**: [How old data is removed]

### API Security

**Outbound Calls**:
- **SSL/TLS**: [Always use HTTPS]
- **Timeout**: [Strategy to prevent hanging]

**Inbound Triggers** (if applicable):
- **Authentication**: [How to verify callers]
- **Rate Limiting**: [Prevent abuse]

> **Note**: Exact security implementations in plan.md

---

## Testing Considerations

> **FOCUS**: What to test, not exact test code

### Test Scope

**Unit Tests**:
- Data validation logic
- Data transformations
- Utility functions

**Integration Tests**:
- External API interactions (mocked)
- Database operations
- End-to-end processing pipeline

**Edge Cases**:
- Empty input data
- Malformed external API responses
- Database connection failures
- Rate limit scenarios

### Test Data Requirements

**Test Entities**:
- Sample valid records
- Invalid records for validation testing
- Edge case records

**External Service Mocks**:
- [Which external services need mocking]
- [What responses to mock]

### Success Criteria

**Coverage Target**: [e.g., >80% code coverage]

**Critical Paths** (must be tested):
- Main processing pipeline
- Error handling and retries
- Data validation
- External integration points

> **Note**: Exact test methods, fixtures in plan.md

---

## Implementation Checklist

- [ ] All data models defined with fields
- [ ] All external integrations specified
- [ ] Execution trigger and schedule defined
- [ ] Processing pipeline outlined
- [ ] Error handling strategy defined
- [ ] State management approach specified
- [ ] Dependencies listed
- [ ] Performance considerations addressed
- [ ] Security strategy outlined
- [ ] Testing approach defined

---

## Next Steps

After completing tech.md:

1. **Review with stakeholders**: Validate technical approach aligns with business spec
2. **Create plan.md**: Use `/workflowkit.plan` command to generate detailed implementation plan
3. **Begin implementation**: Follow plan.md for exact specifications
4. **Iterate**: Update tech.md if architectural decisions change during implementation
