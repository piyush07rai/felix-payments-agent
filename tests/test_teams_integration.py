"""Automated Verification Suite for Phase 4: Microsoft Teams Integration for Felix.

Verifies:
1. Activity validation and Microsoft Bot Framework authorization header enforcement.
2. Activity deduplication preventing duplicate execution of retried network calls.
3. Microsoft Teams mention stripping (<at>Felix</at>, @Felix, HTML entities).
4. End-to-end simulated @Felix mention execution through the Common Integration Layer.
5. Rich Adaptive Card 1.5 generation for Microsoft Teams responses.
6. Contextual multi-turn follow-up queries in the same Teams conversation thread.
7. Tenant-level and channel-level isolation across distinct Microsoft organizations.
"""

import asyncio
import json
import time
import unittest

from app.config import settings
from app.integration import session_manager
from app.teams.client import SENT_TEAMS_ACTIVITIES, TeamsBotClient
from app.teams.security import (
    ActivityDeduplicator,
    teams_deduplicator,
    validate_teams_activity,
)
from app.teams.service import TeamsEventService


class TestFelixTeamsIntegration(unittest.TestCase):

    def setUp(self):
        session_manager.clear_all()
        SENT_TEAMS_ACTIVITIES.clear()
        teams_deduplicator.clear()

    def test_01_activity_validation(self):
        """Verify activity structure validation and Bot Framework authorization rules."""
        # 1. Valid activity payload
        valid_activity = {
            "type": "message",
            "id": "act_001",
            "conversation": {"id": "19:channel_abc@thread.tacv2", "tenantId": "tenant_111"},
            "from": {"id": "29:user_xyz", "name": "Alice"},
            "text": "<at>Felix</at> What is the overall authorization rate?"
        }
        is_valid, err = validate_teams_activity(valid_activity, auth_header=None)
        self.assertTrue(is_valid)
        self.assertEqual(err, "")

        # 2. Missing conversation.id target
        invalid_conv = {
            "type": "message",
            "id": "act_002",
            "conversation": {},
            "text": "hello"
        }
        is_valid, err = validate_teams_activity(invalid_conv, auth_header=None)
        self.assertFalse(is_valid)
        self.assertIn("conversation.id", err)

        # 3. Missing type field
        missing_type = {
            "id": "act_003",
            "conversation": {"id": "19:conv@thread.tacv2"}
        }
        is_valid, err = validate_teams_activity(missing_type, auth_header=None)
        self.assertFalse(is_valid)
        self.assertIn("type", err)

        # 4. Bearer token enforcement when TEAMS_APP_ID is active
        original_app_id = settings.TEAMS_APP_ID
        try:
            settings.TEAMS_APP_ID = "mock-app-guid-12345"
            # Without Authorization header -> should fail
            is_valid, err = validate_teams_activity(valid_activity, auth_header=None)
            self.assertFalse(is_valid)
            self.assertIn("Missing Authorization Bearer token", err)

            # With non-Bearer Authorization header -> should fail
            is_valid, err = validate_teams_activity(valid_activity, auth_header="Basic 12345")
            self.assertFalse(is_valid)
            self.assertIn("must be a Bearer token", err)

            # With valid Bearer header -> should pass
            is_valid, err = validate_teams_activity(valid_activity, auth_header="Bearer eyJhbGciOi...")
            self.assertTrue(is_valid)
        finally:
            settings.TEAMS_APP_ID = original_app_id

    def test_02_activity_deduplication(self):
        """Verify activity deduplicator suppresses duplicate retries within TTL."""
        dedup = ActivityDeduplicator(ttl_seconds=60)
        activity_id = "teams_activity_uuid_999"

        self.assertFalse(dedup.is_duplicate(activity_id))
        self.assertTrue(dedup.is_duplicate(activity_id))
        self.assertTrue(dedup.is_duplicate(activity_id))

        # Different activity ID is allowed
        self.assertFalse(dedup.is_duplicate("teams_activity_uuid_888"))

    def test_03_mention_stripping_and_query_extraction(self):
        """Verify stripping of Teams <at>Felix</at> XML tags and formatting."""
        service = TeamsEventService()

        # 1. Standard Teams XML mention
        t1 = service.extract_query_text("<at>Felix</at> Why has our RuPay approval rate dropped?")
        self.assertEqual(t1, "Why has our RuPay approval rate dropped?")

        # 2. Teams mention with punctuation and HTML entities
        t2 = service.extract_query_text("<at>Felix</at>: &nbsp;Which issuer banks are affected?&amp;")
        self.assertEqual(t2, "Which issuer banks are affected?&")

        # 3. Mention with bot user ID / attributes
        t3 = service.extract_query_text('<at id="28:bot_guid">Felix</at> Show recent declines for card 481400******1234')
        self.assertEqual(t3, "Show recent declines for card 481400******1234")

        # 4. Plain text @Felix mention
        t4 = service.extract_query_text("@Felix Investigate today's payment declines.")
        self.assertEqual(t4, "Investigate today's payment declines.")

    def test_04_simulated_teams_mention_e2e(self):
        """Verify end-to-end execution of a simulated Teams activity through Felix."""
        service = TeamsEventService()

        mock_activity = {
            "type": "message",
            "id": "teams_msg_e2e_101",
            "timestamp": "2026-11-12T02:00:00Z",
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "channelId": "msteams",
            "from": {
                "id": "29:usr_alex_77",
                "name": "Alex Mercer"
            },
            "conversation": {
                "id": "19:payments_war_room@thread.tacv2",
                "tenantId": "tenant_enterprise_alpha",
                "isGroup": True,
                "conversationType": "channel"
            },
            "recipient": {
                "id": "28:bot_felix_app_id",
                "name": "Felix"
            },
            "text": "<at>Felix</at> Why has our RuPay approval rate dropped?",
            "channelData": {
                "teamsChannelId": "19:payments_war_room@thread.tacv2",
                "tenant": {"id": "tenant_enterprise_alpha"}
            }
        }

        resp = asyncio.run(service.handle_activity(mock_activity))

        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, "completed")
        self.assertEqual(resp.turn_index, 1)
        self.assertEqual(resp.workspace_id, "tenant_enterprise_alpha")
        self.assertEqual(resp.conversation_id, "19:payments_war_room@thread.tacv2")

        # Ensure tools ran and output contains analysis
        self.assertGreater(len(resp.tool_activity), 0)
        self.assertIn("RuPay", resp.response_text)

        # Verify Adaptive Card 1.5 payload generation
        teams_payload = resp.platform_payload.get("teams", {})
        self.assertIn("adaptive_card", teams_payload)
        card = teams_payload["adaptive_card"]
        self.assertEqual(card.get("type"), "AdaptiveCard")
        self.assertEqual(card.get("version"), "1.5")
        self.assertGreater(len(card.get("body", [])), 0)

        # Verify outbound activity buffered in SENT_TEAMS_ACTIVITIES
        self.assertGreaterEqual(len(SENT_TEAMS_ACTIVITIES), 1)
        last_outbound = list(SENT_TEAMS_ACTIVITIES)[-1]
        self.assertEqual(last_outbound["conversation_id"], "19:payments_war_room@thread.tacv2")
        self.assertEqual(last_outbound["activity"]["type"], "message")
        self.assertEqual(
            last_outbound["activity"]["attachments"][0]["contentType"],
            "application/vnd.microsoft.card.adaptive"
        )

    def test_05_contextual_follow_up_in_teams_thread(self):
        """Verify follow-up questions in the same Teams channel thread retain context."""
        service = TeamsEventService()
        tenant_id = "tenant_enterprise_alpha"
        conv_id = "19:incident_investigation@thread.tacv2"

        # Turn 1: Primary inquiry
        act1 = {
            "type": "message",
            "id": "teams_msg_t1",
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "channelId": "msteams",
            "from": {"id": "29:usr_alex_77", "name": "Alex Mercer"},
            "conversation": {"id": conv_id, "tenantId": tenant_id},
            "text": "<at>Felix</at> Why has our RuPay approval rate dropped?"
        }
        resp1 = asyncio.run(service.handle_activity(act1))
        self.assertIsNotNone(resp1)
        self.assertEqual(resp1.turn_index, 1)

        # Turn 2: Follow-up question relying on turn 1's context
        act2 = {
            "type": "message",
            "id": "teams_msg_t2",
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "channelId": "msteams",
            "from": {"id": "29:usr_alex_77", "name": "Alex Mercer"},
            "conversation": {"id": conv_id, "tenantId": tenant_id},
            "text": "<at>Felix</at> Which issuer banks are affected?"
        }
        resp2 = asyncio.run(service.handle_activity(act2))
        self.assertIsNotNone(resp2)
        self.assertEqual(resp2.turn_index, 2)
        self.assertEqual(resp2.session_id, resp1.session_id)

        # Verify session state stores both turns
        session = session_manager.get_session(resp1.session_id)
        self.assertIsNotNone(session)
        self.assertEqual(len(session.turns), 2)
        self.assertEqual(session.turns[0]["turn_index"], 1)
        self.assertEqual(session.turns[1]["turn_index"], 2)

    def test_06_tenant_and_channel_isolation(self):
        """Verify different Microsoft Tenants and Teams channels are strictly isolated."""
        service = TeamsEventService()

        # Tenant Alpha
        act_alpha = {
            "type": "message",
            "id": "teams_alpha_1",
            "conversation": {"id": "19:channel_finance@thread.tacv2", "tenantId": "tenant_alpha"},
            "from": {"id": "29:user_alpha"},
            "text": "<at>Felix</at> Show declines for RuPay"
        }
        resp_alpha = asyncio.run(service.handle_activity(act_alpha))

        # Tenant Beta
        act_beta = {
            "type": "message",
            "id": "teams_beta_1",
            "conversation": {"id": "19:channel_finance@thread.tacv2", "tenantId": "tenant_beta"},
            "from": {"id": "29:user_beta"},
            "text": "<at>Felix</at> Show declines for Visa"
        }
        resp_beta = asyncio.run(service.handle_activity(act_beta))

        self.assertNotEqual(resp_alpha.session_id, resp_beta.session_id)
        self.assertEqual(resp_alpha.turn_index, 1)
        self.assertEqual(resp_beta.turn_index, 1)


if __name__ == "__main__":
    unittest.main()
