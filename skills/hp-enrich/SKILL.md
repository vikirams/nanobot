---
name: hp-enrich
description: Highperformr Enrichment — injected automatically when the /enrich slash command runs. Covers single-contact and batch enrichment via LinkedIn URL or name+website, field presets, async polling, and result presentation.
metadata: '{"nanobot": {"always": false}}'
---

# HP Enrich Skill

Injected after `/enrich` executes, or when the user asks to enrich contacts from a resultset.

---

## Active Resultset

When `Active Resultset ID` is present in the runtime context, pass it as `resultset_id` to `mcp_execute`. The server pre-loads `const data = [...]` automatically — do not re-fetch.

---

## Enrichment Inputs

Two input paths — only one is needed per contact:

| Path | Required fields |
|------|----------------|
| LinkedIn URL | `linkedin_url` |
| Name + website | `first_name` + `last_name` + `company_website` |

**Field presets** (`fields` argument):

| Preset | What it returns |
|--------|----------------|
| `basic` (default) | Name, title, company, location, LinkedIn URL |
| `email` | + verified email address |
| `phone` | + phone number |
| `full` | All of the above |

Multiple presets can be combined: `email,phone`.

---

## Single Contact — Code Pattern

Search for the enrichment endpoint first (`mcp_search`), then execute:

```javascript
async ({ hp, sendProgress }) => {
  sendProgress('Enriching contact...');
  return hp.post('/api/enrichment', {
    linkedin_url: 'https://linkedin.com/in/example',  // OR use name + company_website
    // first_name: 'Jane', last_name: 'Smith', company_website: 'acme.com',
    fields: 'email,phone',  // omit for basic only
  });
}
```

---

## Batch Enrichment — From Active Resultset

When the user wants to enrich multiple contacts from the current resultset:

```javascript
async ({ hp, sendProgress, data }) => {
  const results = [];
  for (let i = 0; i < data.length; i++) {
    const contact = data[i];
    sendProgress('Enriching ' + (i + 1) + ' of ' + data.length + '...');
    try {
      const enriched = await hp.post('/api/enrichment', {
        linkedin_url: contact.linkedIn || contact.linkedin_url,
        fields: 'email',  // adjust as needed
      });
      results.push({ ...contact, ...enriched });
    } catch (e) {
      results.push({ ...contact, _enrichError: e.message });
    }
  }
  return results;
}
```

Batch enrichment is rate-sensitive — process sequentially, not in parallel.

---

## Async Polling

If the enrichment API returns `{ status: "pending", job_id: "..." }`:

1. Inform the user the job is running asynchronously.
2. Poll with `hp.get('/api/enrichment/jobs/{job_id}')` until `status === "complete"`.
3. Present results when ready.

---

## Result Presentation

Present the enriched contact as a structured summary:

1. **Name** + **Title** at **Company**
2. **Email** (if requested and found)
3. **Phone** (if requested and found)
4. **LinkedIn URL**
5. **Location** (city, country)
6. Any other fields returned

For batch results: show a summary table (name, email found/not, phone found/not), then offer to download as CSV.

---

## Error Handling

- First failure: analyse the error (missing required fields, invalid URL, rate limit), correct and retry once.
- Second failure: stop. Tell the user what data was needed and what failed.
- For batch: continue processing remaining contacts even if individual enrichments fail; report failures at the end.
