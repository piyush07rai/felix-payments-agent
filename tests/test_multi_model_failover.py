"""Unit tests for Multi-Model Support, Option 1 Automatic Failover, and Request Lifecycle Logging."""

import unittest
from unittest.mock import patch, MagicMock

from app.agent import DeclineAnalystAgent
from app.audit_logging import log_lifecycle_event, get_recent_lifecycle_logs, clear_lifecycle_logs
from app.config import settings
from app.slack.client import SlackClient, SENT_SLACK_MESSAGES


class TestMultiModelAndFailover(unittest.TestCase):
    """Test suite covering Multi-Model orchestration, Option 1 Failover, and Lifecycle Logs."""

    def setUp(self):
        clear_lifecycle_logs()
        SENT_SLACK_MESSAGES.clear()

    def test_lifecycle_logging_records_and_retrieves(self):
        """Verify lifecycle logging records events with category, timestamp, and details."""
        log_lifecycle_event(
            "SLACK_INGRESS",
            "Slack request received from user U123",
            {"channel": "C999", "query": "Why did Visa drop?"}
        )
        log_lifecycle_event(
            "LLM_DISPATCH",
            "Dispatching to primary provider Gemini",
            {"model": "gemini-3.8-flash"}
        )

        logs = get_recent_lifecycle_logs(limit=10)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["category"], "SLACK_INGRESS")
        self.assertEqual(logs[1]["category"], "LLM_DISPATCH")
        self.assertIn("Visa", logs[0]["details"]["query"])

    def test_slack_client_add_reaction(self):
        """Verify SlackClient sends or records emoji reactions for immediate receipt acknowledgment."""
        client = SlackClient(bot_token="simulated_token")
        res = client.add_reaction(channel="C12345", timestamp="1234567.89", name="eyes")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("channel"), "C12345")
        self.assertTrue(res.get("simulated"))

    def test_agent_provider_switch(self):
        """Verify dynamic runtime provider switching across Gemini, OpenAI, and Local Fallback."""
        agent = DeclineAnalystAgent()

        agent.set_provider("openai", model="gpt-4o-mini", enable_failover=True)
        st = agent.get_status()
        self.assertEqual(st["active_provider"], "openai")
        self.assertEqual(st["model"], "gpt-4o-mini")
        self.assertTrue(st["enable_failover"])

        agent.set_provider("fallback")
        st2 = agent.get_status()
        self.assertEqual(st2["active_provider"], "fallback")
        self.assertEqual(st2["model"], "local-analytical-engine")

    def test_automatic_failover_to_fallback(self):
        """Verify Option 1 Automatic Failover engages local engine when external APIs are unconfigured."""
        agent = DeclineAnalystAgent(api_key="", openai_key="", enable_failover=True)
        agent.set_provider("gemini")

        # Run chat query
        events_emitted = []
        def event_cb(ev):
            events_emitted.append(ev)

        result = agent.run_chat(
            message="Why did approval rate drop for Chase?",
            event_callback=event_cb
        )

        # Investigation must succeed via failover rather than crashing
        self.assertIn(result["investigation_status"], ("completed", "failover_fallback_completed"))
        self.assertTrue(len(result["response"]) > 0)
        self.assertNotEqual(result["response"].strip().lower(), "investigation completed.")
        self.assertIn("HDFC Bank", result["response"])
        self.assertTrue(any(ev.get("stage") == "failover" or "facing_error" in str(ev.get("stage")) for ev in events_emitted))

    def test_synthesize_analysis_from_tool_data(self):
        """Verify that synthesizing analysis from tool activity generates full executive brief."""
        from app.tools import get_baseline, lookup_response_code
        agent = DeclineAnalystAgent(api_key="", openai_key="", enable_failover=True)
        tool_activity = [
            {"tool": "get_baseline", "raw_result": get_baseline("approval_rate", "issuer_bank")},
            {"tool": "lookup_response_code", "raw_result": lookup_response_code("91")}
        ]
        findings = []
        resp = agent._synthesize_analysis_from_tool_data(
            prompt="Find issuer-specific approval issues.",
            tool_activity=tool_activity,
            findings=findings
        )
        self.assertNotIn("investigation completed", resp.lower())
        self.assertIn("### Executive Summary", resp)
        self.assertIn("### Key Evidence & Dimensional Baselines", resp)
        self.assertIn("### Root Cause Diagnosis", resp)
        self.assertIn("### Recommended Remediation Playbooks", resp)
        self.assertIn("HDFC Bank", resp)


if __name__ == "__main__":
    unittest.main()
