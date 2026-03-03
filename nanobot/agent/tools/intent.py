"""Intent analysis tool for enrichment request classification.

Call this FIRST for any contact/company discovery or enrichment request.
Returns a structured JSON object that the agent uses to:
  1. Select the correct strategy (DB, Deep Research, Hybrid, CSV)
  2. Extract filters without asking the user
  3. Build the Discovery Plan confirmation message
  4. Pre-build the exact MCP call to execute on user confirmation
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot import telemetry as _telemetry

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

# Canonical filter keys and default; used for validation and fallback.
_FILTER_LIST_KEYS = (
    "titles", "locations", "industries", "industries_exclude", "domains",
    "technologies", "funding_stages", "keywords",
)
_FILTER_NUMERIC_KEYS = (
    "headcount_min", "headcount_max", "revenue_min", "revenue_max",
    "funding_year_min", "funding_year_max",
)
_DEFAULT_FILTERS: dict[str, Any] = {
    **{k: [] for k in _FILTER_LIST_KEYS},
    **{k: None for k in _FILTER_NUMERIC_KEYS},
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
    parsed["filters"] = out_filters
    # Coerce strategy to 1|2|3
    s = parsed.get("strategy")
    if s not in (1, 2, 3):
        try:
            parsed["strategy"] = max(1, min(3, int(s))) if s is not None else 1
        except (TypeError, ValueError):
            parsed["strategy"] = 1
    return parsed


_INTENT_SYSTEM = """\
You extract B2B discovery intent and return one JSON object. Output ONLY valid JSON: no markdown, no ```, no prose. Response must start with { and end with }.

entity_type: "company" (companies/startups/firms) | "contact" (people/titles) | "both" (titles at company type).

strategy:
- A) contact only → 1 (DB).
- B) company → 1 only if ALL: (a) filters map to DB columns only, (b) has location/headcount/funding_stage/past year range, (c) no recency (recent/new/2024/2025/this year), (d) no capability (SaaS/B2B/has X/uses AI). Else → 2.
- C) both → 3 (Hybrid).
Strategy 2 triggers: tech verticals (fintech, cybersecurity, healthtech…), business model (SaaS, B2B, marketplace), recency, capability phrases. When in doubt → 2.

filters — extract precisely. confirmation_summary: human-readable only, never "null".
- locations[]: countries, cities, regions (e.g. Middle East, Singapore).
- headcount_min, headcount_max (int): "<250"/"under 250" → max 250 min null; "50–200" → 50,200; ">100" → min 100 max null.
- industries[], industries_exclude[]: LinkedIn-style (Technology, Financial Services, Healthcare…); exclude if "excluding X"/"not X".
- revenue_min, revenue_max (USD int): "300K–$5M" → 300000,5000000; K=1e3, M/Mil=1e6.
DB columns for Strategy 1: location, headcount, revenue, funding_stage, funding_year (past range), tech_stack, LinkedIn industry. Else → Strategy 2.

mcp_call — tool "mcp_hp-discovery_hp_discovery", params: type, query, limit (always 100). No "page".
- Strategy 1: type "contact"|"company", query = short natural language (e.g. "CTOs in Bangalore", "Companies in Middle East and Singapore, under 250 employees, revenue $300K–$5M").
- Strategy 2: type "deepsearch", query = prompt starting "You are a structured web research agent tasked with discovering [target].\\nResearch thoroughly…\\nTARGET CRITERIA:…\\nFor each company: company_name, domain, linkedin_url, industry, location, headcount_range, description, funding_stage, technologies\\nAim 50-100. Return ONLY valid JSON array."
- Strategy 3: mcp_call has phase_1 (deepsearch company prompt) and phase_2 (type "contact", query "[titles] at these companies: <<domains>>", limit 100).
- CSV: type "contact", query "[titles] at these companies: <<domains from file>>", limit 100.

Return this structure (include all keys):
intent_type, entity_type, flow_type, strategy (1|2|3), filters {titles,locations,industries,industries_exclude,domains,headcount_min,headcount_max,revenue_min,revenue_max,technologies,funding_stages,funding_year_min,funding_year_max,keywords}, count_limit, has_interpretive_filters, confirmation_summary, mcp_call.

Example Strategy 1 (company, headcount+revenue+region):
"Find companies, Employee Size <250, Revenue 300K-$5M, Region: Middle East, Singapore"
→ strategy 1, filters: locations ["Middle East","Singapore"], headcount_max 250, revenue_min 300000, revenue_max 5000000; mcp_call.params.query "Companies in Middle East and Singapore with under 250 employees and revenue $300K to $5M USD"; confirmation_summary "Find companies in Middle East and Singapore with under 250 employees and revenue $300K to $5M".

Example Strategy 2:
"Identify cybersecurity companies in US" → strategy 2, keywords ["cybersecurity"], has_interpretive_filters true, mcp_call.params.type "deepsearch", query "You are a structured web research agent…cybersecurity companies…United States…".
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

    def set_context(self, account_id: str = "") -> None:
        """Inject account_id for telemetry distinct_id."""
        self._account_id = account_id

    @property
    def name(self) -> str:
        return "analyze_enrichment_intent"

    @property
    def description(self) -> str:
        return (
            "Call this FIRST for any contact or company discovery/enrichment request. "
            "Extracts entity_type, strategy (1=DB, 2=DeepResearch, 3=Hybrid), "
            "filters (titles, locations, industries, industries_exclude, headcount, revenue, funding), "
            "confirmation_summary, and pre-built mcp_call for execution on confirmation. "
            "Also detects csv_enrichment, dedup, and export intents."
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

    async def execute(self, user_query: str = "", **kwargs: Any) -> str:
        if not user_query.strip():
            return json.dumps({"error": "user_query is required"})

        response = None
        content = ""
        try:
            response = await asyncio.wait_for(
                self._provider.chat(
                    messages=[
                        {"role": "system", "content": _INTENT_SYSTEM},
                        {"role": "user", "content": user_query},
                    ],
                    tools=None,
                    model=self._model,
                    temperature=0.0,
                    max_tokens=2048,
                ),
                timeout=60.0,
            )
            raw_content = (response.content or "").strip()
            logger.debug("[intent] raw_response: {!r}", raw_content[:400])
            content = _extract_json(raw_content)
            parsed = json.loads(content)
            parsed = _normalize_intent(parsed)
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
                "count_limit": None,
                "has_interpretive_filters": False,
                "confirmation_summary": f"Process request: {user_query[:120]}",
                "mcp_call": None,
                "_parse_error": True,
                "_raw_response": raw,
            })

        except Exception as exc:
            logger.exception("Intent analysis failed")
            _telemetry.capture("intent.analysis_raw_response", {
                "user_query": user_query,
                "model": self._model,
                "parse_success": False,
                "error": str(exc),
            }, account_id=self._account_id)
            return json.dumps({"error": f"Intent analysis failed: {exc}"})
