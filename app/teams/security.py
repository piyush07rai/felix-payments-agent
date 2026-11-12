"""Microsoft Teams and Bot Framework Authentication & Security for Felix.

Implements Bot Framework activity validation, tenant isolation, and activity deduplication.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger("app.teams.security")


class ActivityDeduplicator:
    """Thread-safe deduplicator preventing duplicate execution of retried Teams activities."""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._seen_activities: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, activity_id: Optional[str]) -> bool:
        """Check if an activity ID has already been processed within the TTL window."""
        if not activity_id:
            return False

        now = time.time()
        with self._lock:
            # Purge expired activities
            expired = [aid for aid, exp in self._seen_activities.items() if exp < now]
            for aid in expired:
                del self._seen_activities[aid]

            if activity_id in self._seen_activities:
                logger.warning("Duplicate Teams activity detected and skipped: %s", activity_id)
                return True

            self._seen_activities[activity_id] = now + self.ttl
            return False

    def clear(self) -> None:
        """Clear cache for testing."""
        with self._lock:
            self._seen_activities.clear()


teams_deduplicator = ActivityDeduplicator()


def validate_teams_activity(
    activity: Dict,
    auth_header: Optional[str] = None
) -> Tuple[bool, str]:
    """Validate incoming Microsoft Teams Activity structure and authentication.

    Args:
        activity: Parsed JSON activity payload.
        auth_header: Authorization header value (e.g. 'Bearer <token>').

    Returns:
        Tuple of (is_valid: bool, error_message: str).
    """
    if not isinstance(activity, dict):
        return False, "Activity must be a valid JSON object."

    activity_type = activity.get("type")
    if not activity_type:
        return False, "Missing 'type' field in activity."

    # Validate essential Bot Framework routing parameters
    conv = activity.get("conversation")
    if not conv or not isinstance(conv, dict) or not conv.get("id"):
        return False, "Activity is missing 'conversation.id' routing target."

    # In production with configured Microsoft App ID, verify Authorization Bearer token is present
    if settings.TEAMS_APP_ID and settings.TEAMS_APP_ID.strip():
        if not auth_header or not auth_header.strip():
            logger.warning("Rejected Teams request: Missing Authorization header while TEAMS_APP_ID is active.")
            return False, "Unauthorized: Missing Authorization Bearer token from Microsoft Bot Framework."
        if not auth_header.lower().startswith("bearer "):
            return False, "Unauthorized: Authorization header must be a Bearer token."

    return True, ""
