# Webapp Actions Registry

This file documents the webapp actions available for this agent. Use `<webapp_action>` tags
in your responses to trigger UI changes in the webapp.

## Syntax

```
<webapp_action>{"action": "action_type", "data": { ... }}</webapp_action>
```

Tags are stripped from the visible message — only the webapp sees them.

## Built-in Actions

| Action | When to use | Required data fields |
|--------|-------------|----------------------|
| `refresh_page` | After code changes in building mode | none |
| `reload_data` | After updating backend data in conversation mode | `endpoint` (relative API path) |
| `update_form` | To fill form fields the user described (requires `id` on the `<form>` and `name` on each field) | `form_id`, `values` (field name → value map) |
| `show_notification` | To confirm completed actions or report errors | `message`, `type` (success/error/warning/info) |
| `navigate` | To take the user to a section in a single-page app | `path` (relative URL) |

**Important:** `update_form` only works on standard HTML `<form>` elements with an `id` attribute, targeting child `<input>`/`<select>`/`<textarea>` elements by their `name` attribute. It does NOT work on JavaScript framework state (Alpine.js variables, React state, etc.) or non-form elements like button groups or filter chips. For those, use a custom action documented below.

For full field specifications and usage examples, read `/app/core/webapp-framework/ACTIONS_REFERENCE.md`.

## Custom Actions

When you add custom event listeners to the webapp JavaScript (e.g., `webapp_action_my_event`),
document them here so you remember what is available in conversation mode.

<!-- Add custom actions below this line -->
