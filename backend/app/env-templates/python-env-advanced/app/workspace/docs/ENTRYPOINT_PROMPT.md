# Workflow Trigger Message

This is the **user message** that triggers the workflow execution. Keep it concise and actionable.

---

[TO BE FILLED: Write a 1-2 sentence instruction that triggers this workflow]

---

## Guidelines

- **Be specific**: Clearly state what action should be performed
- **Be concise**: 1-2 sentences maximum
- **Be actionable**: Use command verbs (e.g., "Collect", "Generate", "Process", "Check")
- **Include key parameters**: If the workflow needs configuration, include it here

## Examples for Different Workflow Types

**Data Processing**:
```
Process all CSV files in the input folder and generate summary statistics
```

**Email Monitoring**:
```
Collect from my mailbox unread emails, detect invoices, and give summary about them
```

**Report Generation**:
```
Generate yesterday's sales report and send to the team
```

**Database Tasks**:
```
Run database backup for all production databases and verify integrity
```

**API Integration**:
```
Fetch latest orders from Shopify and sync to our ERP system
```

**Social Media**:
```
Check mentions of our brand in the last 24 hours and summarize sentiment
```

---

**Note**: This message will be used as the first user message when:
- Running the workflow on a schedule
- Demonstrating the workflow's default behavior
- Explaining to users how to trigger the workflow manually
