"""FastAPI Router for Slack Events API and Diagnostic Simulation.

Provides:
- Webhook ingress (`POST /api/slack/events`) with URL verification and signature checks.
- Diagnostic simulator (`POST /api/slack/simulate`) for credential-free testing.
- Status and configuration overview (`GET /api/slack/status`).
- Downloadable Slack App Manifest (`GET /api/slack/manifest`).
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

from app.audit_logging import log_lifecycle_event
from app.config import settings
from app.slack.client import SENT_SLACK_MESSAGES, slack_client
from app.slack.security import slack_deduplicator, verify_slack_signature
from app.slack.service import slack_service

logger = logging.getLogger("app.slack.router")

if FASTAPI_AVAILABLE:
    router = APIRouter(prefix="/api/slack", tags=["Slack Integration"])
else:
    class DummyRouter:
        def post(self, *args, **kwargs):
            return lambda fn: fn
        def get(self, *args, **kwargs):
            return lambda fn: fn
    router = DummyRouter()


class SlackSimulateRequest(BaseModel):
    """Payload to simulate an incoming Slack @Felix mention."""
    text: str = Field(..., description="Message text including mention (e.g. '@Felix why did approval rate drop?')")
    channel: Optional[str] = Field("C_PAYMENTS_INCIDENTS", description="Simulated channel ID")
    user: Optional[str] = Field("U_PAYMENTS_PM", description="Simulated user ID")
    team_id: Optional[str] = Field("T_ENTERPRISE_ORG", description="Simulated workspace team ID")
    thread_ts: Optional[str] = Field(None, description="Optional thread timestamp for multi-turn testing")


@router.post("/events")
async def handle_slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_signature: Optional[str] = Header(None, alias="X-Slack-Signature"),
    x_slack_retry_num: Optional[str] = Header(None, alias="X-Slack-Retry-Num"),
    x_slack_retry_reason: Optional[str] = Header(None, alias="X-Slack-Retry-Reason"),
):
    """Main Slack Events API webhook endpoint.

    Handles URL verification challenges, HMAC-SHA256 signature verification,
    deduplication of Slack retries, and asynchronous background analysis.
    """
    raw_body = await request.body()
    try:
        body_str = raw_body.decode("utf-8")
        payload = json.loads(body_str) if body_str else {}
    except Exception as exc:
        logger.warning("Failed to parse Slack event body: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.")

    # 1. URL Verification Challenge (Used when adding webhook URL in Slack App settings)
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        logger.info("Responding to Slack URL verification challenge.")
        return {"challenge": challenge}

    # 2. Security: Verify HMAC-SHA256 Request Signature
    if settings.SLACK_SIGNING_SECRET and settings.SLACK_SIGNING_SECRET.strip():
        is_valid, err_msg = verify_slack_signature(
            signing_secret=settings.SLACK_SIGNING_SECRET,
            timestamp=x_slack_request_timestamp or "",
            body=raw_body,
            signature=x_slack_signature or "",
        )
        if not is_valid:
            logger.warning("Rejected unverified Slack request: %s", err_msg)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=err_msg)

    # 3. Deduplication: Check for duplicate event retries
    event_id = payload.get("event_id")
    if slack_deduplicator.is_duplicate(event_id):
        logger.info("Skipping duplicate Slack retry for event_id: %s (Retry #%s)", event_id, x_slack_retry_num)
        return {"ok": True, "duplicate": True}

    # 4. Process event in background task
    event_data = payload.get("event", {})
    event_type = event_data.get("type")

    # Ignore messages sent by bots to prevent infinite feedback loops
    if event_data.get("bot_id") or event_data.get("subtype") == "bot_message":
        return {"ok": True, "ignored": "bot_message"}

    if event_type in ("app_mention", "message"):
        log_lifecycle_event(
            "SLACK_INGRESS",
            f"Queued background processing for Slack {event_type}",
            {"user": event_data.get("user"), "channel": event_data.get("channel")}
        )
        # Schedule asynchronous processing to guarantee immediate HTTP 200 response
        background_tasks.add_task(slack_service.handle_mention_event, payload)

    return {"ok": True}


@router.post("/simulate")
async def simulate_slack_mention(req: SlackSimulateRequest):
    """Diagnostic endpoint to simulate an incoming Slack @Felix mention.

    Allows testing the complete Slack pipeline (mention stripping, Zero-PII decoupling,
    tool execution, and Block Kit response generation) without live Slack credentials.
    """
    now_ts = f"{time.time():.6f}"
    mock_payload = {
        "token": "simulated_token",
        "team_id": req.team_id,
        "api_app_id": "A_FELIX_SIM",
        "event_id": f"Ev_sim_{int(time.time() * 1000)}",
        "event_time": int(time.time()),
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "user": req.user,
            "text": req.text,
            "ts": now_ts,
            "channel": req.channel,
            "thread_ts": req.thread_ts or now_ts,
            "event_ts": now_ts,
        }
    }

    resp = await slack_service.handle_mention_event(mock_payload)
    recent_sent = list(SENT_SLACK_MESSAGES)[-2:] if SENT_SLACK_MESSAGES else []

    return {
        "success": True,
        "simulated_query": req.text,
        "extracted_query": slack_service.extract_query_text(req.text),
        "session_id": resp.session_id if resp else None,
        "turn_index": resp.turn_index if resp else None,
        "response_text": resp.response_text if resp else None,
        "tools_executed": [t.get("tool") for t in (resp.tool_activity if resp else [])],
        "findings": resp.findings if resp else [],
        "slack_blocks_preview": resp.platform_payload.get("slack", {}).get("blocks") if resp else [],
        "sent_messages_log": recent_sent
    }


@router.get("/status")
async def get_slack_integration_status():
    """Return status of the Slack app integration and client configuration."""
    return {
        "bot_name": "Felix",
        "configured": slack_client.is_configured,
        "mode": "live" if slack_client.is_configured else "simulation",
        "signing_secret_configured": bool(settings.SLACK_SIGNING_SECRET),
        "bot_token_configured": bool(settings.SLACK_BOT_TOKEN and settings.SLACK_BOT_TOKEN.startswith("xoxb-")),
        "bot_user_id": settings.SLACK_BOT_USER_ID,
        "webhook_endpoint": "/api/slack/events",
        "subscribed_events": ["app_mention", "message.im"],
        "total_sent_messages": len(SENT_SLACK_MESSAGES),
        "recent_messages": list(SENT_SLACK_MESSAGES)[-10:]
    }


@router.get("/manifest")
async def get_slack_manifest():
    """Return the official Slack App Manifest JSON ready for 1-click app creation."""
    from app.slack.manifest import SLACK_APP_MANIFEST
    return SLACK_APP_MANIFEST
