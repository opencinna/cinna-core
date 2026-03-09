# Schema.org Microdata Markup — Full Examples

**You MUST add schema.org microdata attributes to all significant data elements** in every HTML page you build. This enables the platform's chat assistant to understand what the user is currently viewing and provide smarter, context-aware responses.

## What are schema.org markers?

Three HTML attributes work together:
- `itemscope` — marks an element as containing structured data
- `itemtype="https://schema.org/TypeName"` — specifies the type of data
- `itemprop="propertyName"` — marks a specific property value within the item

## Where to add markers

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

## Rule: Mark every meaningful data display

Every table, card, metric, filter, and key content section MUST have appropriate schema.org attributes. Do not leave data displays without markers — the assistant's ability to help the user depends on it.
