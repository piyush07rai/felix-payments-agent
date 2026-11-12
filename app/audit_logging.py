"""Centralized System & Request Lifecycle Audit Logging for Felix.

Tracks the complete end-to-end journey of every request:
1. INGRESS: Slack/Teams/Web incoming event
2. ZERO_PII: Sensitive data tokenization/scrubbing
3. LLM_DISPATCH: Primary model selection and prompt dispatch (Gemini / OpenAI)
4. LLM_BACK_AND_FORTH: Function/tool calling iterations
5. TOOL_EXECUTION: SQL queries, ISO lookups, baselines with latency
6. LLM_RESPONSE: Synthesized brief and tokens
7. FAILOVER: Automatic failover from Primary to Secondary or local fallback
8. EGRESS: Response delivery back to Slack/Teams/Web
9. ERROR: Any operational or API errors

All events are formatted cleanly to stdout for external log drains (e.g. Better Stack, Logtail,
Papertrail, Datadog) AND stored in an in-memory buffer for real-time dashboard inspection.
"""

from collections import deque
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("app.lifecycle")

# Circular in-memory buffer for dashboard inspection
MAX_LOG_ENTRIES = 500
AUDIT_EVENT_LOGS: deque = deque(maxlen=MAX_LOG_ENTRIES)


def log_lifecycle_event(
    category: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    level: str = "INFO"
) -> Dict[str, Any]:
    """Record a structured lifecycle log entry both to stdout and the circular buffer."""
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "timestamp": now_iso,
        "category": category.upper(),
        "level": level.upper(),
        "message": message,
        "details": details or {}
    }

    # Record in memory
    AUDIT_EVENT_LOGS.append(record)

    # Format human-friendly string for stdout (Render Log Stream / Better Stack)
    detail_str = f" | {json.dumps(details)}" if details else ""
    formatted_msg = f"[{category.upper()}] {message}{detail_str}"

    if level.upper() == "ERROR":
        logger.error(formatted_msg)
    elif level.upper() == "WARNING":
        logger.warning(formatted_msg)
    elif level.upper() == "DEBUG":
        logger.debug(formatted_msg)
    else:
        logger.info(formatted_msg)

    return record


def get_recent_lifecycle_logs(
    limit: int = 100,
    category: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve recent lifecycle logs, optionally filtered by category."""
    logs = list(AUDIT_EVENT_LOGS)
    if category:
        cat_clean = category.strip().upper()
        logs = [entry for entry in logs if entry.get("category") == cat_clean]
    return logs[-limit:]


def clear_lifecycle_logs() -> None:
    """Clear in-memory lifecycle logs."""
    AUDIT_EVENT_LOGS.clear()
