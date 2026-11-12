"""Human Approval Framework for Felix (Phase 6).

Implements governance safeguards for operational recommendations:
1. Felix is strictly an advisory analyst and NEVER directly modifies production payment infrastructure.
2. Every remediation action drafted by Felix is registered as an ActionProposal in 'PENDING_HUMAN_APPROVAL' status.
3. Actual approval/rejection requires explicit Human-in-the-Loop authorization by an Organisation Administrator.
4. Execution remains permanently locked ('execution_locked: true') with immutable audit records.
"""

import datetime
import enum
import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.database import get_connection, init_database

logger = logging.getLogger("app.security.approval")


class ApprovalStatus(str, enum.Enum):
    """Lifecycle statuses for human-in-the-loop payment action proposals."""
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ActionApprovalStore:
    """Thread-safe store for human approval proposals with SQLite persistence."""

    def __init__(self):
        self._memory_cache: List[Dict[str, Any]] = []
        try:
            init_database()
        except Exception:
            pass

    def create_proposal(
        self,
        title: str,
        recommended_action: str,
        problem: Optional[str] = None,
        evidence: Optional[str] = None,
        likely_cause: Optional[str] = None,
        expected_impact: Optional[str] = None,
        action_type: Optional[str] = None,
        org_id: Optional[str] = None,
        created_by: Optional[str] = None,
        suggested_owner: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new action proposal requiring human administrator review."""
        proposal_id = f"prop_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        proposal = {
            "proposal_id": proposal_id,
            "created_at": now_iso,
            "org_id": org_id or "default_org",
            "created_by": created_by or "Felix Analyst Agent",
            "title": title or "Recommended Payment Action",
            "action_type": action_type or "routing_change",
            "problem": problem or "",
            "evidence": evidence or "",
            "likely_cause": likely_cause or "",
            "expected_impact": expected_impact or "Approval rate optimization",
            "recommended_action": recommended_action,
            "suggested_owner": suggested_owner or "Payments Engineering",
            "priority": priority or "P1 - High",
            "status": ApprovalStatus.PENDING_HUMAN_APPROVAL.value,
            "reviewed_by": None,
            "reviewed_at": None,
            "rejection_reason": None,
            "execution_locked": True,
            "governance_note": (
                "⚠️ HUMAN APPROVAL REQUIRED: Felix can formulate and recommend payment optimizations, "
                "but cannot directly modify production infrastructure. Production execution requires "
                "manual verification and deployment through standard engineering change management."
            ),
        }

        # 1. Update in-memory cache
        self._memory_cache.insert(0, proposal)

        # 2. Persist in SQLite
        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO action_proposals (
                    proposal_id, created_at, org_id, created_by, title,
                    action_type, problem, evidence, likely_cause,
                    expected_impact, recommended_action, suggested_owner,
                    priority, status, execution_locked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal["proposal_id"],
                    proposal["created_at"],
                    proposal["org_id"],
                    proposal["created_by"],
                    proposal["title"],
                    proposal["action_type"],
                    proposal["problem"],
                    proposal["evidence"],
                    proposal["likely_cause"],
                    proposal["expected_impact"],
                    proposal["recommended_action"],
                    proposal["suggested_owner"],
                    proposal["priority"],
                    proposal["status"],
                    1,
                ),
            )
            conn.commit()
            logger.info("Registered Action Proposal %s in PENDING status", proposal_id)
        except Exception as exc:
            logger.warning("Could not persist action proposal to SQLite: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return proposal

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Fetch an action proposal by ID."""
        conn = None
        try:
            conn = get_connection(read_only=True)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM action_proposals WHERE proposal_id = ?", (proposal_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        for p in self._memory_cache:
            if p["proposal_id"] == proposal_id:
                return p
        return None

    def list_proposals(
        self,
        org_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List proposals with optional status and organization filtering."""
        proposals: List[Dict[str, Any]] = []
        conn = None
        try:
            conn = get_connection(read_only=True)
            cursor = conn.cursor()
            query = "SELECT * FROM action_proposals WHERE 1=1"
            params: List[Any] = []

            if org_id and org_id.lower() != "all":
                query += " AND org_id = ?"
                params.append(org_id)

            if status and status.lower() != "all":
                query += " AND status = ?"
                params.append(status.upper())

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            for row in cursor.fetchall():
                proposals.append(dict(row))
        except Exception as exc:
            logger.warning("Falling back to in-memory proposal list: %s", exc)
            filtered = list(self._memory_cache)
            if org_id and org_id.lower() != "all":
                filtered = [p for p in filtered if p.get("org_id") == org_id]
            if status and status.lower() != "all":
                filtered = [p for p in filtered if p.get("status") == status.upper()]
            proposals = filtered[:limit]
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return proposals

    def review_proposal(
        self,
        proposal_id: str,
        decision: str,
        reviewer_id: str,
        reviewer_role: str,
        rejection_reason: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Perform Human-in-the-Loop review of a pending proposal.
        
        Requires Organisation Administrator authorization.
        """
        # 1. Authorisation enforcement
        if reviewer_role.lower() not in ("admin", "administrator"):
            logger.warning(
                "Unauthorized proposal review attempt: user '%s' with role '%s' attempted to %s proposal '%s'",
                reviewer_id, reviewer_role, decision, proposal_id
            )
            return False, "Access Denied: Only Organisation Administrators can approve or reject proposals.", None

        normalized_decision = decision.strip().lower()
        if normalized_decision in ("approve", "approved"):
            target_status = ApprovalStatus.APPROVED.value
        elif normalized_decision in ("reject", "rejected"):
            target_status = ApprovalStatus.REJECTED.value
        else:
            return False, f"Invalid decision '{decision}'. Must be 'approve' or 'reject'.", None

        proposal = self.get_proposal(proposal_id)
        if not proposal:
            return False, f"Proposal '{proposal_id}' not found.", None

        if proposal["status"] != ApprovalStatus.PENDING_HUMAN_APPROVAL.value:
            return False, f"Proposal has already been reviewed (Current status: {proposal['status']}).", proposal

        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Update in SQLite
        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE action_proposals
                SET status = ?, reviewed_by = ?, reviewed_at = ?, rejection_reason = ?
                WHERE proposal_id = ?
                """,
                (target_status, reviewer_id, now_iso, rejection_reason, proposal_id),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Could not update proposal in SQLite: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # Update in-memory cache
        for p in self._memory_cache:
            if p["proposal_id"] == proposal_id:
                p["status"] = target_status
                p["reviewed_by"] = reviewer_id
                p["reviewed_at"] = now_iso
                p["rejection_reason"] = rejection_reason

        updated_proposal = self.get_proposal(proposal_id)
        logger.info(
            "Proposal %s %s by administrator %s",
            proposal_id, target_status, reviewer_id
        )
        return True, f"Proposal successfully {target_status.lower()}.", updated_proposal


# Singleton action approval store
approval_store = ActionApprovalStore()
