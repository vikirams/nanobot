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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

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
    "founding_year_min", "founding_year_max",
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
    fy_min, fy_max = filters.get("funding_year_min"), filters.get("funding_year_max")
    if fy_min is not None or fy_max is not None:
        if fy_min is not None and fy_max is not None:
            out.append({"label": "Funding Year", "value": f"{fy_min}–{fy_max}"})
        elif fy_max is not None:
            out.append({"label": "Funding Year", "value": f"≤ {fy_max}"})
        else:
            out.append({"label": "Funding Year", "value": f"≥ {fy_min}"})
    if filters.get("funding_stages"):
        out.append({"label": "Funding Stage", "value": ", ".join(str(x) for x in filters["funding_stages"])})
    if filters.get("technologies"):
        out.append({"label": "Technologies", "value": ", ".join(str(x) for x in filters["technologies"])})
    if filters.get("keywords"):
        out.append({"label": "Keywords", "value": ", ".join(str(x) for x in filters["keywords"])})
    return out


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
- founding_year_min, founding_year_max (int): when user says "founded year", "founding year", "established", "started in", "founded in" → extract year range here. This is when the company was CREATED.
- funding_year_min, funding_year_max (int): when user says "funding year", "funded in", "raised in", "funded" → when the company received funding. Do NOT use for "founded"/"founding"/"established".
DB columns for Strategy 1: location, headcount, revenue, funding_stage, founding_year or funding_year (past range), tech_stack, LinkedIn industry. Else → Strategy 2.

mcp_call — tool "mcp_hp-discovery_hp_discovery", params: type, query, limit (always 100). No "page".
- Strategy 1: type "contact"|"company", query = short natural language (e.g. "CTOs in Bangalore", "Companies in Middle East and Singapore, under 250 employees, revenue $300K–$5M").
- Strategy 2: type "deepsearch", query = prompt starting "You are a structured web research agent tasked with discovering [target].\\nResearch thoroughly…\\nTARGET CRITERIA:…\\nFor each company: company_name, domain, linkedin_url, industry, location, headcount_range, description, funding_stage, technologies\\nAim 50-100. Return ONLY valid JSON array."
- Strategy 3: mcp_call has phase_1 (deepsearch company prompt) and phase_2 (type "contact", query "[titles] at these companies: <<domains>>", limit 100).
- CSV: type "contact", query "[titles] at these companies: <<domains from file>>", limit 100.

Return this structure (include all keys):
intent_type, entity_type, flow_type, strategy (1|2|3), filters {titles,locations,industries,industries_exclude,domains,headcount_min,headcount_max,revenue_min,revenue_max,technologies,funding_stages,founding_year_min,founding_year_max,funding_year_min,funding_year_max,keywords}, count_limit, has_interpretive_filters, confirmation_summary, mcp_call.

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
        self._on_progress: Callable[..., Awaitable[None]] | None = None

    def set_context(
        self,
        account_id: str = "",
        *,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        """Inject account_id for telemetry and optional on_progress for streaming status/tokens to UI."""
        self._account_id = account_id
        if on_progress is not None:
            self._on_progress = on_progress

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
            on_token_cb: Callable[[str], Awaitable[None]] | None = None
            if self._on_progress:
                await self._on_progress("🔍 Analyzing your request…")
                first_token = [True]  # mutable so closure can update

                async def _on_token(delta: str) -> None:
                    # One friendly status when LLM starts responding; never stream raw JSON.
                    if self._on_progress and delta and first_token:
                        first_token.clear()
                        await self._on_progress("Segmenting filters & understanding intent…")

                on_token_cb = _on_token

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
                    on_token=on_token_cb,
                ),
                timeout=60.0,
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
