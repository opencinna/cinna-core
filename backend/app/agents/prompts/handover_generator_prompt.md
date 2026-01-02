# Handover Prompt Generator

## Task

Generate a **concise handover prompt** that defines when and how a source agent should trigger a target agent.

## What is a Handover?

A handover is when one agent completes its task and needs to pass control to another agent with specific context. The handover prompt should describe:

1. **Condition**: When should the handover happen?
2. **Context**: What information should be passed to the target agent?
3. **Instructions**: How should the message be formatted?

## Requirements

- **Maximum 2-3 sentences** - This will be added as a tool-call description
- **Clear trigger condition** - State exactly when to handover
- **Specific context** - Define what data/results to include
- **Natural language** - Write as if instructing a human

## Output Format

Return ONLY the handover prompt text. No JSON, no markdown formatting, no extra explanations.

## Good Examples

✅ "Once you've identified the top 3 cryptocurrencies with highest growth potential, hand over to the Cryptocurrency Trader agent with the list of coins and your analysis summary. Example: 'Here are the top 3 cryptos to process: BTC, ETH, SOL with analysis...'

✅ "After completing the code review, if you find critical security issues, hand over to the Security Team Notifier agent with the issue details and affected files. Example: 'Critical security issues found in authentication.py and user.py...'

✅ "When the report generation is complete, hand over to the Email Sender agent with the report file path and recipient list. Example: 'Report generated at /reports/monthly_summary.pdf, send to team@example.com'

## Bad Examples

❌ "Hand over when done" - Too vague, no context specified

❌ "After analysis, send the following detailed breakdown including all technical specifications, performance metrics, comparative analysis against industry standards, and comprehensive recommendations with supporting data to the Trading Bot agent..." - Too long and verbose

❌ "Maybe pass some info to the other agent if needed" - Unclear condition and context

## Guidelines

1. Start with the **completion condition** (e.g., "Once you've...", "After completing...", "When...")
2. Specify the **target agent's name** explicitly
3. List the **specific data** to include (e.g., "top 3 items", "error details", "file path")
4. Provide a **concrete example** of how the handover message should look
5. Keep it **compact** - remember this is tool documentation, not a full workflow

## Context You'll Receive

You'll be given:
- **Source Agent**: Name, entrypoint prompt, workflow prompt
- **Target Agent**: Name, entrypoint prompt, workflow prompt

Use these to understand:
- What the source agent does
- What the target agent expects
- How they should connect logically
