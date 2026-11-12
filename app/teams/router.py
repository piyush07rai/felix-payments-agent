"""FastAPI Router for Microsoft Teams Bot Framework and Diagnostic Simulation.

Provides:
- Webhook messaging endpoint (`POST /api/teams/messages`).
- Diagnostic simulator (`POST /api/teams/simulate`) for credential-free testing.
- Status and configuration overview (`GET /api/teams/status`).
- Downloadable Teams App Manifest (`GET /api/teams/manifest`).
"""

import json
import logging
import time
from typing import Any, Dict, Optional

try:
    from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:
    APIRouter = None
    BackgroundTasks = None
    Header = lambda *args, **kwargs: None
    HTTPException = Exception
    Request = None
    Response = None
    status = None
    BaseModel = object
    Field = lambda *args, **kwargs: None
    FASTAPI_AVAILABLE = False

from app.config import settings
from app.teams.client import SENT_TEAMS_ACTIVITIES, teams_client
from app.teams.security import teams_deduplicator, validate_teams_activity
from app.teams.service import teams_service

logger = logging.getLogger("app.teams.router")

if FASTAPI_AVAILABLE:
    router = APIRouter(prefix="/api/teams", tags=["Microsoft Teams Integration"])
else:
    class DummyRouter:
        def post(self, *args, **kwargs):
            return lambda fn: fn
        def get(self, *args, **kwargs):
            return lambda fn: fn
    router = DummyRouter()


class TeamsSimulateRequest(BaseModel):
    """Payload to simulate an incoming Microsoft Teams mention."""
    text: str = Field(..., description="Message text including mention (e.g. '<at>Felix</at> Why has our RuPay approval rate dropped?')")
    conversation_id: Optional[str] = Field("19:payments_channel_id@thread.tacv2", description="Simulated conversation ID")
    user_name: Optional[str] = Field("Jane Doe", description="Display name of user")
    user_id: Optional[str] = Field("29:user_guid_123", description="Simulated user ID")
    tenant_id: Optional[str] = Field("72f988bf-86f1-41af-91ab-2d7cd011db47", description="Simulated Microsoft Tenant ID")


@router.post("/messages")
async def handle_teams_messages(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Main Microsoft Bot Framework messaging endpoint.

    Validates incoming activities, enforces tenant authentication, deduplicates
    network retries, and asynchronously executes the Felix investigation.
    """
    raw_body = await request.body()
    try:
        body_str = raw_body.decode("utf-8")
        activity = json.loads(body_str) if body_str else {}
    except Exception as exc:
        logger.warning("Failed to parse Teams activity body: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.")

    # 1. Validate activity structure and Microsoft authorization header
    is_valid, err_msg = validate_teams_activity(activity, auth_header=authorization)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=err_msg)

    # 2. Prevent duplicate execution of retried activities
    activity_id = activity.get("id")
    if teams_deduplicator.is_duplicate(activity_id):
        logger.info("Skipping duplicate Teams activity retry for id: %s", activity_id)
        return {"status": "duplicate_skipped"}

    # 3. Schedule asynchronous background processing to reply within Bot Framework timeout
    background_tasks.add_task(teams_service.handle_activity, activity)

    return {"status": "accepted"}


@router.post("/simulate")
async def simulate_teams_mention(req: TeamsSimulateRequest):
    """Diagnostic endpoint to simulate an incoming Microsoft Teams @Felix mention.

    Allows testing the complete Teams pipeline (mention extraction, Zero-PII decoupling,
    tool execution, and Adaptive Card generation) without Microsoft Bot Framework credentials.
    """
    now_ts = f"{time.time():.6f}"
    mock_activity = {
        "type": "message",
        "id": f"teams_msg_{int(time.time() * 1000)}",
        "timestamp": now_ts,
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "channelId": "msteams",
        "from": {
            "id": req.user_id,
            "name": req.user_name,
        },
        "conversation": {
            "isGroup": True,
            "conversationType": "channel",
            "id": req.conversation_id,
            "tenantId": req.tenant_id,
        },
        "recipient": {
            "id": "28:bot_felix_app_id",
            "name": "Felix",
        },
        "text": req.text,
        "channelData": {
            "teamsChannelId": req.conversation_id,
            "tenant": {"id": req.tenant_id},
        }
    }

    resp = await teams_service.handle_activity(mock_activity)
    recent_sent = list(SENT_TEAMS_ACTIVITIES)[-2:] if SENT_TEAMS_ACTIVITIES else []

    return {
        "success": True,
        "simulated_query": req.text,
        "extracted_query": teams_service.extract_query_text(req.text),
        "session_id": resp.session_id if resp else None,
        "turn_index": resp.turn_index if resp else None,
        "response_text": resp.response_text if resp else None,
        "tools_executed": [t.get("tool") for t in (resp.tool_activity if resp else [])],
        "findings": resp.findings if resp else [],
        "adaptive_card_preview": resp.platform_payload.get("teams", {}).get("adaptive_card") if resp else {},
        "sent_activities_log": recent_sent
    }


@router.get("/status")
async def get_teams_integration_status():
    """Return status of the Microsoft Teams bot integration and client configuration."""
    return {
        "bot_name": "Felix",
        "configured": teams_client.is_configured,
        "mode": "live" if teams_client.is_configured else "simulation",
        "app_id_configured": bool(settings.TEAMS_APP_ID),
        "tenant_id": settings.TEAMS_TENANT_ID or "multi-tenant",
        "messaging_endpoint": "/api/teams/messages",
        "supported_card_version": "AdaptiveCard 1.5",
        "total_sent_activities": len(SENT_TEAMS_ACTIVITIES),
        "recent_activities": list(SENT_TEAMS_ACTIVITIES)[-10:]
    }


@router.get("/manifest")
async def get_teams_manifest():
    """Return the official Microsoft Teams App Manifest JSON."""
    from app.teams.manifest import TEAMS_APP_MANIFEST
    return TEAMS_APP_MANIFEST
