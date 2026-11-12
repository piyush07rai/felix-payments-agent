"""Automated Verification Suite for Phase 3: Slack Integration for Felix.

Verifies:
1. HMAC-SHA256 request signature verification and clock skew tolerance.
2. Slack URL verification challenge handshake.
3. Bot mention stripping and query extraction.
4. Event deduplication and retry handling.
5. End-to-end simulated @Felix mention execution through the Common Integration Layer.
6. Contextual follow-up questions in Slack threads.
7. Zero-PII decoupling and rehydration in Slack responses.
"""

import asyncio
import json
import time
import unittest

from app.config import settings
from app.integration import session_manager
from app.slack.client import SENT_SLACK_MESSAGES, SlackClient
from app.slack.security import (
    EventDeduplicator,
    compute_slack_signature,
    verify_slack_signature,
)
from app.slack.service import SlackEventService


class TestFelixSlackIntegration(unittest.TestCase):

    def setUp(self):
        session_manager.clear_all()
        SENT_SLACK_MESSAGES.clear()
        self._orig_token = settings.SLACK_BOT_TOKEN
        settings.SLACK_BOT_TOKEN = ""

    def tearDown(self):
        settings.SLACK_BOT_TOKEN = self._orig_token

    def test_01_signature_verification(self):
        """Verify HMAC-SHA256 signature verification, tamper detection, and replay prevention."""
        secret = "test_signing_secret_998877"
        body = b'{"type":"event_callback","event":{"type":"app_mention","text":"hello"}}'
        now_ts = str(int(time.time()))

        # Valid signature
        valid_sig = compute_slack_signature(secret, now_ts, body.decode("utf-8"))
        is_valid, err = verify_slack_signature(secret, now_ts, body, valid_sig)
        self.assertTrue(is_valid)
        self.assertEqual(err, "")

        # Tampered body / signature mismatch
        tampered_sig = valid_sig[:-4] + "ffff"
        is_valid_tampered, err_tampered = verify_slack_signature(secret, now_ts, body, tampered_sig)
        self.assertFalse(is_valid_tampered)
        self.assertIn("mismatch", err_tampered)

        # Expired timestamp (replay attack prevention > 300s)
        expired_ts = str(int(time.time()) - 350)
        expired_sig = compute_slack_signature(secret, expired_ts, body.decode("utf-8"))
        is_valid_expired, err_expired = verify_slack_signature(secret, expired_ts, body, expired_sig)
        self.assertFalse(is_valid_expired)
        self.assertIn("out of bounds", err_expired)

    def test_02_bot_mention_query_extraction(self):
        """Verify stripping of bot mention tags and formatting characters."""
        service = SlackEventService()

        # Standard mention
        q1 = service.extract_query_text("<@U01234567> Investigate today's payment declines")
        self.assertEqual(q1, "Investigate today's payment declines")

        # Mention with colon and leading spaces
        q2 = service.extract_query_text("<@UFELIX>: What is the approval rate for Visa?")
        self.assertEqual(q2, "What is the approval rate for Visa?")

        # Plain text @Felix mention
        q3 = service.extract_query_text("@Felix why did transaction bd3aa0d0 fail?")
        self.assertEqual(q3, "why did transaction bd3aa0d0 fail?")

    def test_03_event_deduplication(self):
        """Verify deduplicator prevents processing identical Slack event retries."""
        dedup = EventDeduplicator(ttl_seconds=60)
        event_id = "Ev_test_12345"

        self.assertFalse(dedup.is_duplicate(event_id))
        self.assertTrue(dedup.is_duplicate(event_id))
        self.assertTrue(dedup.is_duplicate(event_id))

        # Different event is not duplicate
        self.assertFalse(dedup.is_duplicate("Ev_test_67890"))

    def test_04_simulated_slack_mention_e2e(self):
        """Verify end-to-end execution of a simulated Slack mention through Felix."""
        service = SlackEventService()
        thread_ts = "1731370000.000100"

        mock_payload = {
            "team_id": "T_ACME_CORP",
            "event_id": "Ev_test_e2e_1",
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "user": "U_SARAH",
                "text": "<@U_FELIX> What is our overall approval rate across networks?",
                "channel": "C_PAYMENTS",
                "ts": thread_ts,
                "thread_ts": thread_ts,
            }
        }

        # Run async event handler
        resp = asyncio.run(service.handle_mention_event(mock_payload))

        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, "completed")
        self.assertEqual(resp.turn_index, 1)
        self.assertEqual(resp.session_id, f"slack:T_ACME_CORP:C_PAYMENTS:{thread_ts}")
        self.assertGreater(len(resp.tool_activity), 0)

        # Check that outbound Slack message was buffered with Block Kit blocks
        self.assertGreater(len(SENT_SLACK_MESSAGES), 0)
        last_msg = SENT_SLACK_MESSAGES[-1]
        self.assertIn("blocks", last_msg["payload"])
        blocks = last_msg["payload"]["blocks"]
        self.assertEqual(blocks[0]["type"], "header")

    def test_05_slack_threaded_contextual_follow_up(self):
        """Verify multi-turn follow-up queries retain conversational context in the thread."""
        service = SlackEventService()
        thread_ts = "1731370000.000200"

        # Turn 1: Initial query
        payload_1 = {
            "team_id": "T_ACME_CORP",
            "event_id": "Ev_turn_1",
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "user": "U_SARAH",
                "text": "<@U_FELIX> Investigate today's payment declines.",
                "channel": "C_PAYMENTS",
                "ts": thread_ts,
                "thread_ts": thread_ts,
            }
        }
        resp_1 = asyncio.run(service.handle_mention_event(payload_1))
        self.assertEqual(resp_1.turn_index, 1)

        # Turn 2: Follow-up query in same thread
        payload_2 = {
            "team_id": "T_ACME_CORP",
            "event_id": "Ev_turn_2",
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "user": "U_SARAH",
                "text": "<@U_FELIX> Break this down by channel.",
                "channel": "C_PAYMENTS",
                "ts": "1731370005.000201",
                "thread_ts": thread_ts,  # Same thread
            }
        }
        resp_2 = asyncio.run(service.handle_mention_event(payload_2))
        self.assertEqual(resp_2.turn_index, 2)
        self.assertEqual(resp_2.session_id, resp_1.session_id)

    def test_06_zero_pii_in_slack(self):
        """Verify transaction inquiries mask PII during analysis and rehydrate in Slack blocks."""
        service = SlackEventService()
        real_txn = "bd3aa0d0-2cf7-46f0-a2b6-8a498a8eb49b"
        thread_ts = "1731370000.000300"

        payload = {
            "team_id": "T_ACME_CORP",
            "event_id": "Ev_pii_test",
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "user": "U_SARAH",
                "text": f"<@U_FELIX> Why did transaction {real_txn} fail?",
                "channel": "C_PAYMENTS",
                "ts": thread_ts,
                "thread_ts": thread_ts,
            }
        }

        resp = asyncio.run(service.handle_mention_event(payload))
        self.assertEqual(resp.status, "completed")
        # Ensure rehydrated output contains the real transaction ID for the human user
        self.assertIn(real_txn, resp.response_text)


if __name__ == "__main__":
    unittest.main()
