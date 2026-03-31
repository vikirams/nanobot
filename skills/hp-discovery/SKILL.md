---
name: hp-discovery
description: Highperformr Discovery — injected automatically when the /discovery slash command runs. Covers active resultset handling, data presentation, analysis, and the exact workflow for finding contacts at known companies.
metadata: '{"nanobot": {"always": false}}'
---

# HP Discovery Skill

Injected after `/discovery` executes. The result set is already stored — your job is to present it and offer next steps.

---

## Active Resultset — CRITICAL RULES

When `Active Resultset ID` is present in the runtime context:

1. Pass it directly as `resultset_id` to `mcp_execute` — the server pre-loads `const data = [...]` for you.
2. **Do NOT call `list_resultsets`** — reads a different store, returns empty even when data exists.
3. **Do NOT re-fetch** — the data is already stored; analysing or filtering it does not need a new discovery call.
4. When execute returns a new `resultset_id`, that becomes the new active resultset for follow-ups.

---

## Data Receipt — Present Results

After the discovery result arrives:

1. Summary line: "Found **N** contacts/companies."
2. Preview table — first 5 rows, columns: name, title, company, email, country (or website, industry, size for companies).
3. Sentinel: `[Preview](#preview-last)` on its own line.
4. Actions: `📥 Download CSV | 📤 Push to Segment | 🔗 Push to Webhook | 📊 Analyse`

---

## Workflow: Find Contacts at Known Companies

When the user has a **company** resultset active and asks for contacts ("find contacts", "find people", "who works at these companies", "find decision makers"):

**Do NOT call search_api. Do NOT use find-contacts-v2. Go directly to execute.**

Extract company domains from the active resultset and call `hp-discovery-tool`:

```javascript
async ({ hp, sendProgress, data }) => {
  const domains = data
    .map(c => c.website || c.companyWebsite || c.domain)
    .filter(Boolean);

  if (domains.length === 0) {
    return { error: 'No company domains found in the active result set.' };
  }

  sendProgress('Finding contacts at ' + domains.length + ' companies...');

  // Default: find ALL contacts — no title filter.
  return hp.post('/api/discovery-search/hp-discovery-tool', {
    query: 'contacts at these companies: ' + domains.join(', ')
  });
}
```

**Title filter rules — strictly follow these:**

| What the user said | Query to use |
|---|---|
| "find contacts" / "find people" / "find all contacts" (no roles mentioned) | `contacts at these companies: domain1, domain2, ...` |
| "find VP Sales" / "find CTOs" / specified roles explicitly | `VP Sales at these companies: domain1, domain2, ...` |
| "find our ICP" / "find our personas" | Use the primary persona titles from Company Context: `[titles] at these companies: domain1, ...` |

Never assume ICP titles unless the user said "ICP", "personas", or explicitly named roles.

---

## Workflow: Analyse / Filter Existing Resultset

For operations on already-fetched data (count by field, filter by value, top-N, distribution):

Go directly to `mcp_execute` with `resultset_id` — no search_api step needed.

```javascript
// Group by a field
async ({ hp, sendProgress, data }) => {
  sendProgress('Grouping by industry...');
  const counts = {};
  for (const r of data) {
    const val = r.industry ?? r.company?.companyIndustry ?? 'Unknown';
    counts[val] = (counts[val] || 0) + 1;
  }
  const total = data.length;
  return {
    type: 'visualization',
    title: 'Industry breakdown',
    total,
    unit: 'companies',
    series: Object.entries(counts)
      .sort(([, a], [, b]) => b - a)
      .map(([label, value]) => ({ label, value, percent: Math.round(value / total * 100) })),
  };
}
```

---

## Field Prefix Rules

When writing filter or analysis code, use the correct prefix:

| ❌ Wrong | ✅ Correct |
|---|---|
| `contact.companyName` | `company.companyName` |
| `contact.companyWebsite` | `company.companyWebsite` |
| `contact.companyIndustry` | `company.companyIndustry` |

`company.*` for company fields, `contact.*` for personal fields only.

---

## Workflow: Use search_api

Only call `mcp_search` when you need to discover an **unfamiliar HP API** — enrichment, segment management, webhook push. For discovery and analysis of the current resultset, go directly to execute.

---

## Analysis Suggestions

Proactively suggest 2–3 analyses based on the result shape:

| Field type | Suggested analysis |
|---|---|
| String, cardinality ≤ 50 | Group-by count (industry split, country breakdown) |
| Number (headcount, revenue) | Distribution buckets (0–50, 50–200, 200–500…) |
| String, high cardinality | Top-N (top 10 companies by name) |

---

## VisualizableResult Rendering

When execute returns `{ "type": "visualization", ... }`:

1. Summary: `{title}` with `total` and `unit`.
2. Table: render `series` as `| Label | Value | % |`.
3. If `chart_hint` is `"pie"` or `"bar"`: append `📊 Chart available in UI`.
4. If `chart_hint` is `"table"` and `columns`/`rows` present: render those instead.

---

## Error Handling

- First failure: read the error, adjust the code, retry once with a corrected approach.
- Second failure: stop and tell the user the capability is unavailable, suggest they try `/discovery` again.
