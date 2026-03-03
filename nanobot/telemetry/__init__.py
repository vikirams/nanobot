"""PostHog telemetry — thin singleton wrapper.

All calls are no-ops when the API key is not configured.
The PostHog SDK uses a background thread queue, so no async is needed.
"""
from __future__ import annotations

import posthog as _ph

_enabled = False


def init(api_key: str, host: str = "https://us.i.posthog.com") -> None:
    """Initialise the PostHog client. Safe to call multiple times."""
    global _enabled
    if not api_key:
        return
    _ph.api_key = api_key
    _ph.host = host
    _ph.disabled = False
    _enabled = True


def capture(event: str, properties: dict, account_id: str = "anonymous") -> None:
    """Capture a PostHog event. No-op when telemetry is disabled."""
    if not _enabled:
        return
    try:
        _ph.capture(distinct_id=account_id, event=event, properties=properties)
    except Exception:
        pass  # telemetry must never crash the main flow
