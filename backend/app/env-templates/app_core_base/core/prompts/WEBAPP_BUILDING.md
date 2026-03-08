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

### What are schema.org markers?

Three HTML attributes work together:
- `itemscope` — marks an element as containing structured data
- `itemtype="https://schema.org/TypeName"` — specifies the type of data
- `itemprop="propertyName"` — marks a specific property value within the item

### Where to add markers

**Data tables** — wrap `<table>` with `itemscope itemtype="https://schema.org/Dataset"`, mark each column header with `itemprop="name"`, and each data cell with `itemprop="value"`:
```html
<table itemscope itemtype="https://schema.org/Dataset">
  <caption itemprop="name">Monthly Sales</caption>
  <thead><tr><th itemprop="description">Month</th><th itemprop="description">Revenue</th></tr></thead>
  <tbody>
    <tr itemscope itemtype="https://schema.org/DataFeedItem">
      <td itemprop="name">January</td>
      <td itemprop="value">$12,400</td>
    </tr>
  </tbody>
</table>
```

**Metric cards / KPI tiles** — use `QuantitativeValue` for numbers with units:
```html
<div itemscope itemtype="https://schema.org/QuantitativeValue">
  <span class="label" itemprop="name">Total Revenue</span>
  <span class="value" itemprop="value">$2.4M</span>
  <span class="unit" itemprop="unitText">USD</span>
</div>
```

**Filter / search forms** — wrap form with `itemscope itemtype="https://schema.org/SearchAction"`, mark inputs with `itemprop`:
```html
<form itemscope itemtype="https://schema.org/SearchAction">
  <label itemprop="name">Date Range</label>
  <input itemprop="query-input" name="date_from" value="2026-01-01" />
  <select itemprop="query-input" name="region"><option value="north">North</option></select>
</form>
```

**List items / entity cards** — use `Thing` or a more specific type:
```html
<div itemscope itemtype="https://schema.org/Thing">
  <h3 itemprop="name">Invoice #INV-2026-001</h3>
  <p itemprop="description">ACME Corp — $1,500</p>
  <span itemprop="identifier">INV-2026-001</span>
</div>
```

**Navigation / section headers** — mark page sections with `WebPageElement`:
```html
<section itemscope itemtype="https://schema.org/WebPageElement">
  <h2 itemprop="name">Q1 Performance</h2>
</section>
```

**Status badges / labels** — mark status text with `itemprop="actionStatus"` or `itemprop="value"`:
```html
<span itemscope itemtype="https://schema.org/Thing" itemprop="actionStatus">Active</span>
```

### Rule: Mark every meaningful data display

Every table, card, metric, filter, and key content section MUST have appropriate schema.org attributes. Do not leave data displays without markers — the assistant's ability to help the user depends on it.

---

## Context Bridge Script (REQUIRED)

**You MUST include the context bridge script in every HTML page.** This script enables the platform's chat widget to collect the schema.org data from your page and send it to the assistant.

### Setup (do this for every HTML page)

1. Create `webapp/assets/context-bridge.js` with the following content — **copy it exactly, do not modify**:

```javascript
// Webapp Context Bridge — do not remove or modify
// Enables the platform chat assistant to read page context.
(function () {
  function collectMicrodata() {
    var items = [];
    document.querySelectorAll('[itemscope]').forEach(function (el) {
      var item = {
        type: el.getAttribute('itemtype') || '',
        properties: {}
      };
      el.querySelectorAll('[itemprop]').forEach(function (prop) {
        var name = prop.getAttribute('itemprop');
        var value = (prop.tagName === 'INPUT' || prop.tagName === 'SELECT' || prop.tagName === 'TEXTAREA')
          ? prop.value
          : prop.textContent.trim();
        if (name && value !== '') {
          if (Object.prototype.hasOwnProperty.call(item.properties, name)) {
            if (!Array.isArray(item.properties[name])) {
              item.properties[name] = [item.properties[name]];
            }
            item.properties[name].push(value);
          } else {
            item.properties[name] = value;
          }
        }
      });
      if (Object.keys(item.properties).length > 0) {
        items.push(item);
      }
    });
    return items;
  }

  window.addEventListener('message', function (event) {
    if (!event.data || event.data.type !== 'request_page_context') return;
    var context = {
      url: window.location.href,
      title: document.title,
      microdata: collectMicrodata()
    };
    event.source.postMessage({ type: 'page_context_response', context: context }, event.origin || '*');
  });
})();
```

2. Include it as the **last `<script>` tag** before `</body>` in every HTML page:
```html
  <script src="./assets/context-bridge.js"></script>
</body>
</html>
```

For pages in subdirectories (e.g., `pages/detail.html`), adjust the path: `../assets/context-bridge.js`.

### This is mandatory

Do not skip this script on any page. It is lightweight (< 1KB) and has no performance impact. The chat assistant cannot provide context-aware help without it.

---

## Best Practices
- Start with a single `index.html` for simple dashboards
- Use Tailwind CSS (via CDN) for clean, responsive layouts
- Use Chart.js or Plotly.js for charts, AG Grid for data tables
- Add loading states and spinners for data fetches
- Handle API errors gracefully in the UI (show user-friendly messages)
- For multi-page apps, use client-side routing or simple `<a>` links between HTML files
- Document your data endpoints in `webapp/api/README.md`
- Use meaningful loading states: show skeleton screens or progress indicators while data loads
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
