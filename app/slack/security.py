"""Slack Request Signature Verification and Event Deduplication for Felix.

Enforces HMAC-SHA256 signature verification according to Slack security standards
and prevents duplicate event processing during network retries.
"""

import hashlib
import hmac
import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("app.slack.security")

# Maximum permitted age of incoming Slack request (5 minutes) to prevent replay attacks
SLACK_REQUEST_MAX_AGE_SECONDS = 300


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    tolerance: int = SLACK_REQUEST_MAX_AGE_SECONDS
) -> Tuple[bool, str]:
    """Verify that an incoming webhook request originated from Slack using HMAC-SHA256.

    Args:
        signing_secret: Slack app signing secret from developer console.
        timestamp: Value of 'X-Slack-Request-Timestamp' header.
        body: Raw request payload bytes.
        signature: Value of 'X-Slack-Signature' header (e.g. 'v0=a2114d57b2...').
        tolerance: Maximum allowed clock skew in seconds (default 300s).

    Returns:
        Tuple of (is_valid: bool, error_message: str).
    """
    if not signing_secret or not signing_secret.strip():
        # If signing secret is not configured, reject unless explicitly in dev simulation
        return False, "SLACK_SIGNING_SECRET is not configured on the server."

    if not timestamp or not signature:
        return False, "Missing Slack security headers (timestamp or signature)."

    # 1. Replay attack check: verify request is recent
    try:
        req_time = int(timestamp)
        now = int(time.time())
        if abs(now - req_time) > tolerance:
            return False, f"Request timestamp out of bounds (clock skew: {abs(now - req_time)}s > {tolerance}s)."
    except (ValueError, TypeError):
        return False, f"Invalid timestamp format: '{timestamp}'"

    # 2. Compute expected HMAC-SHA256 signature
    try:
        raw_body_str = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        sig_basestring = f"v0:{timestamp}:{raw_body_str}".encode("utf-8")
        computed_hash = hmac.new(
            signing_secret.encode("utf-8"),
            sig_basestring,
            hashlib.sha256
        ).hexdigest()
        expected_signature = f"v0={computed_hash}"

        # Constant-time comparison to prevent timing attacks
        if hmac.compare_digest(expected_signature, signature):
            return True, ""
        else:
            return False, "HMAC-SHA256 signature mismatch."
    except Exception as exc:
        logger.exception("Error computing Slack signature: %s", exc)
        return False, f"Signature calculation failed: {str(exc)}"


def compute_slack_signature(signing_secret: str, timestamp: str, body: str) -> str:
    """Helper to generate valid Slack signature (used for testing and simulation)."""
    sig_basestring = f"v0:{timestamp}:{body}".encode("utf-8")
    computed_hash = hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()
    return f"v0={computed_hash}"


class EventDeduplicator:
    """Thread-safe event deduplication cache to prevent handling duplicate Slack retries."""

    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self._seen_events: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, event_id: Optional[str]) -> bool:
        """Check if an event_id was already processed within the TTL window."""
        if not event_id:
            return False

        now = time.time()
        with self._lock:
            # Purge expired event entries
            expired = [eid for eid, exp in self._seen_events.items() if exp < now]
            for eid in expired:
                del self._seen_events[eid]

            if event_id in self._seen_events:
                logger.warning("Duplicate Slack event detected and skipped: %s", event_id)
                return True

            # Register event with expiry timestamp
            self._seen_events[event_id] = now + self.ttl
            return False

    def clear(self) -> None:
        """Clear cache for test runs."""
        with self._lock:
            self._seen_events.clear()


# Default singleton deduplicator
slack_deduplicator = EventDeduplicator()
