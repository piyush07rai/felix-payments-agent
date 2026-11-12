"""Microsoft Teams Bot Framework Connector Client for Felix.

Handles sending outbound activities, progress indicators, and Adaptive Cards to
Microsoft Teams conversations using the Bot Framework REST Connector APIs.
Supports both live Microsoft Bot Framework connections and in-memory simulation mode.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("app.teams.client")

# In-memory buffer of outbound activities sent by Felix (used for offline testing and diagnostics)
SENT_TEAMS_ACTIVITIES: deque = deque(maxlen=100)


class TeamsBotClient:
    """Client for dispatching outbound activities to Microsoft Teams conversations."""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_password: Optional[str] = None,
        tenant_id: Optional[str] = None
    ):
        self._custom_app_id = app_id
        self._custom_app_pwd = app_password
        self._custom_tenant_id = tenant_id
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0.0

    @property
    def app_id(self) -> str:
        return self._custom_app_id if self._custom_app_id is not None else settings.TEAMS_APP_ID

    @property
    def app_password(self) -> str:
        return self._custom_app_pwd if self._custom_app_pwd is not None else settings.TEAMS_APP_PASSWORD

    @property
    def tenant_id(self) -> str:
        return self._custom_tenant_id if self._custom_tenant_id is not None else settings.TEAMS_TENANT_ID

    @property
    def is_configured(self) -> bool:
        """Check whether Microsoft Bot Framework credentials are configured."""
        return bool(self.app_id and self.app_password)

    def _get_access_token(self) -> Optional[str]:
        """Acquire Microsoft Bot Framework OAuth2 bearer token."""
        if not self.is_configured:
            return None

        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        # Microsoft OAuth2 Token Endpoint for Bot Framework
        token_url = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.app_id,
            "client_secret": self.app_password,
            "scope": "https://api.botframework.com/.default"
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                token = body.get("access_token")
                expires_in = body.get("expires_in", 3600)
                self._cached_token = token
                self._token_expiry = now + expires_in
                return token
        except Exception as exc:
            logger.exception("Failed to acquire Microsoft Bot Framework access token: %s", exc)
            return None

    def send_activity(
        self,
        service_url: str,
        conversation_id: str,
        text: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        reply_to_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch a message activity to a Microsoft Teams conversation."""
        activity: Dict[str, Any] = {
            "type": "message",
            "text": text,
        }
        if attachments:
            activity["attachments"] = attachments
        if reply_to_id:
            activity["replyToId"] = reply_to_id

        return self._send_connector_request(service_url, conversation_id, activity)

    def send_adaptive_card(
        self,
        service_url: str,
        conversation_id: str,
        card_content: Dict[str, Any],
        fallback_text: str = "Payment analysis report ready.",
        reply_to_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a rich Adaptive Card 1.5 attachment to Microsoft Teams."""
        attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card_content
        }
        return self.send_activity(
            service_url=service_url,
            conversation_id=conversation_id,
            text=fallback_text,
            attachments=[attachment],
            reply_to_id=reply_to_id
        )

    def send_typing_indicator(self, service_url: str, conversation_id: str) -> Dict[str, Any]:
        """Send a typing indicator activity while Felix investigates transaction data."""
        activity = {"type": "typing"}
        return self._send_connector_request(service_url, conversation_id, activity)

    def _send_connector_request(
        self,
        service_url: str,
        conversation_id: str,
        activity: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Send HTTP request to the Teams serviceUrl or buffer in simulated mode."""
        sim_record = {
            "service_url": service_url,
            "conversation_id": conversation_id,
            "activity": activity,
            "simulated": not self.is_configured
        }
        SENT_TEAMS_ACTIVITIES.append(sim_record)

        if not self.is_configured:
            logger.info(
                "TeamsBotClient (Simulated): Dispatched activity type '%s' to conversation '%s'",
                activity.get("type"), conversation_id
            )
            sim_id = f"teams_act_{int(time.time() * 1000)}"
            return {
                "id": sim_id,
                "ok": True,
                "conversationId": conversation_id,
                "simulated": True
            }

        # Real Microsoft Bot Framework Connector call
        clean_service_url = service_url.rstrip("/")
        encoded_conv_id = urllib.parse.quote(conversation_id, safe="")
        endpoint_url = f"{clean_service_url}/v3/conversations/{encoded_conv_id}/activities"

        token = self._get_access_token()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        data = json.dumps(activity).encode("utf-8")
        req = urllib.request.Request(endpoint_url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body
        except urllib.error.HTTPError as http_err:
            logger.exception("HTTP error calling Teams connector %s: %s", endpoint_url, http_err)
            return {"ok": False, "error": f"HTTP {http_err.code}: {http_err.reason}"}
        except Exception as exc:
            logger.exception("Unexpected error calling Teams connector: %s", exc)
            return {"ok": False, "error": str(exc)}


# Default singleton Teams bot client
teams_client = TeamsBotClient()
