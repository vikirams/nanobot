---
name: hp-segments
description: Highperformr Segments — injected automatically when /segment or /segments slash commands run. Covers the segments 2-step flow (list then fetch), active resultset handling, partial name matching, and data presentation for segment contacts.
metadata: '{"nanobot": {"always": false}}'
---

# HP Segments Skill

Injected after `/segments` (list) or `/segment` (fetch contacts) executes.

---

## Active Resultset

**CRITICAL**: When `Active Resultset ID` is present in the runtime context:

1. Pass it directly as `resultset_id` to `mcp_execute` — the server pre-loads `const data = [...]`.
2. **Do NOT call `list_resultsets`** — reads a different store, may be empty even when data exists.
3. **Do NOT re-fetch** — never re-run a segment fetch when data is already stored.

When a filter produces a new resultset, that becomes the new active ID.

---

## 2-Step Tool Protocol (for follow-up requests)

**Step 1** — `mcp_<server>_search` to find the right endpoint.
**Step 2** — `mcp_<server>_execute` to run the code. Pass `resultset_id` when working with stored data.

---

## /segments Two-Step Flow

1. `/segments` → `list_segments` → present list with segment names and contact counts.
2. Wait for user to select or confirm a segment name or UUID.
3. Call `fetch_segment_contacts` (or equivalent via `execute`) for the chosen segment.
4. If result has `status: "fetching"`, inform the user and offer partial analysis on available records.

**Partial name matching**: If the user gives a partial name (e.g. "SaaS CEOs" and the segment is "ICP - SaaS CEOs"), use the list from Step 1 to fuzzy-match and confirm with the user before fetching.

---

## Large Segments

For segments with >1000 contacts, fetching may be paginated or take time. If the response shows a `total` higher than what's returned:

- Inform the user: "Showing N of M contacts — full fetch may take a moment."
- Offer to analyse the available portion while the rest loads.
- When `status: "fetching"` clears, re-present results with the full count.

---

## Data Receipt Protocol

After contacts are fetched:

1. Summary: "Found **N** contacts in **[Segment Name]**."
2. Preview table — first 5 rows, key columns (name, email, title, company, country).
3. Sentinel: `[Preview](#preview-last)` on its own line.
4. Actions: `📥 Download CSV | 📤 Push to Segment | 🔗 Push to Webhook | 📊 Analyse`

---

## Error Handling

- First failure: analyse the error, adjust, retry once.
- Second failure: stop. Tell the user the capability is unavailable.
