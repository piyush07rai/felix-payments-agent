"""Automated Verification Suite for Phase 6: Enterprise Security, Access Control & Reliability.

Verifies:
1. Slack authentication: HMAC-SHA256 signature verification & 300s replay prevention.
2. Microsoft Teams security: Bot Framework Bearer auth header enforcement.
3. Access Control (RBAC): Organisation Administrator vs Standard User permissions.
4. Data & Organisation Isolation: Strict separation between enterprise tenants.
5. Agent Safety: SQL read-only guardrails, destructive query rejection & Zero-PII deny-lists.
6. Enterprise Audit Logging: Full security lifecycle audit with secret scrubbing.
7. Human Approval Framework: Advisory action proposals, admin approval requirement & execution locking.
"""

import time
import unittest

from app.config import settings
from app.integration import (
    CommonMessageRequest,
    common_message_processor,
    session_manager,
)
from app.security.approval import ApprovalStatus, ActionApprovalStore
from app.security.audit import SecurityAuditLogger
from app.security.auth import (
    UserRole,
    get_user_role,
    is_admin,
    verify_admin_access,
    verify_organization_isolation,
)
from app.slack.security import compute_slack_signature, verify_slack_signature
from app.teams.security import validate_teams_activity
from app.tools import run_sql


class TestFelixEnterpriseSecurity(unittest.TestCase):

    def setUp(self):
        session_manager.clear_all()
        self.store = ActionApprovalStore()
        self.auditor = SecurityAuditLogger()

    # -------------------------------------------------------------------------
    # 1. Slack Authentication & Replay Attack Prevention
    # -------------------------------------------------------------------------
    def test_01_slack_authentication_and_replay_prevention(self):
        """Verify HMAC-SHA256 signature check and rejection of replayed/expired requests (>300s)."""
        secret = "corp_signing_secret_998811"
        body = b'{"type":"event_callback","event":{"type":"app_mention","text":"hi"}}'
        now_ts = str(int(time.time()))

        # Valid signature
        valid_sig = compute_slack_signature(secret, now_ts, body.decode("utf-8"))
        is_ok, err = verify_slack_signature(secret, now_ts, body, valid_sig)
        self.assertTrue(is_ok)
        self.assertEqual(err, "")

        # Tampered body signature mismatch
        tampered_sig = valid_sig[:-3] + "abc"
        is_ok, err = verify_slack_signature(secret, now_ts, body, tampered_sig)
        self.assertFalse(is_ok)
        self.assertIn("mismatch", err.lower())

        # Replay attack (> 300 seconds old)
        replayed_ts = str(int(time.time()) - 350)
        replayed_sig = compute_slack_signature(secret, replayed_ts, body.decode("utf-8"))
        is_ok, err = verify_slack_signature(secret, replayed_ts, body, replayed_sig)
        self.assertFalse(is_ok)
        self.assertIn("out of bounds", err.lower())

    # -------------------------------------------------------------------------
    # 2. Microsoft Teams Security & Bot Framework Bearer Auth
    # -------------------------------------------------------------------------
    def test_02_teams_activity_and_bearer_auth(self):
        """Verify Bot Framework Bearer authentication validation and activity structure checks."""
        valid_activity = {
            "type": "message",
            "conversation": {"id": "19:ch@thread.tacv2", "tenantId": "org_tenant_1"},
            "from": {"id": "29:usr1"},
            "text": "Hello Felix"
        }

        # Structure validation
        is_valid, _ = validate_teams_activity(valid_activity, auth_header=None)
        self.assertTrue(is_valid)

        # Missing routing target
        bad_activity = {"type": "message", "conversation": {}}
        is_valid, err = validate_teams_activity(bad_activity, auth_header=None)
        self.assertFalse(is_valid)
        self.assertIn("conversation.id", err)

        # Production Bearer token enforcement when TEAMS_APP_ID is active
        orig_id = settings.TEAMS_APP_ID
        try:
            settings.TEAMS_APP_ID = "mock-bot-app-guid-7777"
            # Unauthenticated request -> blocked
            is_valid, err = validate_teams_activity(valid_activity, auth_header=None)
            self.assertFalse(is_valid)
            self.assertIn("Missing Authorization Bearer token", err)

            # Valid Bearer token -> accepted
            is_valid, _ = validate_teams_activity(valid_activity, auth_header="Bearer eyJhbGciOi...")
            self.assertTrue(is_valid)
        finally:
            settings.TEAMS_APP_ID = orig_id

    # -------------------------------------------------------------------------
    # 3. Access Control (RBAC): Organisation Administrator vs Standard User
    # -------------------------------------------------------------------------
    def test_03_role_based_access_control(self):
        """Verify Administrator vs Standard User role resolution and permission barriers."""
        # 1. Standard user by default
        role = get_user_role(role_header="user", user_id="regular_employee")
        self.assertEqual(role, UserRole.USER)
        self.assertFalse(is_admin(role_header="user", user_id="regular_employee"))

        # 2. Administrator role via X-User-Role header
        admin_role = get_user_role(role_header="admin", user_id="regular_employee")
        self.assertEqual(admin_role, UserRole.ADMIN)
        self.assertTrue(is_admin(role_header="admin"))

        # 3. Administrator role via X-Admin-Key
        key_role = get_user_role(admin_key_header="felix-admin-key-2026")
        self.assertEqual(key_role, UserRole.ADMIN)

        # 4. verify_admin_access barrier
        # Standard user is blocked
        with self.assertRaises(Exception):
            verify_admin_access(x_user_role="user")

        # Administrator passes without exception
        try:
            verify_admin_access(x_user_role="admin")
        except Exception:
            self.fail("verify_admin_access raised an exception for Administrator role.")

    # -------------------------------------------------------------------------
    # 4. Organisation & Tenant Boundary Isolation
    # -------------------------------------------------------------------------
    def test_04_organisation_boundary_isolation(self):
        """Verify organization boundary isolation prevents cross-tenant access."""
        # Same organization -> allowed
        self.assertTrue(verify_organization_isolation("org_acme", "org_acme"))

        # Different organizations -> strictly blocked
        self.assertFalse(verify_organization_isolation("org_alpha", "org_beta"))

        # Session keys are isolated across tenants
        req_alpha = CommonMessageRequest(
            platform="teams",
            workspace_id="tenant_alpha",
            user_id="user_1",
            conversation_id="conv_1",
            message="Check authorization rate"
        )
        req_beta = CommonMessageRequest(
            platform="teams",
            workspace_id="tenant_beta",
            user_id="user_1",
            conversation_id="conv_1",
            message="Check authorization rate"
        )
        sess_alpha = session_manager.get_or_create_session(req_alpha)
        sess_beta = session_manager.get_or_create_session(req_beta)
        self.assertNotEqual(sess_alpha.session_id, sess_beta.session_id)

    # -------------------------------------------------------------------------
    # 5. Agent Safety & Read-Only SQL Guardrails
    # -------------------------------------------------------------------------
    def test_05_agent_safety_and_readonly_sql(self):
        """Verify queries attempting destructive SQL or accessing blocked PII columns fail."""
        # 1. Block destructive operations (DELETE / UPDATE / DROP)
        res_delete = run_sql("DELETE FROM transactions WHERE amount > 100")
        self.assertFalse(res_delete["success"])
        self.assertIn("blocked", res_delete["error"].lower())

        res_drop = run_sql("DROP TABLE transactions")
        self.assertFalse(res_drop["success"])

        res_update = run_sql("UPDATE transactions SET amount = 0")
        self.assertFalse(res_update["success"])

        # 2. Block columns on enterprise PII deny-list (cvv, password, ssn)
        res_cvv = run_sql("SELECT cvv FROM transactions LIMIT 1")
        self.assertFalse(res_cvv["success"])
        self.assertIn("deny-list", res_cvv["error"].lower())

        res_pwd = run_sql("SELECT password FROM transactions LIMIT 1")
        self.assertFalse(res_pwd["success"])

        # 3. Legitimate read-only SELECT query succeeds
        res_ok = run_sql("SELECT count(*) as total FROM transactions")
        self.assertTrue(res_ok["success"])

    # -------------------------------------------------------------------------
    # 6. Enterprise Audit Logging & Secret Scrubbing
    # -------------------------------------------------------------------------
    def test_06_security_audit_logging_and_sanitization(self):
        """Verify security events are logged with delivery status and secrets are scrubbed."""
        evt = self.auditor.log_event(
            event_type="AUTH_CHECK",
            execution_status="SUCCESS",
            org_id="corp_fintech_1",
            platform="slack",
            user_id="U_ALICE",
            user_role="admin",
            execution_duration_ms=45.2,
            delivery_status="DELIVERED",
            details={
                "action": "login",
                "bot_token": "xoxb-real-secret-token",
                "api_key": "gemini-secret-api-key",
                "safe_field": "workspace_setup"
            }
        )

        self.assertEqual(evt["event_type"], "AUTH_CHECK")
        self.assertEqual(evt["delivery_status"], "DELIVERED")
        self.assertEqual(evt["sanitized_details"]["safe_field"], "workspace_setup")
        # Ensure raw secrets are redacted
        self.assertEqual(evt["sanitized_details"]["bot_token"], "[REDACTED_SECURITY_SECRET]")
        self.assertEqual(evt["sanitized_details"]["api_key"], "[REDACTED_SECURITY_SECRET]")

    # -------------------------------------------------------------------------
    # 7. Human Approval Framework & Execution Lock
    # -------------------------------------------------------------------------
    def test_07_human_approval_framework(self):
        """Verify advisory proposals require human approval and execution is permanently locked."""
        # 1. Create action proposal
        proposal = self.store.create_proposal(
            title="Reroute RuPay High-Value MCC 5732",
            recommended_action="Switch acquirer routing from Gateway A to Gateway B for transactions > ₹25,000",
            problem="High RuPay declines in MCC 5732",
            evidence="Decline code 91 rate at 42%",
            expected_impact="+4.1% Approval Rate Uplift",
            org_id="enterprise_alpha",
            created_by="Felix Analyst",
        )

        self.assertEqual(proposal["status"], ApprovalStatus.PENDING_HUMAN_APPROVAL.value)
        self.assertTrue(proposal["execution_locked"])
        self.assertIn("HUMAN APPROVAL REQUIRED", proposal["governance_note"])

        prop_id = proposal["proposal_id"]

        # 2. Standard user cannot approve
        ok, err, _ = self.store.review_proposal(
            proposal_id=prop_id,
            decision="approve",
            reviewer_id="standard_user_1",
            reviewer_role="user"
        )
        self.assertFalse(ok)
        self.assertIn("Access Denied", err)

        # 3. Organisation Administrator approves
        ok, msg, updated = self.store.review_proposal(
            proposal_id=prop_id,
            decision="approve",
            reviewer_id="admin_user_99",
            reviewer_role="admin"
        )
        self.assertTrue(ok)
        self.assertEqual(updated["status"], ApprovalStatus.APPROVED.value)
        self.assertEqual(updated["reviewed_by"], "admin_user_99")
        # Execution remains locked (advisory only - requires engineering change deployment)
        self.assertTrue(updated["execution_locked"])


if __name__ == "__main__":
    unittest.main()
