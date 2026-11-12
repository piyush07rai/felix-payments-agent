"""FastAPI Router for Enterprise Security, Governance & Human Approval (Phase 6).

Provides:
- Endpoints for Human-in-the-Loop Action Proposals (list, create, approve, reject).
- Endpoints for querying Enterprise Security Audit logs.
- Permission and role verification endpoints.
"""

import logging
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, Header, HTTPException, Query, Request, status
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:
    APIRouter = None
    Header = lambda *args, **kwargs: None
    HTTPException = Exception
    Query = lambda *args, **kwargs: None
    Request = None
    status = None
    BaseModel = object
    Field = lambda *args, **kwargs: None
    FASTAPI_AVAILABLE = False

from app.security.approval import approval_store
from app.security.audit import security_auditor
from app.security.auth import get_user_role, is_admin, verify_admin_access

logger = logging.getLogger("app.security.router")

if FASTAPI_AVAILABLE:
    router = APIRouter(tags=["Enterprise Security & Governance"])
else:
    class DummyRouter:
        def post(self, *args, **kwargs):
            return lambda fn: fn
        def get(self, *args, **kwargs):
            return lambda fn: fn
    router = DummyRouter()


class CreateProposalRequest(BaseModel):
    """Payload to create an action proposal."""
    title: str = Field(..., description="Action title (e.g. 'Reroute high-value RuPay MCC 5732 transactions')")
    recommended_action: str = Field(..., description="Proposed technical/operational change")
    problem: Optional[str] = Field(None, description="Decline anomaly description")
    evidence: Optional[str] = Field(None, description="Data evidence and drop metrics")
    likely_cause: Optional[str] = Field(None, description="Root cause hypothesis")
    expected_impact: Optional[str] = Field("+3.5% Approval Rate Uplift", description="Projected impact")
    action_type: Optional[str] = Field("routing_change", description="Category: routing_change, issuer_fallback, retry_policy")
    org_id: Optional[str] = Field("default_org", description="Organisation ID")
    suggested_owner: Optional[str] = Field("Payments Engineering", description="Team owner")
    priority: Optional[str] = Field("P1 - High", description="Priority level")


class ReviewProposalRequest(BaseModel):
    """Payload to review an action proposal (Human-in-the-Loop)."""
    decision: str = Field(..., description="'approve' or 'reject'")
    rejection_reason: Optional[str] = Field(None, description="Reason if rejected")


# -----------------------------------------------------------------------------
# Action Approval Endpoints
# -----------------------------------------------------------------------------

@router.get("/api/actions/proposals")
async def list_action_proposals(
    org_id: Optional[str] = Query("all", description="Filter by organisation ID"),
    status: Optional[str] = Query("all", description="Filter by status: all, PENDING_HUMAN_APPROVAL, APPROVED, REJECTED"),
    limit: int = Query(50, ge=1, le=100),
):
    """List action proposals requiring human administrator review."""
    return {
        "success": True,
        "proposals": approval_store.list_proposals(org_id=org_id, status=status, limit=limit),
    }


@router.post("/api/actions/proposals")
async def create_action_proposal(
    req: CreateProposalRequest,
    x_user_role: Optional[str] = Header("user", alias="X-User-Role"),
    x_user_id: Optional[str] = Header("anonymous", alias="X-User-Id"),
):
    """Register an action proposal drafted by Felix or an analyst."""
    proposal = approval_store.create_proposal(
        title=req.title,
        recommended_action=req.recommended_action,
        problem=req.problem,
        evidence=req.evidence,
        likely_cause=req.likely_cause,
        expected_impact=req.expected_impact,
        action_type=req.action_type,
        org_id=req.org_id,
        created_by=x_user_id,
        suggested_owner=req.suggested_owner,
        priority=req.priority,
    )

    security_auditor.log_event(
        event_type="PROPOSAL_CREATED",
        execution_status="SUCCESS",
        org_id=req.org_id,
        user_id=x_user_id,
        user_role=x_user_role,
        details={"proposal_id": proposal["proposal_id"], "title": req.title},
    )

    return {"success": True, "proposal": proposal}


@router.post("/api/actions/proposals/{proposal_id}/review")
async def review_action_proposal(
    proposal_id: str,
    req: ReviewProposalRequest,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header("admin", alias="X-User-Id"),
):
    """Human-in-the-Loop review of a proposal. Requires Organisation Administrator role."""
    # Enforce Administrator Authorization
    verify_admin_access(x_user_role=x_user_role, x_admin_key=x_admin_key, user_id=x_user_id)

    ok, message, proposal = approval_store.review_proposal(
        proposal_id=proposal_id,
        decision=req.decision,
        reviewer_id=x_user_id or "admin",
        reviewer_role=x_user_role or "admin",
        rejection_reason=req.rejection_reason,
    )

    if not ok:
        raise HTTPException(status_code=400, detail=message)

    security_auditor.log_event(
        event_type="PROPOSAL_REVIEWED",
        execution_status="SUCCESS",
        org_id=proposal.get("org_id") if proposal else "default_org",
        user_id=x_user_id,
        user_role="admin",
        details={
            "proposal_id": proposal_id,
            "decision": req.decision,
            "rejection_reason": req.rejection_reason,
        },
    )

    return {
        "success": True,
        "message": message,
        "proposal": proposal,
    }


# -----------------------------------------------------------------------------
# Security Audit & Permissions Endpoints
# -----------------------------------------------------------------------------

@router.get("/api/security/audit-log")
async def get_security_audit_log(
    org_id: Optional[str] = Query("all", description="Organisation filter"),
    event_type: Optional[str] = Query("all", description="Event type filter"),
    status: Optional[str] = Query("all", description="Status filter"),
    limit: int = Query(50, ge=1, le=200),
):
    """Retrieve immutable security audit trail."""
    return {
        "success": True,
        "logs": security_auditor.get_logs(
            org_id=org_id, event_type=event_type, status=status, limit=limit
        ),
    }


@router.get("/api/security/permissions")
async def check_user_permissions(
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Inspect the current user identity's permissions and role."""
    role = get_user_role(
        role_header=x_user_role, user_id=x_user_id, admin_key_header=x_admin_key
    )
    is_administrator = role == "admin"

    return {
        "role": role,
        "is_admin": is_administrator,
        "capabilities": {
            "can_query_felix": True,
            "can_view_findings": True,
            "can_view_integrations": True,
            "can_modify_integrations": is_administrator,
            "can_approve_proposals": is_administrator,
            "can_view_security_audit": True,
        },
    }
