"""Intent analysis tool for enrichment request classification.

Call this FIRST for any contact/company discovery or enrichment request.
Returns a structured JSON object that the agent uses to:
  1. Select the correct strategy (DB, Deep Research, Hybrid, CSV)
  2. Extract filters without asking the user
  3. Build the Discovery Plan confirmation message
  4. Pre-build the exact MCP call to execute on user confirmation

When on_progress is set (via set_context), sends friendly status messages only
(e.g. "Analyzing your request…", "Segmenting filters & understanding intent…")
so the UI shows progress without exposing raw JSON.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot import telemetry as _telemetry

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

# Canonical filter keys and default; used for validation and fallback.
_FILTER_LIST_KEYS = (
    "titles", "locations", "industries", "industries_exclude", "domains",
    "technologies", "funding_stages", "keywords", "segments", "exclusions",
)
_FILTER_NUMERIC_KEYS = (
    "headcount_min", "headcount_max", "revenue_min", "revenue_max",
    "founding_year_min", "founding_year_max",
    "funding_year_min", "funding_year_max",
)
# ISO date strings (YYYY-MM-DD) for sub-annual funding ranges (e.g. "last 3 months").
# Displayed as "Dec 2025 – Mar 2026" instead of "2025 – 2026".
_FILTER_DATE_KEYS = ("funding_date_min", "funding_date_max")
_DEFAULT_FILTERS: dict[str, Any] = {
    **{k: [] for k in _FILTER_LIST_KEYS},
    **{k: None for k in _FILTER_NUMERIC_KEYS},
    **{k: None for k in _FILTER_DATE_KEYS},
}


def _extract_json(text: str) -> str:
    """Extract a single JSON object from LLM output (handles fences and leading prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence:
        return fence.group(1).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1 if (lines and lines[-1].strip() == "```") else len(lines)
        return "\n".join(lines[1:end]).strip()
    if not text.startswith("{"):
        obj = re.search(r"\{[\s\S]*\}", text)
        return obj.group(0) if obj else text
    return text


# Timeout for the intent LLM call. Generous to support thinking/reasoning models.
_INTENT_TIMEOUT = 120.0  # seconds

# Heartbeat interval — emitted when the model is still generating.
_HEARTBEAT_INTERVAL = 15  # seconds

# JSON key patterns that, when they first appear in the streamed output, trigger
# a human-readable status update. Ordered by expected generation sequence.
# Used as fallback when no reasoning tokens are available.
_PROGRESS_STAGES: tuple[tuple[str, str], ...] = (
    ('"intent_type"',          "Classifying your request..."),
    ('"entity_type"',          "Identifying target — contact or company..."),
    ('"filters"',              "Extracting filters & criteria..."),
    ('"strategy"',             "Selecting discovery strategy..."),
    ('"mcp_call"',             "Building your discovery plan..."),
    ('"confirmation_summary"', "Finalizing summary..."),
)

# Min reasoning chars to accumulate before triggering a translation call.
# Lowered so the first translated message appears sooner (visible before intent completes).
_REASONING_CHUNK_MIN = 120
# Force-flush even without a sentence break at this size.
_REASONING_CHUNK_MAX = 350
# Cap total translation calls per intent execution to limit API usage.
_MAX_TRANSLATIONS = 8

# System prompt for the fast translator model.
# Converts raw model reasoning → 1 short user-facing status line.
_TRANSLATOR_SYSTEM = """\
Translate an AI's internal reasoning snippet into one short user-facing status message.
Rules:
- Maximum 10 words
- Present tense, active voice
- Be specific — mention the actual topic (locations, dates, funding stage, etc.)
- No meta-commentary like "The AI is thinking about..."
- No quotes, no trailing punctuation

Examples:
  Reasoning: "The user says last 3 months, so funding_year_min should map to..."
  Status: Computing date range for last 3 months

  Reasoning: "They want Series A and above so funding_stages should include Series A..."
  Status: Extracting Series A+ funding stage filters

  Reasoning: "Locations mentioned are India and US, so I'll set locations to..."
  Status: Setting geographic filters for India and US

  Reasoning: "This is clearly a company search since user mentions startups and firms..."
  Status: Identifying as company discovery request\
"""

_MAX_CLARIFYING_QUESTIONS = 5


def _normalize_clarifying_questions(raw: Any) -> list[dict[str, Any]]:
    """Validate and trim clarifying_questions to a list of {id, question, options, recommended_index?}."""
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if i >= _MAX_CLARIFYING_QUESTIONS:
            break
        if not isinstance(item, dict):
            continue
        q = str(item.get("question") or "").strip()
        opts = item.get("options")
        if not q or not isinstance(opts, list) or len(opts) < 2:
            continue
        options = [str(o).strip() for o in opts if str(o).strip()]
        if len(options) < 2:
            continue
        rec = item.get("recommended_index")
        if rec is not None:
            try:
                rec = max(0, min(len(options) - 1, int(rec)))
            except (TypeError, ValueError):
                rec = None
        out.append({
            "id": str(item.get("id") or f"q{i}").strip() or f"q{i}",
            "question": q,
            "options": options,
            "recommended_index": rec,
        })
    return out


def _normalize_intent(parsed: dict[str, Any]) -> dict[str, Any]:
    """Ensure filters and top-level fields have correct shape and types."""
    filters = parsed.get("filters")
    if not isinstance(filters, dict):
        parsed["filters"] = dict(_DEFAULT_FILTERS)
        return parsed
    out_filters: dict[str, Any] = {}
    for k in _FILTER_LIST_KEYS:
        val = filters.get(k)
        out_filters[k] = list(val) if isinstance(val, list) else []
    for k in _FILTER_NUMERIC_KEYS:
        val = filters.get(k)
        if val is None:
            out_filters[k] = None
        elif isinstance(val, int):
            out_filters[k] = val
        else:
            try:
                out_filters[k] = int(val) if val != "" else None
            except (TypeError, ValueError):
                out_filters[k] = None
    # Guard: models sometimes emit Unix timestamps instead of 4-digit year integers.
    # Any year value > 9999 is treated as a Unix timestamp and converted to its year.
    _YEAR_FIELDS = ("founding_year_min", "founding_year_max", "funding_year_min", "funding_year_max")
    for k in _YEAR_FIELDS:
        val = out_filters.get(k)
        if val is not None and isinstance(val, int) and val > 9999:
            try:
                out_filters[k] = datetime.fromtimestamp(val, tz=timezone.utc).year
            except (OSError, OverflowError, ValueError):
                out_filters[k] = None

    # Pass through ISO date strings for sub-annual precision (e.g. "2025-12-07").
    for k in _FILTER_DATE_KEYS:
        val = filters.get(k)
        out_filters[k] = val if isinstance(val, str) and val else None

    parsed["filters"] = out_filters
    # Coerce strategy to 1|2|3
    s = parsed.get("strategy")
    if s not in (1, 2, 3):
        try:
            parsed["strategy"] = max(1, min(3, int(s))) if s is not None else 1
        except (TypeError, ValueError):
            parsed["strategy"] = 1
    # Optional clarifying questions (AskUserQuestion-style)
    parsed["clarifying_questions"] = _normalize_clarifying_questions(parsed.get("clarifying_questions"))
    return parsed


def _format_revenue(val: int) -> str:
    """Format revenue int as human-readable (e.g. 300000 -> $300K, 5000000 -> $5M)."""
    if val >= 1_000_000:
        return f"${val // 1_000_000}M"
    if val >= 1_000:
        return f"${val // 1_000}K"
    return f"${val}"


def _build_filters_display(filters: dict[str, Any]) -> list[dict[str, str]]:
    """Build list of {label, value} for filters that have values. Used for Discovery Plan — only these lines should be shown."""
    out: list[dict[str, str]] = []
    # List filters
    if filters.get("titles"):
        out.append({"label": "Titles", "value": ", ".join(str(x) for x in filters["titles"])})
    if filters.get("locations"):
        out.append({"label": "Location", "value": "; ".join(str(x) for x in filters["locations"])})
    if filters.get("industries"):
        out.append({"label": "Industry", "value": "; ".join(str(x) for x in filters["industries"])})
    if filters.get("industries_exclude"):
        out.append({"label": "Industry exclude", "value": "; ".join(str(x) for x in filters["industries_exclude"])})
    if filters.get("domains"):
        doms = filters["domains"]
        out.append({"label": "Domains", "value": f"{len(doms)} domain(s)" if len(doms) > 3 else "; ".join(str(x) for x in doms[:10])})
    # Numeric
    h_min, h_max = filters.get("headcount_min"), filters.get("headcount_max")
    if h_min is not None or h_max is not None:
        if h_min is not None and h_max is not None:
            out.append({"label": "Employee size", "value": f"{h_min}–{h_max}"})
        elif h_max is not None:
            out.append({"label": "Employee size", "value": f"under {h_max}"})
        else:
            out.append({"label": "Employee size", "value": f"over {h_min}"})
    r_min, r_max = filters.get("revenue_min"), filters.get("revenue_max")
    if r_min is not None or r_max is not None:
        if r_min is not None and r_max is not None:
            out.append({"label": "Revenue (USD)", "value": f"{_format_revenue(r_min)}–{_format_revenue(r_max)}"})
        elif r_max is not None:
            out.append({"label": "Revenue (USD)", "value": f"≤ {_format_revenue(r_max)}"})
        else:
            out.append({"label": "Revenue (USD)", "value": f"≥ {_format_revenue(r_min)}"})
    # Founding year = when company was founded/established (distinct from funding year)
    found_min, found_max = filters.get("founding_year_min"), filters.get("founding_year_max")
    if found_min is not None or found_max is not None:
        if found_min is not None and found_max is not None:
            out.append({"label": "Founding Year", "value": f"{found_min}–{found_max}"})
        elif found_max is not None:
            out.append({"label": "Founding Year", "value": f"≤ {found_max}"})
        else:
            out.append({"label": "Founding Year", "value": f"≥ {found_min}"})
    fd_min, fd_max = filters.get("funding_date_min"), filters.get("funding_date_max")
    fy_min, fy_max = filters.get("funding_year_min"), filters.get("funding_year_max")

    def _fmt_fund_date(d: str) -> str | None:
        try:
            return datetime.fromisoformat(d).strftime("%b %Y")
        except (ValueError, TypeError):
            return None

    d_min_str = _fmt_fund_date(fd_min) if fd_min else None
    d_max_str = _fmt_fund_date(fd_max) if fd_max else None

    if d_min_str or d_max_str:
        # Prefer precise month-year display when date strings are available.
        if d_min_str and d_max_str:
            label = d_min_str if d_min_str == d_max_str else f"{d_min_str} – {d_max_str}"
            out.append({"label": "Funded Between", "value": label})
        elif d_min_str:
            out.append({"label": "Funded Since", "value": d_min_str})
        else:
            out.append({"label": "Funded Before", "value": d_max_str})
    elif fy_min is not None or fy_max is not None:
        if fy_min is not None and fy_max is not None:
            label = f"{fy_min}" if fy_min == fy_max else f"{fy_min} – {fy_max}"
            out.append({"label": "Funded Between", "value": label})
        elif fy_max is not None:
            out.append({"label": "Funded Before", "value": str(fy_max)})
        else:
            out.append({"label": "Funded Since", "value": str(fy_min)})
    if filters.get("funding_stages"):
        out.append({"label": "Funding Stage", "value": ", ".join(str(x) for x in filters["funding_stages"])})
    if filters.get("technologies"):
        out.append({"label": "Technologies", "value": ", ".join(str(x) for x in filters["technologies"])})
    if filters.get("keywords"):
        out.append({"label": "Keywords", "value": ", ".join(str(x) for x in filters["keywords"])})
    if filters.get("segments"):
        out.append({"label": "Priority Segments", "value": "; ".join(str(x) for x in filters["segments"])})
    if filters.get("exclusions"):
        out.append({"label": "Exclusions", "value": "; ".join(str(x) for x in filters["exclusions"])})
    return out


_INTENT_SYSTEM = """\
You extract B2B discovery intent and return one JSON object. Output ONLY valid JSON: no markdown, no ```, no prose. Response must start with { and end with }.

COMPLEX QUERIES (multi-paragraph, campaign context):
When the user provides long-form text (campaign descriptions, product URLs, ICP text, priority segments, exclusions): treat campaign context and product URLs as background; extract as signal: (1) core discovery goal (who/what), (2) ideal target criteria, (3) segments[] — named priority verticals or categories the user listed (e.g. "Telematics & Fleet", "Smart Metering", "Industrial IoT"), (4) exclusions[] — what to exclude (e.g. "MNOs", "MVNOs", "pure hardware manufacturers"). Put each segment and exclusion as a distinct string in the arrays.

entity_type: "company" (companies/startups/firms) | "contact" (people/titles) | "both" (titles at company type).

DATA FRESHNESS — The internal LinkedIn database is approximately 1 year old. Any query that requires data newer than 12 months from today MUST use Strategy 2. This includes: "recently funded", "funded in [current year or last year]", "funded in the last N months/weeks", "latest revenue", "newest companies", "founded in [current or last year]", "recently raised", "new companies", any relative time phrase within 12 months of today.

LINKEDIN DATA LIMITATION — LinkedIn's industry taxonomy is coarse (Technology, Financial Services, Healthcare, Retail, etc.). It CANNOT identify sub-verticals, business models, or capabilities. The following descriptors CANNOT be resolved from the database and ALWAYS require Strategy 2:
  • Sub-vertical / domain labels: cybersecurity, fintech, healthtech, edtech, proptech, insurtech, legaltech, cleantech, AI/ML companies, blockchain, IoT, cloud computing, dev tools, data analytics, regtech, agritech, spacetech, mobility, logistics-tech, HR tech, martech, adtech — any "X-tech" or "X-as-a-service" label.
  • Business model descriptors: SaaS, marketplace, B2B, B2C, D2C, product-based, service-based, subscription, platform, API-first, open-source, developer tools.
  • Capability / offering phrases: "uses AI", "provides X service", "has Y product", "does Z", "offers A feature", "mobile app development companies", "product engineering firms", "deep tech".
  • Qualitative or interpretive company descriptions: any phrase that describes WHAT a company does or HOW it operates beyond a LinkedIn industry category.
  • Campaign, ICP, or account-list requests with any of the above.
  The database CAN resolve: contact titles, location (country/city/region), headcount range, revenue range, LinkedIn industry category (coarse), funding_stage, founding_year (historical range more than 12 months ago), named technology stack (Salesforce, AWS, HubSpot, etc.).

strategy:
- A) entity_type "contact" — user wants people, not companies → Strategy 1 (direct DB: titles + structural contact filters).
- B) entity_type "company":
    → Strategy 1 ONLY when ALL of these are true: all filters are DB-resolvable (location, headcount, revenue, LinkedIn industry, funding_stage, founding_year range older than 12 months), no time-sensitive filter within 12 months, no sub-vertical/business-model/capability descriptor.
    → Strategy 2 for EVERY other company query: any vertical label, business model descriptor, capability phrase, time-sensitive filter within 12 months, qualitative description, campaign/ICP request, or when in doubt.
- C) entity_type "both" — user wants contacts at a specific type of company → Strategy 3 (Hybrid: deep research finds companies via web, then DB finds contacts at those companies by domain).
When in doubt → Strategy 2.

filters — extract precisely. confirmation_summary: human-readable only, never "null".
- locations[]: countries, cities, regions (e.g. Middle East, Singapore).
- headcount_min, headcount_max (int): "<250"/"under 250" → max 250 min null; "50–200" → 50,200; ">100" → min 100 max null.
- industries[], industries_exclude[]: LinkedIn-style (Technology, Financial Services, Healthcare…); exclude if "excluding X"/"not X".
- revenue_min, revenue_max (USD int): "300K–$5M" → 300000,5000000; K=1e3, M/Mil=1e6.
- founding_year_min, founding_year_max (int): when user says "founded year", "founding year", "established", "started in", "founded in" → extract year range here. This is when the company was CREATED. MUST be a 4-digit year integer (e.g., 2024). NEVER a Unix timestamp or ISO date string.
- funding_year_min, funding_year_max (int): when user says "funding year", "funded in", "raised in", "funded in the last N months" → extract the year range when funding occurred. Do NOT use for "founded"/"founding"/"established". MUST be a 4-digit year integer (e.g., 2025). NEVER a Unix timestamp or ISO date string. For sub-year ranges like "last 3 months", use the year of the start date for _min and year of the end date for _max (e.g., "last 3 months" from Mar 2026 → min=2025, max=2026).
- funding_date_min, funding_date_max (ISO date string "YYYY-MM-DD" or null): ALSO set these whenever the time range is sub-annual (e.g. "last 3 months", "last 6 months", "Q3 2025", "recently", "since October"). Use the concrete start and end dates from TODAY'S DATE context. Example: "last 3 months" from 2026-03-07 → funding_date_min "2025-12-07", funding_date_max "2026-03-07". Leave null for full-year ranges like "funded in 2024" or "funded 2023–2025".
- funding_stages[]: ONLY include stages explicitly mentioned or clearly implied. "Series A and above" → ["Series A", "Series B", "Series C", "Series D", "Series E", "Late Stage", "Growth Equity"]. "Early stage" → ["Seed", "Pre-Seed", "Series A"]. "Series B+" → ["Series B", "Series C", "Series D", "Series E", "Late Stage"]. Do NOT list every possible stage unless user says "all stages" or equivalent.
- technologies[]: ONLY explicit tech stack tools the company uses in production (e.g., Salesforce, AWS, HubSpot, Kubernetes, Python). Use ONLY when the user specifically asks for companies that use a named tool or platform. NEVER use for company type, domain, or descriptors — "AI companies", "SaaS companies", "fintech" → use keywords[] instead.
- keywords[]: Broad company descriptors that don't fit other filters — type, domain, industry vertical, or capability (e.g., "AI", "machine learning", "fintech", "SaaS", "cybersecurity", "B2B", "marketplace"). Use whenever the user describes WHAT the company is or does, not a specific tech tool they use. "AI companies" → keywords ["AI", "artificial intelligence"]. "Fintech startups" → keywords ["fintech"].
- segments[]: priority segments/verticals the user listed (e.g. Telematics & Fleet, Smart Metering, Industrial IoT, IoT Platforms).
- exclusions[]: explicit exclusions (e.g. MNOs, MVNOs, semiconductor vendors, pure hardware manufacturers).
Strategy 1 DB columns: location, headcount, revenue, funding_stage, founding_year (historical, >12 months ago), funding_year (historical, >12 months ago), named tech_stack, LinkedIn industry (coarse). Any filter outside this list → Strategy 2.

mcp_call — describes what the MCP server will execute. NanoBot reads this, passes search_type + formed query to the /discovery MCP prompt which routes to the correct API endpoint.
- Strategy 1 (direct): { params: { type: "contact"|"company", query: <short natural language>, limit: 100 } }
  Routes to: internal DB via hp-discovery-tool.
  query examples: "CTOs in Bangalore", "Companies in Middle East and Singapore, under 250 employees, revenue $300K–$5M".
- Strategy 2 (deep_research): { params: { type: "deepsearch", query: <full structured prompt>, limit: 100 } }
  Routes to: hp-deep-research (web/external research).
  query MUST be a full structured prompt starting "You are a structured company discovery engine..." and MUST include:
  (1) DISCOVERY CRITERIA block: numbered lines for all filters, segments; if exclusions present, add EXCLUSIONS block with active check ("Before adding ANY company, verify it does NOT fall into an excluded category").
  (2) REQUIRED OUTPUT FIELDS (multi-line list, NOT inline JSON): mandatory for every result — name (company name), website (full URL https://…), source_url (web source URL where match was verified), reasoning (why selected; cite specific signals/evidence). Add domain-relevant fields inferred from the query.
  (3) SEARCH STRATEGY: for campaigns with segments, search each segment independently. For simple queries, search multiple angles/keywords.
  (4) OTHER REQUIREMENTS: no hallucination, no duplicates, order by relevance (strongest matches first), verify exclusions before including. End with strict JSON-only output and target volume (50-100 or 100-200).
  For campaign/ICP-style requests add: segment, lead_score (High/Medium/Low), product_use_case, target_rationale; optionally product_leader, engineering_leader, operations_leader.
  Do NOT use a fixed schema — adapt output fields to the query domain.
- Strategy 3 (hybrid): { phase_1: { params: { type: "deepsearch", query: <full structured prompt for companies>, limit: 100 } }, phase_2: { params: { type: "contact", query: "[titles] at these companies: <<domains>>", limit: 100 } } }
  phase_1 routes to: hp-deep-research. phase_2 routes to: hp-discovery-tool (DB) with <<domains>> substituted by discovered company websites.
  phase_2 query must include specific titles extracted from the user request.

CLARIFYING QUESTIONS (optional, like AskUserQuestion): When the request is ambiguous — e.g. multiple valid interpretations (region? company vs contact?), critical filter missing with several plausible values, or scope unclear — you MAY add "clarifying_questions": [{"id": "short_id", "question": "Human-readable question?", "options": ["Option A", "Option B", "Option C"], "recommended_index": 0}]. Use recommended_index (0-based) for the option you think best. Maximum 4 questions. If the user intent is clear, omit clarifying_questions or use []. The agent will show these to the user and re-run intent with their answers before presenting the plan.

Return this structure (include all keys):
intent_type, entity_type, flow_type, strategy (1|2|3), strategy_reasoning (string: 1-2 sentences explaining WHY you chose this strategy — cite the specific rule that triggered it, e.g. "Strategy 2: query uses tech-vertical label 'AI' and time-relative 'last 3 months'"), filters {titles,locations,industries,industries_exclude,domains,headcount_min,headcount_max,revenue_min,revenue_max,technologies,funding_stages,founding_year_min,founding_year_max,funding_year_min,funding_year_max,funding_date_min,funding_date_max,keywords,segments,exclusions}, count_limit, has_interpretive_filters, confirmation_summary, mcp_call[, clarifying_questions].

Example Strategy 1 (company, headcount+revenue+region):
"Find companies, Employee Size <250, Revenue 300K-$5M, Region: Middle East, Singapore"
→ strategy 1, filters: locations ["Middle East","Singapore"], headcount_max 250, revenue_min 300000, revenue_max 5000000; mcp_call.params.query "Companies in Middle East and Singapore with under 250 employees and revenue $300K to $5M USD"; confirmation_summary "Find companies in Middle East and Singapore with under 250 employees and revenue $300K to $5M".

Example Strategy 2 (simple):
"Identify cybersecurity companies in US" → strategy 2, keywords ["cybersecurity"], has_interpretive_filters true, mcp_call.params.type "deepsearch", query includes REQUIRED OUTPUT FIELDS (multi-line): name, website, linkedin_url, specialty, location, size, source_url, reasoning. SEARCH STRATEGY: multiple angles. OTHER REQUIREMENTS: no hallucination, no duplicates, order by relevance.

Example Strategy 2 (complex / campaign):
"Build a target account list for IoT device intelligence campaign. Target: companies deploying cellular IoT fleets (LTE-M, NB-IoT). Segments: Telematics & Fleet, Smart Metering, Industrial IoT, IoT Platforms. Exclude: MNOs, MVNOs, pure hardware manufacturers."
→ strategy 2, entity_type "company", keywords ["IoT","cellular IoT","device intelligence","LTE-M","NB-IoT"], segments ["Telematics & Fleet Management","Smart Metering & Utilities","Industrial IoT","IoT Platforms / Device Management SaaS"], exclusions ["MNOs","MVNOs","pure hardware manufacturers"], mcp_call.params.type "deepsearch", query starts with "You are a structured company discovery engine for campaign targeting." Includes: DISCOVERY CRITERIA (numbered) with segment descriptions; EXCLUSIONS block with active check; REQUIRED OUTPUT FIELDS (multi-line): name, website, linkedin_url, segment, fleet_scale, device_map_use_case, lead_score, product_leader, engineering_leader, operations_leader, source_url, target_rationale, reasoning; SEARCH STRATEGY: search each segment independently; OTHER REQUIREMENTS: no hallucination, exclusion check, no duplicates, order by segment then relevance. confirmation_summary "Build target account list for IoT device intelligence: companies deploying cellular IoT fleets in Telematics, Smart Metering, Industrial IoT, IoT Platforms; exclude MNOs, MVNOs, pure hardware manufacturers."

Example Strategy 2 (recency + funding stage + domain + geography):
"AI Companies funded in the last 3 months, Series A and above, HQ: India and US"
→ strategy 2 (recency + domain descriptor triggers deep research), entity_type "company",
  keywords ["AI", "artificial intelligence"] (NOT technologies — "AI" describes company domain, not a tech tool),
  technologies [] (empty — user did not name a specific tool or platform),
  locations ["India", "US"],
  funding_stages ["Series A", "Series B", "Series C", "Series D", "Series E", "Late Stage", "Growth Equity"] (Series A and above — do NOT enumerate every possible stage),
  funding_year_min and funding_year_max: compute "last 3 months" using TODAY'S DATE from the date context at the end of this prompt. Extract the year of the start date for _min and year of the end date for _max. NEVER output Unix timestamps — only 4-digit year integers,
  funding_date_min and funding_date_max: also set the exact ISO dates for sub-annual precision (e.g. "2025-12-07" and "2026-03-07" for last 3 months from Mar 2026),
  confirmation_summary "Find AI companies funded in the last 3 months (Series A+) headquartered in India or the US".
"""


class IntentAnalysisTool(Tool):
    """Structured intent extractor — call FIRST for any enrichment request.

    Returns classification + pre-built mcp_call ready for immediate execution
    after user confirmation. Eliminates the need for a separate prompt-engineering
    step for Strategy 2 deep research queries.
    """

    def __init__(self, provider: "LLMProvider", model: str) -> None:
        self._provider = provider
        self._model = model
        self._account_id = ""
        self._on_progress: Callable[..., Awaitable[None]] | None = None
        self._translator_model: str | None = None

    def set_context(
        self,
        account_id: str = "",
        *,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        thinking_translator_model: str | None = None,
    ) -> None:
        """Inject account_id, progress callback, and optional thinking translator config."""
        self._account_id = account_id
        if on_progress is not None:
            self._on_progress = on_progress
        if thinking_translator_model is not None:
            self._translator_model = thinking_translator_model

    async def _translate_reasoning(self, chunk: str) -> None:
        """Fire-and-forget: send a reasoning chunk to the fast translator model.

        The translator converts raw chain-of-thought text into a single short
        user-facing status line and forwards it to on_progress.
        Silently no-ops on any error — translation is best-effort.
        """
        if not self._translator_model or not self._on_progress:
            return
        logger.debug("[intent] translating reasoning ({} chars): {!r}", len(chunk), chunk[:80])
        try:
            resp = await asyncio.wait_for(
                self._provider.chat(
                    messages=[
                        {"role": "system", "content": _TRANSLATOR_SYSTEM},
                        {"role": "user", "content": chunk[-400:]},
                    ],
                    model=self._translator_model,
                    max_tokens=25,
                    temperature=0.0,
                ),
                timeout=5.0,
            )
            status = (resp.content or "").strip()
            logger.debug("[intent] translation result: {!r}", status)
            if status and self._on_progress:
                await self._on_progress(status)
        except Exception as exc:
            logger.debug("[intent] translation failed: {}", exc)

    @property
    def name(self) -> str:
        return "analyze_enrichment_intent"

    @property
    def description(self) -> str:
        return (
            "Call this FIRST for any contact or company discovery/enrichment request. "
            "Extracts entity_type, strategy (1=DB, 2=DeepResearch, 3=Hybrid), "
            "filters (titles, locations, industries, industries_exclude, headcount, revenue, funding), "
            "filters_display (only filters with values — use this for the Discovery Plan; do not show omitted lines), "
            "confirmation_summary, and pre-built mcp_call for execution on confirmation. "
            "Also detects csv_enrichment, dedup, and export intents. When the request is ambiguous, may return clarifying_questions (array of {id, question, options, recommended_index}); if present, the agent must show these to the user and re-run intent with their answers before presenting the Discovery Plan."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_query": {
                    "type": "string",
                    "description": "The user's raw enrichment or discovery request (verbatim)",
                },
            },
            "required": ["user_query"],
        }

    @staticmethod
    def _build_system_prompt() -> list[dict[str, str]]:
        """Build the intent system prompt as cache-friendly content blocks.

        Block 1 (static): The extraction rules and examples (~2K tokens).
        Block 2 (dynamic): Today's date — changes daily but doesn't invalidate
        the cached prefix for Block 1.
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        three_months_ago = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        six_months_ago = (now - timedelta(days=180)).strftime("%Y-%m-%d")
        date_block = (
            f"TODAY'S DATE: {today}\n"
            f"Use this to interpret all relative time expressions. Examples:\n"
            f'- "last 3 months" = {three_months_ago} to {today}\n'
            f'- "recently funded" = {six_months_ago} to {today}\n'
            f'- "this year" = {now.year}-01-01 to {today}\n'
            f"Convert all relative time references to concrete funding_year_min / funding_year_max or exact date ranges."
        )
        return [
            {"type": "text", "text": _INTENT_SYSTEM},
            {"type": "text", "text": date_block},
        ]

    async def execute(self, user_query: str = "", **kwargs: Any) -> str:
        if not user_query.strip():
            return json.dumps({"error": "user_query is required"})

        response = None
        content = ""
        system_prompt = self._build_system_prompt()
        heartbeat_task: asyncio.Task | None = None
        try:
            on_token_cb: Callable[[str], Awaitable[None]] | None = None
            on_reasoning_cb: Callable[[str], Awaitable[None]] | None = None
            if self._on_progress:
                if self._translator_model:
                    logger.info(
                        "[intent] thinking translator enabled (model=%s); progress will show translated reasoning",
                        self._translator_model,
                    )
                else:
                    logger.info(
                        "[intent] no thinking_translator_model in config; set agents.defaults.thinking_translator_model to see in-progress reasoning"
                    )
                await self._on_progress("🔍 Analyzing your request…")

                acc: list[str] = [""]
                sent_stages: set[str] = set()

                # --- Reasoning buffer + translator ---
                # Handles thinking content from both sources:
                #   1. delta.reasoning_content  (native provider support, e.g. direct DeepSeek)
                #   2. <think>…</think> tags in the content stream (OpenAI-compat gateways)
                # Batches into ~300-char chunks and fires async translation calls.
                # _flush_fn[0] is called when </think> closes to force-flush the buffer.
                _flush_fn: list[Any] = [None]

                if self._translator_model:
                    reasoning_buf: list[str] = [""]
                    last_translated: list[int] = [0]
                    translation_count: list[int] = [0]
                    first_chunk_shown: list[bool] = [False]

                    async def _handle_reasoning(text: str) -> None:
                        # Show "Thinking…" as soon as any reasoning arrives so the user sees activity.
                        if not first_chunk_shown[0] and text.strip():
                            first_chunk_shown[0] = True
                            if self._on_progress:
                                await self._on_progress("Thinking…")
                        reasoning_buf[0] += text
                        if translation_count[0] >= _MAX_TRANSLATIONS:
                            return
                        total = len(reasoning_buf[0])
                        since_last = total - last_translated[0]
                        if since_last < _REASONING_CHUNK_MIN:
                            return
                        recent = reasoning_buf[0][last_translated[0]:]
                        break_pos = max(
                            recent.rfind("."), recent.rfind("!"),
                            recent.rfind("?"), recent.rfind("\n"),
                        )
                        if break_pos < 50 and since_last < _REASONING_CHUNK_MAX:
                            return
                        chunk = recent[:break_pos + 1] if break_pos >= 50 else recent
                        last_translated[0] = total
                        translation_count[0] += 1
                        asyncio.create_task(self._translate_reasoning(chunk))

                    async def _flush_reasoning() -> None:
                        """Force-flush remaining reasoning buffer (called on </think> close)."""
                        if translation_count[0] >= _MAX_TRANSLATIONS:
                            return
                        leftover = reasoning_buf[0][last_translated[0]:]
                        if leftover.strip():
                            last_translated[0] = len(reasoning_buf[0])
                            translation_count[0] += 1
                            asyncio.create_task(self._translate_reasoning(leftover))

                    _flush_fn[0] = _flush_reasoning
                    on_reasoning_cb = _handle_reasoning

                # --- Content token handler ---
                # Parses the raw content stream, handling two cases:
                #   A) <think>…</think> spans: routes thinking text to _handle_reasoning.
                #      Tags can split across multiple delta chunks — tracked with _think_open.
                #      Buffer is force-flushed when </think> closes.
                #   B) Regular JSON content: checks for key landmarks → stage status messages.
                _think_open: list[bool] = [False]

                async def _on_token(delta: str) -> None:
                    remaining = delta
                    while remaining:
                        if _think_open[0]:
                            # Inside <think> block — scan for closing tag.
                            end = remaining.find("</think>")
                            if end == -1:
                                # Entire chunk is thinking content.
                                if on_reasoning_cb:
                                    await on_reasoning_cb(remaining)
                                remaining = ""
                            else:
                                # Closing tag found — flush thinking portion then continue.
                                think_part = remaining[:end]
                                if think_part and on_reasoning_cb:
                                    await on_reasoning_cb(think_part)
                                _think_open[0] = False
                                # Force-flush buffer so we get at least one translation
                                # even if reasoning was too short to auto-trigger.
                                if _flush_fn[0]:
                                    await _flush_fn[0]()
                                remaining = remaining[end + 8:]  # skip </think>
                        else:
                            # Outside <think> block — scan for opening tag.
                            start = remaining.find("<think>")
                            if start == -1:
                                # Pure JSON content — run stage detection.
                                acc[0] += remaining
                                for key, msg in _PROGRESS_STAGES:
                                    if key not in sent_stages and key in acc[0]:
                                        sent_stages.add(key)
                                        if self._on_progress:
                                            await self._on_progress(msg)
                                remaining = ""
                            else:
                                # JSON content before the tag, then enter thinking mode.
                                if start > 0:
                                    content_part = remaining[:start]
                                    acc[0] += content_part
                                    for key, msg in _PROGRESS_STAGES:
                                        if key not in sent_stages and key in acc[0]:
                                            sent_stages.add(key)
                                            if self._on_progress:
                                                await self._on_progress(msg)
                                _think_open[0] = True
                                remaining = remaining[start + 7:]  # skip <think>

                on_token_cb = _on_token

                # Background heartbeat — elapsed time so user knows we're alive.
                async def _heartbeat() -> None:
                    elapsed = 0
                    while True:
                        await asyncio.sleep(_HEARTBEAT_INTERVAL)
                        elapsed += _HEARTBEAT_INTERVAL
                        if self._on_progress:
                            await self._on_progress(f"Still working… ({elapsed}s elapsed)")

                heartbeat_task = asyncio.create_task(_heartbeat())

            # Enable thinking output when a translator model is configured so
            # the reasoning tokens are returned by the API (Gemini, Anthropic, etc.).
            # Models that don't support the parameter will ignore it via drop_params.
            thinking_cfg = (
                {"type": "enabled", "budget_tokens": 8192}
                if self._translator_model else None
            )

            response = await asyncio.wait_for(
                self._provider.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_query},
                    ],
                    tools=None,
                    model=self._model,
                    temperature=0.0,
                    max_tokens=4096,
                    thinking=thinking_cfg,
                    on_token=on_token_cb,
                    on_reasoning_token=on_reasoning_cb,
                ),
                timeout=_INTENT_TIMEOUT,
            )
            raw_content = (response.content or "").strip()
            logger.debug("[intent] raw_response: {!r}", raw_content[:400])
            content = _extract_json(raw_content)
            parsed = json.loads(content)
            parsed = _normalize_intent(parsed)
            parsed["filters_display"] = _build_filters_display(parsed["filters"])
            _telemetry.capture("intent.analysis_raw_response", {
                "user_query": user_query,
                "model": self._model,
                "parse_success": True,
            }, account_id=self._account_id)
            _telemetry.capture("intent.analysis_success", {
                "user_query": user_query,
                "model": self._model,
                "intent_type": parsed.get("intent_type"),
                "entity_type": parsed.get("entity_type"),
                "flow_type": parsed.get("flow_type"),
                "strategy": parsed.get("strategy"),
                "has_mcp_call": parsed.get("mcp_call") is not None,
            }, account_id=self._account_id)
            return json.dumps(parsed, ensure_ascii=False, indent=2)

        except json.JSONDecodeError as exc:
            _telemetry.capture("intent.analysis_raw_response", {
                "user_query": user_query,
                "model": self._model,
                "parse_success": False,
                "error": str(exc),
            }, account_id=self._account_id)
            raw = (response.content or "")[:300] if response else ""
            q = user_query.lower()
            entity_type = (
                "company"
                if any(w in q for w in ("compan", "startup", "firm", "organis", "organiz", "business"))
                else "contact"
            )
            return json.dumps({
                "intent_type": "general",
                "entity_type": entity_type,
                "flow_type": "db_first",
                "strategy": 1,
                "filters": dict(_DEFAULT_FILTERS),
                "filters_display": [],
                "count_limit": None,
                "has_interpretive_filters": False,
                "confirmation_summary": f"Process request: {user_query[:120]}",
                "mcp_call": None,
                "clarifying_questions": [],
                "_parse_error": True,
                "_raw_response": raw,
            })

        except TimeoutError:
            msg = f"Intent analysis timed out after {_INTENT_TIMEOUT:.0f}s — consider using a faster model for intentModel"
            logger.error(msg)
            _telemetry.capture("intent.analysis_raw_response", {
                "user_query": user_query,
                "model": self._model,
                "parse_success": False,
                "error": "timeout",
            }, account_id=self._account_id)
            return json.dumps({"error": msg})

        except Exception as exc:
            logger.exception("Intent analysis failed")
            _telemetry.capture("intent.analysis_raw_response", {
                "user_query": user_query,
                "model": self._model,
                "parse_success": False,
                "error": str(exc),
            }, account_id=self._account_id)
            return json.dumps({"error": f"Intent analysis failed: {exc}"})

        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
