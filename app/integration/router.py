"""FastAPI Router for Enterprise Integrations Dashboard (Phase 5).

Provides endpoints to:
1. Fetch live integration overview (Slack & Teams status, message counts, last interactions).
2. Configure Slack and Microsoft Teams securely with backend secret persistence.
3. Test platform connectivity and validate credentials.
4. Enable, disable, or disconnect integrations.
5. Query the audit activity log with multi-criteria filtering.
6. Provide system health status.
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

from app.integration.manager import integration_manager
from app.security.auth import verify_admin_access
from app.security.audit import security_auditor

logger = logging.getLogger("app.integration.router")

if FASTAPI_AVAILABLE:
    router = APIRouter(prefix="/api/integrations", tags=["Enterprise Integrations Dashboard"])
else:
    class DummyRouter:
        def post(self, *args, **kwargs):
            return lambda fn: fn
        def get(self, *args, **kwargs):
            return lambda fn: fn
    router = DummyRouter()


class SlackConfigRequest(BaseModel):
    """Payload to update Slack App configuration."""
    app_id: Optional[str] = Field(None, description="Slack App ID (e.g. A01234567)")
    bot_token: Optional[str] = Field(None, description="Bot User OAuth Token (xoxb-...)")
    signing_secret: Optional[str] = Field(None, description="Slack App Signing Secret")
    workspace_id: Optional[str] = Field(None, description="Slack Workspace / Team ID (e.g. T01234567)")
    workspace_name: Optional[str] = Field(None, description="Organization or Workspace display name")
    bot_user_id: Optional[str] = Field(None, description="Bot User ID (e.g. U_FELIX)")
    enabled: Optional[bool] = Field(None, description="Whether Slack integration is enabled")


class TeamsConfigRequest(BaseModel):
    """Payload to update Microsoft Teams configuration."""
    app_id: Optional[str] = Field(None, description="Microsoft Application (Client) ID")
    app_password: Optional[str] = Field(None, description="Microsoft App Password / Client Secret")
    tenant_id: Optional[str] = Field(None, description="Microsoft 365 Tenant ID")
    organization_name: Optional[str] = Field(None, description="Enterprise organization display name")
    enabled: Optional[bool] = Field(None, description="Whether Teams integration is enabled")


class PlatformActionRequest(BaseModel):
    """Payload to test, toggle, or disconnect an integration."""
    platform: str = Field(..., description="Platform identifier: 'slack' or 'teams'")
    enabled: Optional[bool] = Field(None, description="Target enable status for toggle")


@router.get("/overview")
async def get_integrations_overview(request: Request = None):
    """Fetch complete live status, masked secrets, and message metrics for all integrations."""
    base_url = str(request.base_url).rstrip("/") if request else ""
    return integration_manager.get_overview(base_url=base_url)


@router.post("/slack/configure")
async def configure_slack(
    req: SlackConfigRequest,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Update Slack integration credentials and settings securely. Requires Organisation Administrator."""
    verify_admin_access(x_user_role=x_user_role, x_admin_key=x_admin_key, user_id=x_user_id)

    success, message = integration_manager.update_slack_configuration(
        app_id=req.app_id,
        bot_token=req.bot_token,
        signing_secret=req.signing_secret,
        workspace_id=req.workspace_id,
        workspace_name=req.workspace_name,
        bot_user_id=req.bot_user_id,
        enabled=req.enabled,
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)

    security_auditor.log_event(
        event_type="CONFIG_MODIFIED",
        execution_status="SUCCESS",
        platform="slack",
        user_id=x_user_id or "admin",
        user_role="admin",
        details={"action": "update_slack_configuration", "workspace": req.workspace_name},
    )

    overview = integration_manager.get_overview()
    return {
        "success": True,
        "message": message,
        "slack": overview["platforms"]["slack"],
    }


@router.post("/teams/configure")
async def configure_teams(
    req: TeamsConfigRequest,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Update Microsoft Teams integration credentials and settings securely. Requires Organisation Administrator."""
    verify_admin_access(x_user_role=x_user_role, x_admin_key=x_admin_key, user_id=x_user_id)

    success, message = integration_manager.update_teams_configuration(
        app_id=req.app_id,
        app_password=req.app_password,
        tenant_id=req.tenant_id,
        organization_name=req.organization_name,
        enabled=req.enabled,
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)

    security_auditor.log_event(
        event_type="CONFIG_MODIFIED",
        execution_status="SUCCESS",
        platform="teams",
        user_id=x_user_id or "admin",
        user_role="admin",
        details={"action": "update_teams_configuration", "org": req.organization_name},
    )

    overview = integration_manager.get_overview()
    return {
        "success": True,
        "message": message,
        "teams": overview["platforms"]["teams"],
    }


@router.post("/test")
async def test_integration_connection(req: PlatformActionRequest):
    """Validate credentials and test connection health for a platform."""
    result = integration_manager.test_connection(req.platform)
    return result


@router.post("/toggle")
async def toggle_integration(
    req: PlatformActionRequest,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Enable or disable a specific messaging integration. Requires Organisation Administrator."""
    verify_admin_access(x_user_role=x_user_role, x_admin_key=x_admin_key, user_id=x_user_id)

    target_state = True if req.enabled is None else bool(req.enabled)
    success, message = integration_manager.toggle_platform(req.platform, enabled=target_state)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    security_auditor.log_event(
        event_type="INTEGRATION_TOGGLED",
        execution_status="SUCCESS",
        platform=req.platform,
        user_id=x_user_id or "admin",
        user_role="admin",
        details={"platform": req.platform, "target_state": target_state},
    )

    overview = integration_manager.get_overview()
    return {
        "success": True,
        "message": message,
        "overview": overview,
    }


@router.post("/disconnect")
async def disconnect_integration(
    req: PlatformActionRequest,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Disconnect a messaging integration. Requires Organisation Administrator."""
    verify_admin_access(x_user_role=x_user_role, x_admin_key=x_admin_key, user_id=x_user_id)

    success, message = integration_manager.disconnect_platform(req.platform)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    security_auditor.log_event(
        event_type="INTEGRATION_DISCONNECTED",
        execution_status="SUCCESS",
        platform=req.platform,
        user_id=x_user_id or "admin",
        user_role="admin",
        details={"platform": req.platform},
    )

    overview = integration_manager.get_overview()
    return {
        "success": True,
        "message": message,
        "overview": overview,
    }


@router.get("/activity")
async def get_integration_activity(
    platform: Optional[str] = Query("all", description="Filter by platform: all, slack, teams, web"),
    status: Optional[str] = Query("all", description="Filter by status: all, completed, error"),
    search: Optional[str] = Query(None, description="Free-text search query or user/channel"),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """Query live integration activity log with multi-criteria filtering."""
    return integration_manager.get_activities(
        platform=platform,
        status=status,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/health")
async def get_integration_health():
    """Retrieve detailed health status of all enterprise integrations and subsystems."""
    overview = integration_manager.get_overview()
    return overview["health"]
