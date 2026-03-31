---
name: highperformr
description: Highperformr GTM Engine — general reference for natural-language HP requests not triggered by a slash command. Use for any question about HP tools, how to find/fetch/analyse contacts and companies, or when no focused skill was automatically injected.
metadata: '{"nanobot": {"always": false}}'
---

# Highperformr GTM Engine

Use for HP-related natural-language questions. For slash-command operations, focused skills are injected automatically:

| Slash Command | Skill Injected |
|---------------|---------------|
| `/discovery`  | `hp-discovery` |
| `/segment`    | `hp-segments` |
| `/segments`   | `hp-segments` |
| `/enrich`     | `hp-enrich` |
| `/merge`      | `hp-dataops` |
| `/dedupe`     | `hp-dataops` |
| `/push-to-segment` | `hp-dataops` |

---

## MCP Tools

Two tools are available:

- `mcp_<server>_search` — RAG search over the Highperformr OpenAPI spec to find the right endpoint or pattern
- `mcp_<server>_execute` — runs a sandboxed JavaScript function with `hp` client and optional `resultset_id` pre-loading

**Standard pattern:**
```
search → find endpoint → execute → return result
```

Skip `search` only when you already know the exact endpoint or pattern from this session.

---

## Active Resultset

When `Active Resultset ID` appears in the runtime context, pass it as `resultset_id` to `mcp_execute`. The server pre-loads `const data = [...]` automatically — do not re-fetch.

---

## Field Prefix Rules — CRITICAL

When writing filter or field-access code:

| ❌ Wrong | ✅ Correct |
|---|---|
| `contact.companyName` | `company.companyName` |
| `contact.companyWebsite` | `company.companyWebsite` |
| `contact.companyIndustry` | `company.companyIndustry` |
| `contact.companyHeadcount` | `company.companyHeadcount` |

**Rule:** `company.*` for all company fields. `contact.*` for personal fields only (name, email, title, phone, LinkedIn, country).

The `contact.sources` condition is **always required** in `filter-contacts` calls.

---

## Error Handling

- First failure: analyse, adjust, retry once.
- Second failure: stop and report.
