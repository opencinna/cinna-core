# Handover Prompt Generator

## Task

Generate a **concise, action-oriented handover prompt** that instructs a source agent to USE the create_agent_task tool to trigger a target agent.

## What is a Handover?

A handover is when one agent completes its task and must USE THE TOOL to pass control to another agent with specific context. The handover prompt is a DIRECT INSTRUCTION to call the create_agent_task tool.

## Critical Requirements

- **IMPERATIVE LANGUAGE** - Use "MUST use the create_agent_task tool" or "immediately call create_agent_task"
- **EXPLICIT TOOL MENTION** - Always reference the "create_agent_task tool" by name
- **Maximum 2-3 sentences** - This will be added as tool documentation
- **Clear trigger condition** - State exactly WHEN to use the tool
- **Specific context** - Define what data/results to include in task_message
- **NO MARKDOWN FORMATTING** - No headers, no bold, just plain instructional text

## Output Format

Return ONLY the handover prompt text. No JSON, no markdown headers/formatting, no code blocks, no extra explanations.

## Good Examples (ACTION-ORIENTED)

✅ "As soon as you successfully fetch all cryptocurrency rates in this conversation, IMMEDIATELY use the create_agent_task tool to hand over to CryptoRateLogger. Include each symbol with its current price in the task_message. Example: 'BTC: $87,318.27, ETH: $2,927.89, SOL: $156.43'"

✅ "In this conversation, after you complete the code review, if you find any critical security issues, you MUST immediately use the create_agent_task tool right then. In the task_message, list each issue with affected file. Example: 'SQL injection in auth.py line 45, XSS in user_profile.py line 103'"

✅ "When you complete the report generation in this conversation, immediately call create_agent_task in the same response with the file path and recipient list. Example: 'Report ready at /reports/monthly_summary.pdf, recipients: team@company.com, manager@company.com'"

## Bad Examples

❌ "Once you have fetched the rates, hand over to the Logger agent" - Too passive, no "this conversation", doesn't emphasize TOOL USAGE

❌ "# IMPORTANT: Hand over when done with results" - Has markdown formatting, too vague, no timing context

❌ "Maybe pass some info to the other agent if needed" - Not imperative, unclear condition, optional language

❌ "After detailed analysis including comprehensive data review..." - Too long and verbose

❌ "When analysis is done, hand over" - Doesn't specify "in this conversation" or "immediately", no sense of NOW

## Guidelines

1. **ADD TEMPORAL CONTEXT** - Use "in this conversation", "right now", "as soon as", "immediately in the same response"
2. **START with timing** - "As soon as you...", "In this conversation, when you...", "The moment you complete..."
3. **EMPHASIZE IMMEDIACY** - "IMMEDIATELY use", "MUST immediately call", "right then"
4. **Specify target agent name** - Include it in the instruction
5. **Define task_message content** - Be specific about what to include
6. **Provide concrete example** - Show EXACTLY how the task_message should look
7. **NO markdown** - No headers (#), no bold (**), just plain instructional text
8. **Keep it compact** - 2-3 sentences maximum

## Context You'll Receive

You'll be given:
- **Source Agent**: Name, entrypoint prompt, workflow prompt
- **Target Agent**: Name, entrypoint prompt, workflow prompt

Use these to understand:
- What the source agent does
- What the target agent expects
- How they should connect logically

## CRITICAL REMINDER

The generated prompt MUST make the agent understand this is happening:
- **In the current conversation** (not some future hypothetical scenario)
- **Right now** (as soon as the condition is met)
- **In the same response** (don't wait for next message)
- **By calling the tool** (not describing or mentioning)

Use phrases like:
- "As soon as you [complete X] in this conversation, IMMEDIATELY use create_agent_task..."
- "In this conversation, the moment you [finish Y], call create_agent_task right then..."
- "When you [complete Z] in this conversation, immediately call create_agent_task in the same response..."
