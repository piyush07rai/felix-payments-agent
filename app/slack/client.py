"""Slack Web API Client for Felix.

Handles sending threaded messages, updating interim status messages, and formatting
Slack Block Kit payloads using the Slack Web API (chat.postMessage, chat.update).
Supports both real Slack connections and in-memory simulated mode for offline testing.
"""

import json
import logging
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("app.slack.client")

# In-memory buffer of messages sent by Felix (used for automated testing and UI inspection)
SENT_SLACK_MESSAGES: deque = deque(maxlen=100)


class SlackClient:
    """Client for interacting with Slack Web API endpoints."""

    def __init__(self, bot_token: Optional[str] = None):
        self._custom_token = bot_token
        self.api_base = "https://slack.com/api"

    @property
    def bot_token(self) -> str:
        return self._custom_token if self._custom_token is not None else settings.SLACK_BOT_TOKEN

    @property
    def is_configured(self) -> bool:
        """Check whether a real Slack Bot Token is configured."""
        token = self.bot_token
        return bool(token and token.startswith("xoxb-"))

    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send a message to a Slack channel or thread using chat.postMessage."""
        payload: Dict[str, Any] = {
            "channel": channel,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks

        return self._send_api_request("chat.postMessage", payload)

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing Slack message using chat.update."""
        payload: Dict[str, Any] = {
            "channel": channel,
            "ts": ts,
            "text": text,
        }
        if blocks:
            payload["blocks"] = blocks

        return self._send_api_request("chat.update", payload)

    def _send_api_request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute HTTP request to Slack Web API or record in simulation buffer."""
        # Record into memory buffer for auditing/testing
        record = {
            "endpoint": endpoint,
            "payload": payload,
            "simulated": not self.is_configured
        }
        SENT_SLACK_MESSAGES.append(record)

        if not self.is_configured:
            logger.info("SlackClient (Simulated) %s: %s", endpoint, payload.get("text", "")[:120])
            import time
            sim_ts = f"{time.time():.6f}"
            return {
                "ok": True,
                "ts": sim_ts,
                "channel": payload.get("channel"),
                "message": payload,
                "simulated": True
            }

        # Real Slack Web API call
        url = f"{self.api_base}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                res_json = json.loads(body)
                if not res_json.get("ok"):
                    logger.error("Slack API error on %s: %s", endpoint, res_json.get("error"))
                return res_json
        except urllib.error.HTTPError as http_err:
            logger.exception("HTTP error calling Slack %s: %s", endpoint, http_err)
            return {"ok": False, "error": f"HTTP {http_err.code}: {http_err.reason}"}
        except Exception as exc:
            logger.exception("Unexpected error calling Slack %s: %s", endpoint, exc)
            return {"ok": False, "error": str(exc)}


# Default singleton Slack client
slack_client = SlackClient()
