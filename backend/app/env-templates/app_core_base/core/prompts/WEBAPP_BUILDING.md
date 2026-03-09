# Web App Building Guide

## Overview
You can build a web application for data visualization at `/app/workspace/webapp/`.
This webapp is served through the platform and can be shared via URL or embedded as an iframe.

## Directory Structure
Place all webapp files in `/app/workspace/webapp/`:
- `index.html` — required entry point
- `assets/` — CSS, JS, images
- `pages/` — additional HTML pages
- `api/` — Python scripts that serve dynamic data (see Data Endpoints below)

## Technology Rules
- The final webapp must consist of **production-ready static files** (HTML, CSS, JS, images)
- Use CDN-loaded libraries: Tailwind CSS, Chart.js, Plotly.js, AG Grid, Alpine.js, htmx, etc.
- You MAY use npm, bundlers (Vite, esbuild, etc.) during development to speed up your workflow
- But you MUST always produce a final built output in `/app/workspace/webapp/` — no dev servers, no `node_modules` at runtime
- The platform serves files directly from `webapp/` — there is no build step at serve time
- If you use a bundler, run the build and place output in `webapp/`. Remove build artifacts when done
- Keep files self-contained — the webapp must work when served as static files from any URL prefix

## Data Endpoints (Dynamic Data)
For dashboards that need live data from databases or APIs:

1. Create Python scripts in `webapp/api/` (e.g., `webapp/api/get_sales.py`)
2. Each script:
   - Reads JSON params from stdin: `params = json.loads(sys.stdin.read() or '{}')`
   - Prints JSON result to stdout: `print(json.dumps(result))`
   - Can import any workspace packages, access databases in `/app/workspace/files/`, etc.
   - Default timeout: 60 seconds. Max allowed: 300 seconds (for heavy queries)
3. In your HTML/JS, call endpoints via relative URL:
   ```javascript
   const response = await fetch('./api/get_sales', {
     method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({ params: { date_from: '2026-01-01' }, timeout: 60 })
   });
   const data = await response.json();
   ```
4. The `timeout` field in the request body controls how long the script can run (default 60s, max 300s).
   Use higher values for complex analytics queries on large databases.

## Data Endpoint Script Template
```python
#!/usr/bin/env python3
"""Data endpoint: describe what this returns."""
import json
import sys
import sqlite3

def main():
    params = json.loads(sys.stdin.read() or '{}')

    # Example: query a database
    db_path = '/app/workspace/files/data.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT * FROM sales WHERE date >= ? LIMIT ?",
        (params.get('date_from', '2020-01-01'), params.get('limit', 100))
    )
    columns = [desc[0] for desc in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()

    print(json.dumps({"columns": columns, "rows": rows, "total": len(rows)}))

if __name__ == '__main__':
    main()
```

## Schema.org Microdata Markup (REQUIRED)

**You MUST add schema.org microdata attributes to all significant data elements** in every HTML page you build. This enables the platform's chat assistant to understand what the user is currently viewing and provide smarter, context-aware responses.

Three HTML attributes work together:
- `itemscope` — marks an element as containing structured data
- `itemtype="https://schema.org/TypeName"` — specifies the type of data
- `itemprop="propertyName"` — marks a specific property value within the item

Add them to all significant data elements: tables, metric cards, filter forms, list items, navigation sections, and status badges. Every meaningful data display MUST have schema.org attributes — the chat assistant's context-awareness depends on it.

For full HTML examples for each element type, read `/app/core/webapp-framework/SCHEMA_EXAMPLES.md`.

## Context Bridge Script (REQUIRED)

The script `./assets/context-bridge.js` is **auto-available** in every webapp — you do not need to create it. Just include it as the last `<script>` tag before `</body>` in every HTML page:

```html
  <script src="./assets/context-bridge.js"></script>
</body>
```

For pages in subdirectories (e.g., `pages/detail.html`), adjust the path: `../assets/context-bridge.js`. Do not skip it on any page — the chat assistant cannot provide context-aware help without it, and agent action commands will not work.

## Agent-to-Webapp Actions

You can trigger UI changes in the webapp from your responses by embedding action tags:

```
<webapp_action>{"action": "action_type", "data": { ... }}</webapp_action>
```

Tags are automatically stripped from the visible chat message — the user only sees your regular text. Actions execute silently in the webapp.

| Action | When to use |
|---|---|
| `refresh_page` | After code changes in building mode |
| `reload_data` | After updating backend data in conversation mode |
| `update_form` | To apply filter changes the user asked for in natural language |
| `show_notification` | To confirm completed actions or report errors |
| `navigate` | To take the user to a relevant section in an SPA |

For full documentation — data field specs, custom event listeners, and examples — read `/app/core/webapp-framework/ACTIONS_REFERENCE.md`.

## Best Practices
- Start with a single `index.html` for simple dashboards
- Use Tailwind CSS (via CDN) for clean, responsive layouts
- Use Chart.js or Plotly.js for charts, AG Grid for data tables
- Add loading states and spinners for data fetches
- Handle API errors gracefully in the UI (show user-friendly messages)
- For multi-page apps, use client-side routing or simple `<a>` links between HTML files
- Document your data endpoints in `webapp/api/README.md`
- **Add schema.org microdata to all data elements** (see Schema.org section above)
- **Include context-bridge.js in every HTML page** (see Context Bridge Script section above)

## Size Limit
Total webapp directory must stay under 100MB. Keep assets optimized.
Use minified CSS/JS when possible. Optimize images.

## What NOT to Do
- Do not leave dev servers running (`npm run dev`, `python -m http.server`, etc.)
- Do not leave `node_modules/` in the webapp directory — build and clean up
- Do not use server-side rendering frameworks that require a running server (Next.js SSR, Nuxt SSR)
- Do not write to files outside of `/app/workspace/webapp/`
- Do not hardcode absolute URLs — always use relative paths (`./api/endpoint`, `./assets/style.css`)
