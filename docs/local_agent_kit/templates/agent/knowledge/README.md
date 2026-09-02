# Knowledge — {{name}}

Static reference material this agent needs in order to be correct: business rules,
how an external system actually behaves, terminology, decision rationale. Read-only
at runtime, both locally and in the cloud.

Create one file (or one folder) per topic and reference it from
`docs/WORKFLOW_PROMPT.md` so the agent knows where to look:

```
knowledge/
├── invoice_matching_rules.md
└── vendor_portal/
    ├── api_quirks.md
    └── field_mapping.md
```

Do not put here:

- prompts (`docs/`), tunable parameters (`config/`), lookup tables the scripts load
  (`files/`), or anything the agent produces at runtime (`app-data/storage/`);
- credentials, hostnames with tokens in them, or personal data.

Add this folder only when the ladder's **Knowledge & local skills** trigger has
fired — three or more distinct capabilities, or domain documentation longer than a
page. See `.cinna-kit/guides/08-knowledge-and-local-skills.md`.
