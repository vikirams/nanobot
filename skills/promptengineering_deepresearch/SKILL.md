---
name: deep-research-promptgen
description: >
  Generates optimized structured deep research prompts for Gemini-3-Flash grounded discovery from natural language queries.
  Use this skill whenever a user wants to find contacts, leads, companies, or professionals using any combination of filters
  such as job title, education, past experience, location, revenue, funding, tech stack, or any other attribute.
  Triggers on phrases like "find me contacts who...", "discover companies that...", "build a prompt to search for...",
  "research professionals with...", "generate a deep research prompt for...", or any lead/prospect discovery intent.
  Always use this skill before passing a prompt to the MCP discover tool with type="deepsearch".
---

# Deep Research Prompt Generation Skill

## Purpose

Parse any natural language discovery query and generate a **precise, schema-locked deep research prompt** optimized for Gemini-3-Flash grounded web research.

Outputs a structured prompt ready to pass to the MCP discover tool (`type="deepsearch"`).

---

## Step 1: Classify the Query

First, determine the **primary discovery target** from the user's NLP query.

| Signal in query | Mode |
|---|---|
| Job titles, roles, seniority, skills, education, past experience, certifications | **CONTACT** |
| Company attributes: location, revenue, headcount, funding, tech stack, industry, tools used | **COMPANY** |
| Both people AND companies explicitly | **CONTACT** (default; embed company filters as context) |

---

## Step 2: Extract Filter Dimensions

Parse ALL filter signals from the query. Map them to the correct dimension bucket:

### Contact Filter Dimensions

| Dimension | Examples |
|---|---|
| **Current Title** | "VP of Sales", "Head of Engineering", "Founder", "C-suite" |
| **Seniority** | "Director and above", "senior", "entry-level" |
| **Current Company** | "at a Series B startup", "at a Fortune 500", "at Google" |
| **Industry** | "SaaS", "fintech", "healthcare", "e-commerce" |
| **Location** | "based in London", "US-based", "APAC region" |
| **Past Experience** | "previously worked at McKinsey", "ex-Googler", "former banker" |
| **Education** | "Stanford MBA", "IIT graduate", "CS degree" |
| **Skills / Certifications** | "AWS certified", "Salesforce admin", "fluent in Python" |
| **Company Size** | "at companies with 50-200 employees" |
| **Funding Stage** | "at Series A-C companies" |
| **Tech Used** | "uses HubSpot", "company uses Snowflake" |
| **Keywords / Bio signals** | "mentions AI in their bio", "open to work" |

### Company Filter Dimensions

| Dimension | Examples |
|---|---|
| **Industry / Vertical** | "SaaS", "logistics", "edtech" |
| **Location** | "headquartered in Germany", "UK-based", "APAC offices" |
| **Revenue Range** | "$1M-$10M ARR", "over $50M revenue" |
| **Headcount** | "50-500 employees", "enterprise" |
| **Funding Stage** | "Series A", "bootstrapped", "post-IPO" |
| **Funding Amount** | "raised over $10M", "seed funded" |
| **Investors** | "backed by a16z", "Y Combinator alumni" |
| **Tech Stack** | "uses Salesforce", "built on AWS", "Shopify stores" |
| **Tech Service Provider** | "uses Stripe for payments", "Zendesk for support" |
| **Keywords / Signals** | "hiring for ML roles", "recently launched", "expanding to US" |
| **Business Model** | "B2B", "marketplace", "PLG motion" |
| **Year Founded** | "founded after 2018", "established companies pre-2010" |

---

## Step 3: Build the Discovery Criteria Block

Translate the extracted filters into a **clear, explicit prose criteria block** for the prompt. Be specific. Do not be vague.

Format criteria as numbered requirements:

```
1. [Filter dimension]: [Specific value or range]
2. [Filter dimension]: [Specific value or range]
...
```

If a filter is implicit (e.g., "senior" implies Director+), make it explicit in the criteria.

---

## Step 4: Select and Fill the Prompt Template

### CONTACT Deep Research Prompt

```
You are a structured professional contact discovery engine using grounded web research.

Your task is to discover real professional contacts matching ALL of the following criteria:

DISCOVERY CRITERIA:
{{criteria_block}}

TARGET ENTITY TYPE:
Professional Contacts

REQUIRED OUTPUT FORMAT:
Return ONLY a valid JSON array. Each object MUST contain EXACTLY these fields:

{
  "name": "Full name of the contact",
  "linkedin_url": "Full LinkedIn profile URL (https://linkedin.com/in/...)",
  "title": "Current job title",
  "company_name": "Current employer company name",
  "company_linkedin": "Company LinkedIn page URL (https://linkedin.com/company/...)",
  "company_url": "Company website URL",
  "location": "City, Country",
  "match_reason": "One sentence explaining why this person matches the criteria"
}

STRICT REQUIREMENTS:
1. Return ONLY the JSON array — no explanations, no markdown, no extra text, no comments
2. DO NOT invent or hallucinate contacts
3. ONLY include contacts with verifiable LinkedIn profiles
4. ONLY include currently active professionals
5. ALL criteria above must be satisfied — do not return partial matches
6. If a field cannot be verified, use null — do not guess

QUALITY REQUIREMENTS:
- Prefer decision-makers and senior roles when seniority is unspecified
- Prefer contacts with complete, active LinkedIn profiles
- Prefer contacts whose current role matches the criteria (not a past role)

DATA VALIDITY:
- linkedin_url: must be a real https://linkedin.com/in/... URL
- company_linkedin: must be a real https://linkedin.com/company/... URL
- company_url: must be a live company website

Return between 10 and 200 results. Maximize result count while maintaining quality.
```

---

### COMPANY Deep Research Prompt

```
You are a structured company discovery engine using grounded web research.

Your task is to discover real companies matching ALL of the following criteria:

DISCOVERY CRITERIA:
{{criteria_block}}

TARGET ENTITY TYPE:
Companies

REQUIRED OUTPUT FORMAT:
Return ONLY a valid JSON array. Each object MUST contain EXACTLY these fields:

{
  "company_name": "Company name",
  "industry": "Primary industry or vertical",
  "company_linkedin": "Company LinkedIn page URL (https://linkedin.com/company/...)",
  "company_url": "Company website URL",
  "location": "HQ City, Country",
  "headcount_range": "Estimated employee count range e.g. 50-200",
  "funding_stage": "e.g. Series B, Bootstrapped, Public",
  "tech_stack": ["tool1", "tool2"],
  "match_reason": "One sentence explaining why this company matches the criteria"
}

STRICT REQUIREMENTS:
1. Return ONLY the JSON array — no explanations, no markdown, no extra text, no comments
2. DO NOT invent or hallucinate companies
3. ONLY include real companies with active web presence
4. ALL criteria above must be satisfied — do not return partial matches
5. If a field cannot be verified, use null — do not guess

DATA VALIDITY:
- company_linkedin: must be a real https://linkedin.com/company/... URL
- company_url: must be a live, accessible company website

Return between 10 and 200 results. Maximize result count while maintaining quality.
```

---

## Step 5: Output

Return the **fully rendered prompt** (with `{{criteria_block}}` replaced) ready to pass to the MCP discover tool.

Also output a **brief summary** in this format before the prompt:

```
MODE: Contact | Company
FILTERS DETECTED: [list of dimensions identified]
CRITERIA COUNT: [N]
```

Then the full rendered prompt.

---

## Examples

### Example 1: Contact Query

**User query:** "Find me CTOs and VPs of Engineering at Series B SaaS companies in the US who previously worked at FAANG companies"

**Extracted filters:**
- Current Title: CTO, VP of Engineering
- Industry: SaaS
- Company Funding Stage: Series B
- Company Location: United States
- Past Experience: FAANG (Facebook/Meta, Amazon, Apple, Netflix, Google)

**Criteria block:**
```
1. Current Title: CTO or VP of Engineering (or equivalent such as Head of Engineering)
2. Industry: B2B SaaS companies
3. Company Funding Stage: Series B
4. Company Location: United States (US-based HQ)
5. Past Experience: Previously employed at one or more FAANG companies (Meta, Amazon, Apple, Netflix, Google, or their subsidiaries)
```

---

### Example 2: Company Query

**User query:** "Companies in Germany using Salesforce and Snowflake with 100-500 employees and Series A or B funding"

**Extracted filters:**
- Location: Germany
- Tech Stack: Salesforce, Snowflake
- Headcount: 100-500
- Funding Stage: Series A or Series B

**Criteria block:**
```
1. Headquarters Location: Germany
2. Technology Stack: Must actively use both Salesforce (CRM) and Snowflake (data warehouse)
3. Employee Headcount: Between 100 and 500 employees
4. Funding Stage: Series A or Series B
```

---

### Example 3: Contact Query with Education

**User query:** "Marketing managers at e-commerce brands who have an MBA and are based in London or New York"

**Criteria block:**
```
1. Current Title: Marketing Manager (or Senior Marketing Manager, Growth Manager, or equivalent)
2. Industry: E-commerce or D2C retail brands
3. Education: Holds an MBA degree
4. Location: Based in London (UK) or New York City (US)
```

---

## Edge Case Handling

| Situation | Handling |
|---|---|
| Ambiguous seniority | Default to "Manager and above" |
| No location specified | Remove location filter; search globally |
| Revenue + headcount both given | Include both as AND conditions |
| Tech stack (niche tool) | Include in criteria; note "may reduce result count" |
| Very broad query (e.g. "find me startup founders") | Add clarifying note and generate with reasonable defaults; suggest user narrows criteria |
| Both contacts AND companies requested | Split into two separate prompts, one per mode |

---

## Response Validation Notes

After receiving results from the MCP discover tool:

1. Parse JSON array
2. Discard records where required fields are `null` or malformed
3. Validate URL formats (linkedin_url, company_linkedin, company_url)
4. Deduplicate by `linkedin_url` (contacts) or `company_url` (companies)
5. Return clean, normalized dataset to user

---

## Gemini-3-Flash Optimization Principles Applied

- Output schema is **explicitly defined with field names and types**
- Strict JSON enforced with no markdown or prose leakage
- Entity type clearly stated at top of prompt
- Hallucination explicitly prohibited
- `match_reason` field forces grounded reasoning per result
- `null` fallback prevents fabricated data for missing fields

