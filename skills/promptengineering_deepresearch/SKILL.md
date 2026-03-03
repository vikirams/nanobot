---
name: deep-research-promptgen
description: >
  Generates optimized structured deep research prompts for Google-grounded discovery (e.g. Gemini-3-Flash).
  Use when calling the Highperformr discover tool with type="deepsearch".
  Generic: use for any research task — companies, contacts, events, products, research papers, grants,
  "find X that match Y", "build a deep research prompt", or any discovery requiring web grounding.
---

# Deep Research Prompt Generation (Generic)

Generate a **precise, schema-locked prompt** for Google-grounded web research. The prompt must be optimized for systematic search and **strict JSON-only output**. The model must **infer the required JSON attributes from the user's query** and the generated prompt must instruct the engine to return only a valid JSON array with those attributes — no markdown, no prose, no code fences.

---

## Step 1: Classify Target & Entity Type

| Query signals | Mode | Entity type |
|---|---|---|
| Job titles, roles, skills, education, past experience | **CONTACT** | Professional contacts |
| Company attributes: industry, location, headcount, funding, tech stack | **COMPANY** | Companies |
| Events, conferences, webinars, meetups | **CUSTOM** | Events |
| Products, tools, software, solutions | **CUSTOM** | Products |
| Research papers, studies, reports | **CUSTOM** | Research / publications |
| Grants, funding opportunities, programs | **CUSTOM** | Opportunities |
| "Find X that...", "List all Y where...", other concrete nouns | **CUSTOM** | Infer from query (X or Y) |
| Both people and companies | **CONTACT** | Contacts (embed company filters as context) |

For **CUSTOM**, set `entity_type` (singular) and `entity_type_plural` (e.g. "event" / "events") from the user's goal.

---

## Step 2: Extract Filter Dimensions (Criteria)

**Preset — Contact:** Title, Seniority, Company, Industry, Location, Past Experience, Education, Skills, Company Size, Funding Stage, Tech Used, Keywords

**Preset — Company:** Industry/Vertical, Location, Revenue, Headcount, Funding Stage/Amount, Investors, Tech Stack, Keywords, Business Model, Year Founded

**Custom:** Infer from the user's query. Typical dimensions:
- **What** (type, category, topic, domain)
- **Where** (location, region, platform, source)
- **When** (date range, recency, deadline)
- **Scale** (size, volume, budget, stage)
- **Attributes** (features, constraints, exclusions)

Use search-friendly terms (concrete names, ranges, categories) so Google research can match them. For high-volume goals (e.g. 100+), avoid over-constraining.

---

## Step 3: Build Criteria Block

Translate filters into numbered, explicit prose:

```
1. [Dimension]: [Specific value or range]
2. [Dimension]: [Specific value or range]
```

Make implicit filters explicit. One line per dimension. Omit dimensions the user did not specify.

---

## Step 4: Infer JSON Schema from User Query & Set Volume

**Rule: The model must figure out the required JSON attributes from the user's query.** Do not use a fixed schema unless the entity type is CONTACT or COMPANY (presets).

**How to infer schema:**
- Read the user query for **explicit asks** (e.g. "with dates and locations" → include `date`, `location`).
- For the **entity type**, include attributes that are typically needed:
  - **Events:** name, date, location, url, organizer, type (e.g. conference/webinar).
  - **Products:** name, vendor, category, url, description or price_range.
  - **Research/Publications:** title, authors, year, url, source, abstract_or_summary.
  - **Grants/Opportunities:** name, deadline, amount_or_scope, url, eligibility.
- Always include at least: **identifier/name**, **url or source** (for verification), and **match_reason** (why it matches the criteria).
- Mark fields as required vs optional (optional can be null if unverifiable).

**Preset (CONTACT / COMPANY):** Use the schema and volume from the preset templates below.

**Custom:** Set `{{output_schema}}` to a single-line JSON object with the inferred field names and short descriptions (e.g. `{"name":"...","url":"...","date":"...","match_reason":"..."}`).

Set **volume**: min and max results from the query (e.g. "100+" → 100–200). Default: 10–100 for custom, 10–200 for presets.

---

## Step 5: Research Strategy (All Modes)

The prompt should instruct the engine to:

1. **Use Google (or equivalent web search)** to find entities — do not rely on memory alone.
2. **Search multiple angles** (sub-categories, regions, keywords, time ranges) to maximize coverage when volume is requested.
3. **Prioritize verifiable results**: only include items that can be confirmed via web (URL, source, or credible listing).

Embed these in the chosen template.

---

## Step 6: Fill Template

### Option A — Preset: CONTACT

```
You are a structured professional contact discovery engine using grounded web research (e.g. Google search).

Your task is to discover real professional contacts matching ALL of the following criteria.

DISCOVERY CRITERIA:
{{criteria_block}}

TARGET ENTITY TYPE: Professional Contacts

REQUIRED OUTPUT FORMAT: Return ONLY a valid JSON array. Each object MUST contain EXACTLY these fields:
{"name":"Full name","linkedin_url":"https://linkedin.com/in/...","title":"Current job title","company_name":"Current employer","company_linkedin":"https://linkedin.com/company/...","company_url":"Company website URL","location":"City, Country","match_reason":"One sentence why this person matches"}

STRICT JSON RESPONSE:
- Your entire response MUST be a single valid JSON array. No markdown (no \`\`\`json), no text before or after, no explanations.
- Each array element is an object with EXACTLY the fields listed above. Use null for unverifiable fields.

OTHER REQUIREMENTS:
1. DO NOT invent or hallucinate contacts. Use web search to verify.
2. ONLY include contacts with verifiable LinkedIn profiles.
3. ALL criteria must be satisfied — no partial matches.

Return between 10 and 200 results. Maximize count while maintaining quality.
```

### Option B — Preset: COMPANY

```
You are a structured company discovery engine. Use Google (or equivalent web search) to systematically find real companies — do not rely on memory alone. Search multiple angles (sub-industries, regions, keywords) to maximize coverage.

Your task is to discover real companies matching ALL of the following criteria. Aim for at least 100 companies; return up to 200 if available.

DISCOVERY CRITERIA:
{{criteria_block}}

TARGET ENTITY TYPE: Companies

REQUIRED OUTPUT FORMAT: Return ONLY a valid JSON array. Each object MUST contain EXACTLY these fields:
{"name":"Company name","industry":"Primary industry or vertical","linkedin_url":"https://linkedin.com/company/... — REQUIRED: find the company LinkedIn page via web search, use null only if truly unfindable","website":"https://... — REQUIRED: find the company official website via web search, use null only if truly unfindable","location":"HQ City, Country","size":"e.g. 50-200","funding_stage":"e.g. Series B, Bootstrapped, Public","tech_stack":["tool1","tool2"] or [],"match_reason":"One sentence why this company matches"}

STRICT JSON RESPONSE:
- Your entire response MUST be a single valid JSON array. No markdown (no \`\`\`json), no text before or after, no explanations.
- Each array element is an object with EXACTLY the fields listed above. Use null for unverifiable fields.

OTHER REQUIREMENTS:
1. DO NOT invent or hallucinate companies. Every entry must be findable via web search.
2. For EVERY company, actively search for its official website URL and LinkedIn company page URL — these are critical fields.
3. ONLY include real companies with active web presence (website, LinkedIn, or credible listing).
4. ALL criteria must be satisfied — no partial matches.

VOLUME: Return at least 100 companies and up to 200. Use multiple search queries and angles to reach this count while keeping all results criteria-compliant.
```

### Option C — Generic (CUSTOM) Template

```
You are a structured discovery engine for {{entity_type_plural}} using grounded web research (e.g. Google search). Do not rely on memory alone — use search to find real {{entity_type_plural}} that match the criteria. Search multiple angles (categories, regions, keywords) to maximize coverage.

Your task is to discover real {{entity_type_plural}} matching ALL of the following criteria.

DISCOVERY CRITERIA:
{{criteria_block}}

TARGET ENTITY TYPE: {{entity_type_plural}}

REQUIRED OUTPUT FORMAT: Return ONLY a valid JSON array. Each object MUST contain EXACTLY these fields (inferred from the user's research need):
{{output_schema}}

STRICT JSON RESPONSE:
- Your entire response MUST be a single valid JSON array. No markdown (no \`\`\`json), no text before or after, no explanations.
- Each array element is an object with EXACTLY the fields listed above. Use null for unverifiable fields.

OTHER REQUIREMENTS:
1. DO NOT invent or hallucinate {{entity_type_plural}}. Every entry must be verifiable via web search.
2. ALL criteria must be satisfied — no partial matches.

VOLUME: Return between {{min_results}} and {{max_results}} results. Use multiple search queries if needed to reach the target while keeping all results criteria-compliant.
```

For **Option C**, fill placeholders:

- `{{entity_type}}` / `{{entity_type_plural}}`: e.g. "event" / "events", "product" / "products" (from Step 1).
- `{{criteria_block}}`: from Step 3.
- `{{output_schema}}`: **Infer from user query** (Step 4). Single-line JSON object with field names and short descriptions, e.g. `{"name":"...","url":"...","date":"...","match_reason":"..."}`. Include only attributes that are relevant to the entity type and user ask.
- `{{min_results}}` / `{{max_results}}`: from user or default (e.g. 10 and 100, or 100 and 200).

---

## Step 7: Output

The rendered prompt (all placeholders replaced) is the **GENERATED_PROMPT**.

**Pass only this prompt as the `query` argument.** Do not prepend a MODE/FILTERS summary.

MCP call:
```
discover(type="deepsearch", query="<GENERATED_PROMPT>", expected_format="json")
```

`<GENERATED_PROMPT>` must start with "You are a structured..." — nothing before it.

---

## Edge Cases

| Situation | Handling |
|---|---|
| User asks for "100+ companies" | Use COMPANY preset; avoid overly narrow criteria. |
| Ambiguous seniority (contacts) | Default to "Manager and above". |
| No location / date / scope specified | Omit that dimension; global or open-ended search. |
| Very broad query | Generate with reasonable defaults; note it and suggest narrowing. |
| Both contacts AND companies | Split into two separate prompts. |
| Custom entity, no schema given | Infer minimal schema: identifier, name/title, url/source, match_reason. |
| Filters too narrow for requested volume | Widen slightly in criteria and instruct to search multiple sub-segments. |

---

## Post-Result Validation

After receiving results:

1. **Strict JSON only:** If the response contains markdown, prose, or text outside a JSON array, strip it and parse only the JSON array. If no valid JSON array is present, treat as failure and request a retry with strict JSON.
2. Parse the JSON array.
3. Discard records with null **required** fields (preset: use template schema; custom: use inferred required fields from the prompt).
4. Validate URL/format fields if present (`url`, `linkedin_url`, `company_url`, etc.).
5. Deduplicate by a primary key (preset: `website` or `linkedin_url`; custom: first unique field such as `url` or `id`).
6. If result count < user's volume target: suggest re-run with relaxed criteria or report the gap.

Return the clean, normalized dataset as strict JSON.
