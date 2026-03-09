# Agent-to-Webapp Actions — Full Reference

You can send action commands from your responses to trigger UI changes in the webapp without requiring the user to manually refresh. Embed action tags anywhere in your text response using this format:

```
<webapp_action>{"action": "action_type", "data": { ... }}</webapp_action>
```

The tag and its content are automatically stripped from the visible chat message — the user only sees your regular response text. The action is executed silently in the webapp.

## Available Action Types

### `refresh_page`
Reload the entire webapp iframe. Use this after making code changes in building mode.

```
<webapp_action>{"action": "refresh_page"}</webapp_action>
```

No `data` field required.

### `reload_data`
Signal the webapp to refetch data from a specific endpoint. Use this in conversation mode when you have updated backend data that the dashboard should reflect.

```
<webapp_action>{"action": "reload_data", "data": {"endpoint": "/api/reports"}}</webapp_action>
```

| Field | Description |
|---|---|
| `data.endpoint` | The relative API path that should be re-fetched |

The webapp receives a `webapp_reload_data` custom event with `event.detail.endpoint`. Your data-fetching code should listen for this event and trigger a refetch.

### `update_form`
Set values on a specific form in the webapp. Use this in conversation mode to apply filter changes that the user asked for in natural language.

```
<webapp_action>{"action": "update_form", "data": {"form_id": "report-filter", "values": {"date_range": "2024-Q4", "region": "north"}}}</webapp_action>
```

| Field | Description |
|---|---|
| `data.form_id` | The `id` attribute of the `<form>` element |
| `data.values` | Object mapping `name` attributes to new values |

The bridge script sets each field's value and fires `input` + `change` events so reactive frameworks (Alpine.js, htmx, etc.) update automatically.

### `show_notification`
Display a notification to the user. Use this to confirm completed actions or report errors.

```
<webapp_action>{"action": "show_notification", "data": {"message": "Report generated successfully", "type": "success"}}</webapp_action>
```

| Field | Values | Description |
|---|---|---|
| `data.message` | any string | The notification text |
| `data.type` | `"success"` \| `"error"` \| `"warning"` \| `"info"` | Visual style of the notification |

The webapp receives a `webapp_show_notification` custom event. Add an event listener to render a toast or banner using your preferred UI library.

### `navigate`
Navigate within a single-page app. Use this in conversation mode to take the user to a relevant section.

```
<webapp_action>{"action": "navigate", "data": {"path": "/dashboard/reports"}}</webapp_action>
```

| Field | Description |
|---|---|
| `data.path` | The relative path to navigate to |

Uses `history.pushState` + a `popstate` event so SPA routers (htmx, Alpine, custom) update correctly. Falls back to `window.location.href` if `pushState` is unavailable.

## Custom Event Listeners

For `reload_data` and `show_notification`, add listeners in your page JS:

```javascript
// Listen for data reload requests
window.addEventListener('webapp_reload_data', function(e) {
  var endpoint = e.detail.endpoint;
  // Trigger your data fetch for this endpoint
  fetchData(endpoint);
});

// Listen for notification requests
window.addEventListener('webapp_show_notification', function(e) {
  var msg = e.detail.message;
  var type = e.detail.type || 'info';
  // Show using your preferred notification library
  showToast(msg, type);
});
```

## Best Practices

- **Building mode — use `refresh_page`** after writing or modifying HTML/JS/CSS files so the user sees the updated webapp immediately.
- **Conversation mode — prefer `update_form` and `reload_data`** over `refresh_page`. These are non-destructive and preserve the user's scroll position and UI state.
- **Combine text with actions** — always explain what you did in your text response before emitting the action tag. The user sees the text first.
- **Multiple actions in one response** — you can include several `<webapp_action>` tags; they are emitted in order.
- **Do not use actions as a substitute for building** — actions manipulate the existing UI; they cannot create new UI elements. Use building mode (write files, then `refresh_page`) to add new features.

## Example: Conversation Mode Filter Update

```
I've updated the date filter to Q4 2024 for you. The dashboard will refresh the data now.

<webapp_action>{"action": "update_form", "data": {"form_id": "report-filter", "values": {"date_range": "2024-Q4"}}}</webapp_action>
<webapp_action>{"action": "reload_data", "data": {"endpoint": "/api/reports"}}</webapp_action>
```

## Example: Building Mode Code Change

```
I've updated the chart colors and added the trend line to the revenue chart. Refreshing the page so you can see the changes.

<webapp_action>{"action": "refresh_page"}</webapp_action>
```
