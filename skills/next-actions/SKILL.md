---
name: next-actions
description: Always-loaded inference table for follow-up suggestions. After every HP GTM result, use this skill to determine and present the most relevant next steps for the user based on what they just did.
metadata: '{"nanobot": {"always": true}}'
---

# Next Actions — Inference Guide

After **every** HP GTM result, close your response with a short **"What's next?"** line that surfaces the most relevant follow-up options for what the user just did.

**Format:**
```
💡 **What's next?** [2–4 short suggestions separated by ·]
```

Keep it to one line. Each suggestion is a short natural-language phrase the user can say or click.

---

## Inference Table

Use the user's most recent action to pick the right suggestions.

---

### 🔍 User ran a discovery / found contacts or companies

```
💡 **What's next?** Download as CSV · Push to Segment · Enrich emails · Analyse by field
```

Options to offer (pick the most relevant 3–4):
- **Download as CSV** — export the result for use in spreadsheets or other tools
- **Push to Segment** — import contacts into an HP Segment for targeting
- **Push to webhook** — send to an outreach tool (HeyReach, Instantly, Smartlead…)
- **Enrich emails** — add verified email addresses to the contacts
- **Enrich phones** — add phone numbers
- **Analyse by field** — break down by industry, country, title, company size, etc.
- **Filter the list** — narrow down by criteria (e.g. only VP+, only UK, only SaaS)

---

### 📋 User listed all segments

```
💡 **What's next?** Fetch contacts from a segment · Search for a segment by name
```

Options to offer:
- **Fetch contacts from [segment name]** — pull the full contact list from a specific segment
- **Search for a segment** — find a segment by partial name
- **Show contact count for each** — if not already shown

---

### 👥 User fetched contacts from a segment

```
💡 **What's next?** Download as CSV · Check field fill rate · Analyse by industry · Enrich emails
```

Options to offer:
- **Download as CSV** — export the full contact list
- **Check field fill rate** — identify which columns are populated vs. empty (useful to spot enrichment gaps)
- **Analyse by field** — break down by country, industry, title, headcount, etc.
- **Enrich emails** — fill in missing email addresses
- **Enrich phones** — fill in missing phone numbers
- **Push to webhook** — send to an outreach tool
- **Filter the list** — subset by specific criteria

---

### 📊 User ran an analysis or visualisation

```
💡 **What's next?** Filter contacts matching this criteria · Download the underlying data · Push filtered list to Segment
```

Options to offer:
- **Filter contacts matching [top result]** — e.g. "show me only the Finance contacts"
- **Download underlying data as CSV** — export the full dataset the analysis ran on
- **Push filtered list to Segment** — create a segment from a specific slice
- **Analyse by another field** — pivot the analysis by a different column

---

### ✉️ User enriched a contact (email / phone / basic)

```
💡 **What's next?** Download as CSV · Push to Segment · View full profile
```

Options to offer:
- **Download as CSV** — export enriched results
- **Push to Segment** — add enriched contacts to a segment
- **Enrich more contacts** — if they have a resultset, batch enrich the rest

---

### 🔀 User merged two lists

```
💡 **What's next?** Download merged list as CSV · Push to Segment · Analyse the merged list
```

Options to offer:
- **Download merged list as CSV**
- **Push to Segment** — import the deduplicated list
- **Analyse the merged list** — check industry/country breakdown of the combined set

---

### 🔍 User filtered / subsetted a resultset

```
💡 **What's next?** Download filtered list as CSV · Push to Segment · Push to webhook
```

Options to offer:
- **Download filtered list as CSV**
- **Push to Segment** — import the filtered list
- **Push to webhook** — send to outreach tool
- **Further filter** — apply additional criteria

---

### 📤 User pushed to Segment

```
💡 **What's next?** View the segment in HP · Fetch contacts back from the new segment
```

Options to offer:
- **View the segment** — open in Highperformr UI
- **Fetch contacts from [new segment name]** — confirm what was imported

---

### 📥 User exported / downloaded CSV

No follow-up needed — terminal action. Confirm the download link and close.

---

## Presentation Rules

1. **Always one line** — `💡 **What's next?** ...` at the end of the response, after the data.
2. **Match the context** — pick suggestions relevant to *this specific result*, not generic ones.
3. **Use the actual names** — say "Fetch contacts from *ICP - SaaS CEOs*" not just "Fetch contacts from a segment".
4. **Don't repeat what just happened** — if the user just discovered contacts, don't suggest "Discover contacts".
5. **Max 4 suggestions** — keep it scannable. Omit suggestions that clearly don't apply.

---

## STRICT RULES — Never Violate

- **Do NOT inject Company DNA, ICP personas, or product-specific framing into next-action suggestions.** Suggestions must be generic data operations (download, push, filter, analyse), not persona-targeted outbound actions.
- **Do NOT suggest "find [ICP title] contacts" after an analysis or filter unless the user explicitly asked for contacts.** The user decides when to look for contacts.
- **Do NOT reference the company from Company Context** (e.g. "Snowflake angle:", "for your ICP:") in next-action text. These are generic workflow suggestions.
- **If the user ran a company search and asked for analysis, the next actions are**: analyse further, filter by criteria, download, push — not "find CTOs at these companies".

Bad (never do this):
```
💡 **What's next?** Snowflake ICP hit — get CDO/CTO contacts · Filter to Tech/Finance · Find VPs Data Engineering
```

Good:
```
💡 **What's next?** Filter to Financial Services · Download as CSV · Analyse by company size · Find contacts at these companies
```

---

## Field Fill Rate Analysis (reference)

When the user asks to "check field fill rate" or "identify which columns are populated":

```javascript
// Pass resultset_id from Active Resultset ID
async ({ data }) => {
  const fields = [...new Set(data.flatMap(r => Object.keys(r)))];
  return fields.map(f => {
    const filled = data.filter(r => r[f] != null && r[f] !== "" && r[f] !== "null").length;
    return { field: f, filled, total: data.length, pct: Math.round((filled / data.length) * 100) };
  }).sort((a, b) => b.pct - a.pct);
}
```

Present as a table: `| Field | Filled | Total | % |` sorted by fill rate descending.
