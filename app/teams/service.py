"""Microsoft Teams Activity Service for Felix.

Parses incoming Teams activities, strips bot mention tags (<at>Felix</at>),
captures tenant, user, channel, and conversation references, routes inquiries
through the Common Integration Layer, and delivers Teams Adaptive Cards.
"""

import asyncio
import html
import logging
import re
from typing import Any, Dict, Optional

from app.config import settings
from app.integration import CommonMessageRequest, CommonMessageResponse, common_message_processor
from app.teams.client import TeamsBotClient, teams_client

logger = logging.getLogger("app.teams.service")


class TeamsEventService:
    """Service handling Microsoft Teams activities and routing to Felix."""

    def __init__(self, client: Optional[TeamsBotClient] = None, processor=None):
        self.client = client or teams_client
        self.processor = processor or common_message_processor

    @staticmethod
    def extract_query_text(raw_text: str) -> str:
        """Strip Teams mention tags (e.g. '<at>Felix</at>') and HTML entities to extract clean query."""
        if not raw_text:
            return ""

        # Unescape HTML entities like &nbsp;
        text = html.unescape(raw_text).replace('\xa0', ' ')

        # Strip Teams <at>...</at> tags (with or without attributes like id="...")
        cleaned = re.sub(r"<at\b[^>]*>.*?</at>", "", text, flags=re.IGNORECASE | re.DOTALL)

        # Strip plain-text @Felix mentions
        cleaned = re.sub(r"@felix\b", "", cleaned, flags=re.IGNORECASE)

        # Strip remaining XML/HTML tags if present
        cleaned = re.sub(r"<[^>]+>", "", cleaned)

        return cleaned.strip(" :,\t\n\r\xa0")

    async def handle_activity(self, activity: Dict[str, Any]) -> Optional[CommonMessageResponse]:
        """Asynchronously process an incoming Microsoft Teams message activity."""
        activity_type = activity.get("type")
        if activity_type != "message":
            logger.info("Ignoring non-message Teams activity of type '%s'", activity_type)
            return None

        # 1. Capture Microsoft Teams routing and identity parameters
        conv_obj = activity.get("conversation") or {}
        channel_data = activity.get("channelData") or {}
        from_obj = activity.get("from") or {}

        # Tenant ID provides strict organization-level isolation
        tenant_id = (
            conv_obj.get("tenantId")
            or channel_data.get("tenant", {}).get("id")
            or settings.TEAMS_TENANT_ID
            or "msteams_tenant"
        )

        user_id = from_obj.get("id", "teams_user")
        user_name = from_obj.get("name")
        conv_id = conv_obj.get("id", "")
        channel_id = channel_data.get("teamsChannelId") or conv_id
        message_id = activity.get("id", "")
        service_url = activity.get("serviceUrl", "https://smba.trafficmanager.net/amer/")
        raw_text = activity.get("text", "")

        query = self.extract_query_text(raw_text)
        if not query:
            query = "Provide overview of payment decline trends and authorization rate."

        logger.info(
            "Handling Teams activity from tenant '%s', user '%s' in conv '%s': '%s'",
            tenant_id, user_name or user_id, conv_id[:40], query[:80]
        )

        # 2. Capture complete Conversation Reference for proactive/threaded delivery
        conversation_reference = {
            "activityId": message_id,
            "user": from_obj,
            "bot": activity.get("recipient"),
            "conversation": conv_obj,
            "channelId": activity.get("channelId", "msteams"),
            "serviceUrl": service_url,
        }

        # 3. Progressive Delivery: send typing indicator & interim acknowledgement
        if service_url:
            await asyncio.to_thread(self.client.send_typing_indicator, service_url, conv_id)

        # 4. Map into normalized Phase 2 CommonMessageRequest
        req = CommonMessageRequest(
            platform="teams",
            workspace_id=tenant_id,
            user_id=user_id,
            user_name=user_name,
            conversation_id=conv_id,
            thread_id=conv_id,  # Teams conversation.id preserves exact thread context
            message=query,
            metadata={
                "teams_message_id": message_id,
                "teams_channel_id": channel_id,
                "service_url": service_url,
                "conversation_reference": conversation_reference,
            }
        )

        # 5. Route through Common Integration Layer (Zero-PII shield + existing agent tools)
        try:
            resp: CommonMessageResponse = await self.processor.process_message_async(req)
        except Exception as exc:
            logger.exception("Error processing Teams activity via common processor: %s", exc)
            err_text = f"⚠️ **Felix encountered an issue while investigating:** {str(exc)}"
            if service_url:
                await asyncio.to_thread(
                    self.client.send_activity,
                    service_url,
                    conv_id,
                    err_text,
                    reply_to_id=message_id
                )
            return None

        # 6. Deliver rich Teams Adaptive Card 1.5 payload back to conversation
        teams_card = resp.platform_payload.get("teams", {}).get("adaptive_card")
        if service_url and teams_card:
            await asyncio.to_thread(
                self.client.send_adaptive_card,
                service_url=service_url,
                conversation_id=conv_id,
                card_content=teams_card,
                fallback_text=resp.response_text,
                reply_to_id=message_id
            )
        elif service_url:
            await asyncio.to_thread(
                self.client.send_activity,
                service_url=service_url,
                conversation_id=conv_id,
                text=resp.response_text,
                reply_to_id=message_id
            )

        logger.info(
            "Delivered Teams Adaptive Card for session %s (turn #%d, %d tools)",
            resp.session_id, resp.turn_index, len(resp.tool_activity)
        )
        return resp


# Default singleton Teams service
teams_service = TeamsEventService()
