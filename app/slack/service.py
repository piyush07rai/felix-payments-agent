"""Slack Event Dispatcher and Processing Service for Felix.

Parses incoming Slack events (app_mention, message), strips bot mention tags,
maintains thread continuity, posts progressive interim updates, and invokes the
Phase 2 Common Message Processor.
"""

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from app.audit_logging import log_lifecycle_event
from app.config import settings
from app.integration import CommonMessageRequest, CommonMessageResponse, common_message_processor, convert_markdown_to_slack_mrkdwn
from app.slack.client import SlackClient, slack_client

logger = logging.getLogger("app.slack.service")


class SlackEventService:
    """Orchestrates Slack event handling and routes to the Common Integration Layer."""

    def __init__(self, client: Optional[SlackClient] = None, processor=None):
        self.client = client or slack_client
        self.processor = processor or common_message_processor

    @staticmethod
    def extract_query_text(raw_text: str) -> str:
        """Strip bot mention tags (e.g. '<@U012345>' or '@Felix') to extract pure user query."""
        if not raw_text:
            return ""
        # Strip Slack user mention tags like <@U12345678>
        cleaned = re.sub(r"<@[A-Za-z0-9_-]+>", "", raw_text)
        # Strip literal @Felix mentions if present
        cleaned = re.sub(r"@felix\b", "", cleaned, flags=re.IGNORECASE)
        # Strip leading punctuation/whitespace
        return cleaned.strip(" :,\t\n\r")

    async def handle_mention_event(self, event_payload: Dict[str, Any]) -> Optional[CommonMessageResponse]:
        """Asynchronously process a Slack app_mention event and respond in thread."""
        team_id = event_payload.get("team_id") or event_payload.get("context_team_id") or "slack_workspace"
        event = event_payload.get("event") or {}

        user_id = event.get("user", "unknown_user")
        channel_id = event.get("channel", "")
        message_ts = event.get("ts", "")
        # If thread_ts is absent, the incoming message is top-level; start a thread using its message_ts
        thread_ts = event.get("thread_ts") or message_ts
        raw_text = event.get("text", "")

        query = self.extract_query_text(raw_text)
        if not query:
            query = "Help: Provide overview of recent payment decline trends."

        log_lifecycle_event(
            "SLACK_INGRESS",
            f"Slack mention from user {user_id} in channel {channel_id}",
            {"channel": channel_id, "thread_ts": thread_ts, "query": query[:80]}
        )

        # 1. Immediate visual acknowledgment in Slack (Emoji Reaction within ~100ms)
        try:
            await asyncio.to_thread(
                self.client.add_reaction,
                channel=channel_id,
                timestamp=message_ts,
                name="eyes"
            )
        except Exception as react_err:
            logger.debug("Could not add Slack reaction emoji: %s", react_err)

        # 2. Post immediate progressive status message in thread (Acknowledge human user)
        interim_post = await asyncio.to_thread(
            self.client.post_message,
            channel=channel_id,
            text="🔍 Felix is analyzing transaction data...",
            thread_ts=thread_ts,
            blocks=[
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "🔍 *Felix* is analyzing transaction data and running decline diagnostics..."
                        }
                    ]
                }
            ]
        )
        interim_ts = interim_post.get("ts")

        # 3. Map into normalized Phase 2 CommonMessageRequest
        req = CommonMessageRequest(
            platform="slack",
            workspace_id=team_id,
            user_id=user_id,
            conversation_id=channel_id,
            thread_id=thread_ts,
            message=query,
            metadata={
                "slack_event_id": event_payload.get("event_id"),
                "slack_message_ts": message_ts,
                "slack_channel": channel_id,
            }
        )

        # 3. Process through Common Integration Layer (Zero-PII decoupling + Felix agent tools)
        try:
            resp: CommonMessageResponse = await self.processor.process_message_async(req)
        except Exception as exc:
            logger.exception("Error processing Slack message via integration processor: %s", exc)
            err_text = f"⚠️ *Felix encountered an issue while investigating:* {str(exc)}"
            if interim_ts:
                await asyncio.to_thread(
                    self.client.update_message,
                    channel=channel_id,
                    ts=interim_ts,
                    text=err_text
                )
            else:
                await asyncio.to_thread(
                    self.client.post_message,
                    channel=channel_id,
                    text=err_text,
                    thread_ts=thread_ts
                )
            return None

        # 4. Deliver final rich Block Kit response back to Slack thread
        slack_blocks = resp.platform_payload.get("slack", {}).get("blocks")
        final_text = resp.response_text or "Analysis complete."
        slack_plain_text = convert_markdown_to_slack_mrkdwn(final_text)

        if interim_ts:
            # Update the interim thinking message with final diagnostic analysis
            await asyncio.to_thread(
                self.client.update_message,
                channel=channel_id,
                ts=interim_ts,
                text=slack_plain_text[:2900],
                blocks=slack_blocks
            )
        else:
            await asyncio.to_thread(
                self.client.post_message,
                channel=channel_id,
                text=slack_plain_text[:2900],
                thread_ts=thread_ts,
                blocks=slack_blocks
            )

        log_lifecycle_event(
            "SLACK_EGRESS",
            f"Delivered Slack response for session {resp.session_id} (turn #{resp.turn_index})",
            {"channel": channel_id, "thread_ts": thread_ts, "tools_count": len(resp.tool_activity)}
        )
        return resp


# Default singleton Slack service
slack_service = SlackEventService()
