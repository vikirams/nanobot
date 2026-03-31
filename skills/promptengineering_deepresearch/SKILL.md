---
name: promptengineering_deepresearch
description: Generate a single deep-research prompt (or template) that instructs an autonomous research agent to return a JSON array of companies from a natural-language query. Mandate only company_name, company_website, and reasoning; all other fields must be decided by the research model based on the user query. Use for "deep research companies", "find companies list", "company list as JSON", or when the user wants a prompt (not the research). NOT for running the research yourself.
metadata: '{"nanobot": {"always": false}}'
---

# Explanatory Deep Research Prompt for Company Lists

Generate a **single, runnable prompt** (or template) that instructs an **autonomous** deep-research system to identify companies from the user’s query and return a **JSON array** of company objects.

## How to Generate the Prompt

When turning the user’s natural-language request into the final prompt, act as an expert prompt engineer:

- **Output only the refined prompt** – deliver the prompt (or template) only; no preamble, no “here’s your prompt”, no explanation before or after.
- **Be specific and detailed** – include concrete criteria, field names, types, and quality bars so the model has no ambiguity.
- **Include constraints, format requirements, and context** – state JSON-only, exact keys, minimum count, and any scope (region, industry, date) so the output is bounded and consistent.
- **Use clear structure** – use sections (e.g. CRITERIA, OUTPUT SCHEMA, FORMAT) in the prompt you generate so the downstream model can follow it reliably.

## Critical Research-Agent Behavior (must be in the prompt you generate)

The prompt you generate must instruct the research agent:

- **No clarifying questions**: never ask the user for clarification/confirmation; proceed with best judgment.
- **No mid-task pausing**: never stop to present options; continue until the final JSON is produced.
- **Quality-first**: do not invent companies; only include real companies that can be verified from credible public sources.
- **Missing data**: if an optional field cannot be found, set it to `"N/A"` (never block completion).
- **JSON-only output**: return only the JSON array—no markdown, no code fences, no extra prose.

## Mandatory Response Fields (ONLY these are mandatory)

Every company object in the JSON array **must** include:

| Field | Description |
|-------|-------------|
| `company_name` | Official or commonly used company name. |
| `company_website` | Primary company website URL. |
| `reasoning` | One or two sentences explaining why the company matches the user’s criteria, and **what evidence was used** (describe the proof; do not add a separate required `source_proof` field). |

## Optional Fields (must be chosen by the research model)

- In the prompt you generate, instruct the research model to **choose additional fields** that best fit the user’s request (e.g. `industry`, `country`, `headquarters_location`, `linkedin_url`, `employee_range`, `certifications`, `partner_type`, `services_focus`, `products`, `keywords`).
- The prompt must require the model to **declare its chosen optional fields up front**, then use the **same set of optional fields for every company object** (use `"N/A"` when unknown).
- Do **not** hardcode a fixed optional schema in this skill; the model should decide based on the user query.

## Output Format in the Prompt

In the generated prompt, require:

1. **JSON only** – response must be a single JSON array, no markdown or prose wrapper unless the tool requires it.
2. **Schema** – state the exact keys and types: the 3 mandatory keys above + the model-chosen optional keys (declared before listing companies).
3. **Minimum count** – require “at least N companies” where N is taken from the user request; if the user gave no number, set a sensible target (e.g. 50–200) and state it explicitly.
4. **Quality** – require `company_website` to be a valid URL; require `reasoning` to reference the user’s criteria and the evidence used.

## Scale and Efficiency

- Tell the model to **prioritize breadth** while keeping the three mandatory fields present and truthful.
- If the target is large (e.g. 200+), instruct the model to **batch the research** (by region/segment/category) to avoid timeouts, while still returning one final JSON array.
- If the user’s query is vague, the prompt you generate should **self-narrow** scope (e.g. pick a region, define segments) to keep results relevant.

## Example Prompt Shape

Use this structure when generating the user’s prompt (fill placeholders from the user query):

```
You are an autonomous deep-research agent. Complete the task fully without asking clarifying questions.

Identify at least <N> real companies that match the criteria below. Do not invent companies.

CRITERIA:
<user query pasted or paraphrased here>

Before listing companies, decide the most relevant optional fields based on the criteria and keep them consistent across all companies.

Return a single JSON array. Each array item must be an object with:
- company_name (string)
- company_website (string, valid URL)
- reasoning (string, 1–2 sentences; must cite the evidence used and why it matches the criteria)
- <your chosen optional fields> (consistent keys across all objects; use "N/A" if unknown)

Respond with the JSON array ONLY. No markdown, no code fence, no extra text.
```

## What You Produce

- **Deliver** a ready-to-use prompt (or template with clear placeholders) that the user can paste into their deep-research tool.
- **Include** the exact mandatory fields and the instruction that optional fields must be chosen by the research model and made consistent across all items.
- **Do not** run the research yourself; only generate the prompt text.
