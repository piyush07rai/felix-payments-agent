"""Automated Verification Suite for Phase 2: Common Enterprise Integration Layer.

Verifies:
1. Message receipt through the unified schema.
2. Query comprehension and existing tools execution.
3. Multi-turn contextual follow-ups in threads.
4. Workspace, channel, and user isolation.
5. Zero-PII surrogate tokenization and local rehydration.
6. Unified response construction with Slack blocks & Teams Adaptive Cards.
7. Error handling and validation tracking.
"""

import unittest
from app.integration import (
    CommonMessageRequest,
    CommonMessageResponse,
    common_message_processor,
    session_manager,
)


class TestFelixCommonIntegration(unittest.TestCase):

    def setUp(self):
        # Clear sessions before each test
        session_manager.clear_all()

    def test_01_message_receipt_and_tool_execution(self):
        """Verify Felix receives a normalized message, executes existing tools, and generates response."""
        req = CommonMessageRequest(
            platform="slack",
            workspace_id="acme_corp",
            user_id="usr_001",
            conversation_id="chan_payments",
            thread_id="th_1001",
            message="What is the overall approval rate across networks?"
        )

        resp = common_message_processor.process_message(req)

        self.assertEqual(resp.status, "completed")
        self.assertEqual(resp.turn_index, 1)
        self.assertEqual(resp.session_id, "slack:acme_corp:chan_payments:th_1001")
        self.assertGreater(len(resp.tool_activity), 0)
        self.assertTrue(len(resp.response_text) > 20)
        self.assertIn("slack", resp.platform_payload)
        self.assertIn("teams", resp.platform_payload)
        self.assertTrue(len(resp.platform_payload["slack"]["blocks"]) > 0)
        self.assertEqual(resp.platform_payload["teams"]["adaptive_card"]["type"], "AdaptiveCard")

    def test_02_contextual_follow_up(self):
        """Verify multi-turn contextual continuity within the same conversation thread."""
        req1 = CommonMessageRequest(
            platform="slack",
            workspace_id="acme_corp",
            user_id="usr_001",
            conversation_id="chan_payments",
            thread_id="th_1001",
            message="What is the overall approval rate across networks?"
        )
        resp1 = common_message_processor.process_message(req1)
        self.assertEqual(resp1.turn_index, 1)

        # Follow-up in the same thread
        req2 = CommonMessageRequest(
            platform="slack",
            workspace_id="acme_corp",
            user_id="usr_001",
            conversation_id="chan_payments",
            thread_id="th_1001",
            message="Can you break down the decline rate by channel?"
        )
        resp2 = common_message_processor.process_message(req2)
        self.assertEqual(resp2.turn_index, 2)
        self.assertEqual(resp2.session_id, resp1.session_id)

        session = session_manager.get_session(resp1.session_id)
        self.assertIsNotNone(session)
        self.assertEqual(len(session.turns), 2)

    def test_03_workspace_and_channel_isolation(self):
        """Verify distinct workspaces and channels have completely isolated sessions."""
        req_a = CommonMessageRequest(
            platform="slack",
            workspace_id="workspace_alpha",
            user_id="user_a",
            conversation_id="chan_1",
            thread_id="th_1",
            message="What is the approval rate?"
        )
        resp_a = common_message_processor.process_message(req_a)

        req_b = CommonMessageRequest(
            platform="teams",
            workspace_id="workspace_beta",
            user_id="user_b",
            conversation_id="chan_2",
            thread_id="th_2",
            message="What is the decline rate?"
        )
        resp_b = common_message_processor.process_message(req_b)

        self.assertNotEqual(resp_a.session_id, resp_b.session_id)
        self.assertEqual(resp_a.workspace_id, "workspace_alpha")
        self.assertEqual(resp_b.workspace_id, "workspace_beta")
        self.assertEqual(resp_a.turn_index, 1)
        self.assertEqual(resp_b.turn_index, 1)

    def test_04_zero_pii_rehydration(self):
        """Verify transaction inquiry runs through Zero-PII decoupling and rehydrates properly."""
        real_txn = "bd3aa0d0-2cf7-46f0-a2b6-8a498a8eb49b"
        req = CommonMessageRequest(
            platform="teams",
            workspace_id="acme_corp",
            user_id="usr_001",
            conversation_id="chan_ops",
            thread_id="th_2001",
            message=f"Please check transaction {real_txn}"
        )

        resp = common_message_processor.process_message(req)
        self.assertEqual(resp.status, "completed")
        # Ensure rehydration provided the real ID to the user
        self.assertIn(real_txn, resp.response_text)
        self.assertGreater(len(resp.tool_activity), 0)

    def test_05_validation_and_error_handling(self):
        """Verify invalid requests return structured error responses."""
        req = CommonMessageRequest(
            platform="slack",
            workspace_id="acme_corp",
            user_id="usr_001",
            conversation_id="chan_test",
            message="   "
        )
        resp = common_message_processor.process_message(req)
        self.assertEqual(resp.status, "error")
        self.assertIsNotNone(resp.error)
        self.assertIn("empty", resp.error.lower())


if __name__ == "__main__":
    unittest.main()
