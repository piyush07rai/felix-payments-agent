"""Automated Verification Suite for Phase 5: Enterprise Integrations Dashboard for Felix.

Verifies:
1. Real status computation (Connected, Disconnected, Configuration Required).
2. Backend secret masking (ensuring raw tokens/secrets are never exposed).
3. Configuration update and validation logic (Slack and Teams credentials).
4. Activity recording, persistence, and multi-criteria filtering (platform, status, search).
5. Connectivity testing and health diagnostic reports.
6. Platform enable, disable, and disconnect operations.
"""

import time
import unittest

from app.config import settings
from app.integration import (
    CommonMessageRequest,
    common_message_processor,
    session_manager,
)
from app.integration.manager import (
    EnterpriseIntegrationManager,
    RECENT_ACTIVITIES,
    integration_manager,
    mask_secret,
)


class TestFelixIntegrationsDashboard(unittest.TestCase):

    def setUp(self):
        session_manager.clear_all()
        RECENT_ACTIVITIES.clear()
        self._orig_slack_token = settings.SLACK_BOT_TOKEN
        self._orig_slack_secret = settings.SLACK_SIGNING_SECRET
        self._orig_slack_enabled = settings.SLACK_ENABLED
        self._orig_slack_ws_name = getattr(settings, "SLACK_WORKSPACE_NAME", "")
        self._orig_teams_id = settings.TEAMS_APP_ID
        self._orig_teams_pwd = settings.TEAMS_APP_PASSWORD
        self._orig_teams_enabled = settings.TEAMS_ENABLED

    def tearDown(self):
        from app.config import save_settings_to_env
        settings.SLACK_BOT_TOKEN = self._orig_slack_token
        settings.SLACK_SIGNING_SECRET = self._orig_slack_secret
        settings.SLACK_ENABLED = self._orig_slack_enabled
        if hasattr(settings, "SLACK_WORKSPACE_NAME"):
            settings.SLACK_WORKSPACE_NAME = self._orig_slack_ws_name
        settings.TEAMS_APP_ID = self._orig_teams_id
        settings.TEAMS_APP_PASSWORD = self._orig_teams_pwd
        settings.TEAMS_ENABLED = self._orig_teams_enabled
        save_settings_to_env({
            "SLACK_BOT_TOKEN": self._orig_slack_token,
            "SLACK_SIGNING_SECRET": self._orig_slack_secret,
            "SLACK_ENABLED": self._orig_slack_enabled,
            "TEAMS_APP_ID": self._orig_teams_id,
            "TEAMS_APP_PASSWORD": self._orig_teams_pwd,
            "TEAMS_ENABLED": self._orig_teams_enabled,
        })
        RECENT_ACTIVITIES.clear()

    def test_01_secret_masking(self):
        """Verify secrets are masked and raw values are never returned."""
        token = "xoxb-mocktoken-unit-test-secret-3456"
        masked = mask_secret(token, prefix_len=5, suffix_len=4)
        self.assertTrue(masked.startswith("xoxb-"))
        self.assertTrue(masked.endswith("3456"))
        self.assertIn("••••••••••••", masked)
        self.assertNotIn("mocktoken", masked)

        # Empty / None handling
        self.assertEqual(mask_secret(None), "")
        self.assertEqual(mask_secret(""), "")

    def test_02_status_computation(self):
        """Verify status displays 'Configuration Required', 'Connected', or 'Disconnected' dynamically."""
        mgr = EnterpriseIntegrationManager()

        # Save original settings
        orig_slack_token = settings.SLACK_BOT_TOKEN
        orig_slack_secret = settings.SLACK_SIGNING_SECRET
        orig_slack_enabled = settings.SLACK_ENABLED

        try:
            # 1. Configuration Required when secrets are missing
            settings.SLACK_BOT_TOKEN = ""
            settings.SLACK_SIGNING_SECRET = ""
            overview = mgr.get_overview()
            self.assertEqual(overview["platforms"]["slack"]["status"], "configuration_required")
            self.assertEqual(overview["platforms"]["slack"]["status_label"], "Configuration Required")

            # 2. Connected when credentials are configured and enabled
            settings.SLACK_BOT_TOKEN = "xoxb-mock-bot-token-for-test"
            settings.SLACK_SIGNING_SECRET = "signingsecret_test_32chars_long"
            settings.SLACK_ENABLED = True
            overview = mgr.get_overview()
            self.assertEqual(overview["platforms"]["slack"]["status"], "connected")
            self.assertEqual(overview["platforms"]["slack"]["status_label"], "Connected")

            # 3. Disconnected when credentials are configured but disabled
            settings.SLACK_ENABLED = False
            overview = mgr.get_overview()
            self.assertEqual(overview["platforms"]["slack"]["status"], "disconnected")
            self.assertEqual(overview["platforms"]["slack"]["status_label"], "Disconnected")
        finally:
            settings.SLACK_BOT_TOKEN = orig_slack_token
            settings.SLACK_SIGNING_SECRET = orig_slack_secret
            settings.SLACK_ENABLED = orig_slack_enabled

    def test_03_configuration_validation(self):
        """Verify validation prevents invalid credentials and preserves existing secrets on mask."""
        mgr = EnterpriseIntegrationManager()

        orig_token = settings.SLACK_BOT_TOKEN
        try:
            # Invalid token prefix
            ok, err = mgr.update_slack_configuration(bot_token="invalid_prefix_token_123")
            self.assertFalse(ok)
            self.assertIn("xoxb-", err)

            # Valid token
            ok, msg = mgr.update_slack_configuration(bot_token="xoxb-test-valid-bot-token-9999")
            self.assertTrue(ok)
            self.assertEqual(settings.SLACK_BOT_TOKEN, "xoxb-test-valid-bot-token-9999")

            # Masked input does NOT overwrite existing secret
            ok, msg = mgr.update_slack_configuration(
                bot_token="xoxb-••••••••••••9999",
                workspace_name="Updated Acme Workspace"
            )
            self.assertTrue(ok)
            self.assertEqual(settings.SLACK_BOT_TOKEN, "xoxb-test-valid-bot-token-9999")
            self.assertEqual(settings.SLACK_WORKSPACE_NAME, "Updated Acme Workspace")
        finally:
            settings.SLACK_BOT_TOKEN = orig_token

    def test_04_activity_recording_and_filtering(self):
        """Verify activity logs are recorded on execution and queryable with filters."""
        mgr = EnterpriseIntegrationManager()

        # Record activities across platforms
        mgr.record_activity(
            platform="slack",
            query="Why has approval rate dropped?",
            execution_status="completed",
            execution_duration_ms=120.5,
            workspace_name="Slack Ops",
            user_name="Alice",
            conversation_id="C_PAYMENTS",
            tools_called=["run_sql"],
            findings_count=1,
        )

        mgr.record_activity(
            platform="teams",
            query="Which issuer banks are affected?",
            execution_status="completed",
            execution_duration_ms=95.0,
            workspace_name="Teams Finance",
            user_name="Bob",
            conversation_id="19:channel@thread.tacv2",
            tools_called=["run_sql", "get_baseline"],
            findings_count=2,
        )

        mgr.record_activity(
            platform="slack",
            query="",
            execution_status="error",
            execution_duration_ms=5.0,
            workspace_name="Slack Ops",
            user_name="Charlie",
            conversation_id="C_PAYMENTS",
            error_message="Message body cannot be empty.",
        )

        # 1. Filter by platform
        slack_acts = mgr.get_activities(platform="slack", limit=10)
        self.assertGreaterEqual(slack_acts["total"], 2)
        for act in slack_acts["activities"]:
            self.assertEqual(act["platform"], "slack")

        # 2. Filter by status
        error_acts = mgr.get_activities(status="error", limit=10)
        self.assertGreaterEqual(error_acts["total"], 1)
        for act in error_acts["activities"]:
            self.assertEqual(act["execution_status"], "error")

        # 3. Search query
        search_acts = mgr.get_activities(search="approval rate", limit=10)
        self.assertGreaterEqual(search_acts["total"], 1)
        self.assertIn("approval rate", search_acts["activities"][0]["query"].lower())

    def test_05_platform_connection_test(self):
        """Verify connection test diagnostics identify missing or present configuration."""
        mgr = EnterpriseIntegrationManager()

        orig_token = settings.SLACK_BOT_TOKEN
        orig_secret = settings.SLACK_SIGNING_SECRET

        try:
            # Without credentials -> configuration_required
            settings.SLACK_BOT_TOKEN = ""
            settings.SLACK_SIGNING_SECRET = ""
            res = mgr.test_connection("slack")
            self.assertFalse(res["success"])
            self.assertEqual(res["status"], "configuration_required")

            # With valid credentials -> connected
            settings.SLACK_BOT_TOKEN = "xoxb-valid-test-token-1234"
            settings.SLACK_SIGNING_SECRET = "signingsecret_32chars_test_key_ok"
            res = mgr.test_connection("slack")
            self.assertTrue(res["success"])
            self.assertEqual(res["status"], "connected")
        finally:
            settings.SLACK_BOT_TOKEN = orig_token
            settings.SLACK_SIGNING_SECRET = orig_secret

    def test_06_toggle_and_disconnect(self):
        """Verify platform enable, disable, and disconnect actions update state."""
        mgr = EnterpriseIntegrationManager()

        orig_enabled = settings.SLACK_ENABLED
        try:
            # Disconnect
            ok, msg = mgr.disconnect_platform("slack")
            self.assertTrue(ok)
            self.assertFalse(settings.SLACK_ENABLED)

            # Re-enable
            ok, msg = mgr.toggle_platform("slack", enabled=True)
            self.assertTrue(ok)
            self.assertTrue(settings.SLACK_ENABLED)
        finally:
            settings.SLACK_ENABLED = orig_enabled


if __name__ == "__main__":
    unittest.main()
