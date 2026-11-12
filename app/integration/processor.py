"""Common Message Processing Service for Enterprise Felix.

Coordinates incoming messages across any platform (Slack, Teams, Web, API),
resolves isolated session state, executes the existing Felix agent with contextual history,
and formats unified responses.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.agent import analyst_agent
from app.integration.schemas import CommonMessageRequest, CommonMessageResponse
from app.integration.session import SessionManager, session_manager
from app.integration.response_handler import CommonResponseHandler
from app.integration.manager import integration_manager
from app.security.audit import security_auditor
from app.security.auth import get_user_role

logger = logging.getLogger("app.integration.processor")


class CommonMessageProcessor:
    """Core messaging pipeline processing normalized requests from any platform."""

    def __init__(self, agent=None, session_mgr: Optional[SessionManager] = None):
        self.agent = agent or analyst_agent
        self.session_mgr = session_mgr or session_manager

    def validate_request(self, req: CommonMessageRequest) -> Tuple[bool, str]:
        """Verify that the normalized request has all required enterprise fields."""
        if not req.platform or not str(req.platform).strip():
            return False, "Missing 'platform' in request (e.g. 'slack', 'teams', 'web')."
        if not req.workspace_id or not str(req.workspace_id).strip():
            return False, "Missing 'workspace_id' for enterprise tenant isolation."
        if not req.conversation_id or not str(req.conversation_id).strip():
            return False, "Missing 'conversation_id' (channel or direct message ID)."
        if not req.user_id or not str(req.user_id).strip():
            return False, "Missing 'user_id' for platform user tracking."
        if not req.message or not str(req.message).strip():
            return False, "Message body cannot be empty."
        return True, ""

    def process_message(self, req: CommonMessageRequest) -> CommonMessageResponse:
        """Synchronously process an incoming common message request through Felix."""
        start_time = time.time()
        logger.info(
            "Processing %s message for workspace '%s', channel '%s', user '%s'",
            req.platform, req.workspace_id, req.conversation_id, req.user_id
        )

        # 1. Request validation
        is_valid, validation_err = self.validate_request(req)
        if not is_valid:
            logger.warning("Invalid common message request: %s", validation_err)
            session = self.session_mgr.get_or_create_session(req)
            elapsed_ms = (time.time() - start_time) * 1000
            integration_manager.record_activity(
                platform=req.platform,
                query=req.message,
                execution_status="error",
                execution_duration_ms=elapsed_ms,
                workspace_id=req.workspace_id,
                user_id=req.user_id,
                user_name=req.user_name,
                conversation_id=req.conversation_id,
                error_message=validation_err,
            )
            security_auditor.log_event(
                event_type="QUERY_REJECTED",
                execution_status="DENIED",
                org_id=req.workspace_id,
                platform=req.platform,
                user_id=req.user_id,
                user_role=get_user_role(user_id=req.user_id),
                execution_duration_ms=elapsed_ms,
                delivery_status="DROPPED",
                details={"reason": validation_err},
            )
            return CommonResponseHandler.create_response(
                req=req,
                session=session,
                agent_result={"investigation_status": "error", "response": validation_err},
                execution_time_ms=elapsed_ms,
                turn_index=len(session.turns) + 1,
                error=validation_err
            )

        # 2. Session & thread resolution (Strict multi-tenant / channel isolation)
        session = self.session_mgr.get_or_create_session(req)

        # 3. Pull contextual conversation history (Enables follow-up questions)
        history = session.get_history(max_turns=6)

        # 4. Invoke existing Felix agent (Reuses existing tools & Gemini integration)
        try:
            agent_result = self.agent.run_chat(
                message=req.message.strip(),
                history=history,
                session_id=session.session_id,
            )
        except Exception as exc:
            logger.exception("Error executing Felix agent for session %s: %s", session.session_id, exc)
            elapsed_ms = (time.time() - start_time) * 1000
            err_msg = f"Felix encountered an error while analyzing payment data: {str(exc)}"
            integration_manager.record_activity(
                platform=req.platform,
                query=req.message,
                execution_status="error",
                execution_duration_ms=elapsed_ms,
                workspace_id=req.workspace_id,
                workspace_name=getattr(req, "workspace_name", None) or req.workspace_id,
                user_id=req.user_id,
                user_name=req.user_name,
                conversation_id=req.conversation_id,
                error_message=str(exc),
            )
            security_auditor.log_event(
                event_type="QUERY_FAILED",
                execution_status="ERROR",
                org_id=req.workspace_id,
                platform=req.platform,
                user_id=req.user_id,
                user_role=get_user_role(user_id=req.user_id),
                execution_duration_ms=elapsed_ms,
                delivery_status="FAILED",
                details={"error": str(exc)},
            )
            return CommonResponseHandler.create_response(
                req=req,
                session=session,
                agent_result={"investigation_status": "error", "response": err_msg},
                execution_time_ms=elapsed_ms,
                turn_index=len(session.turns) + 1,
                error=str(exc)
            )

        elapsed_ms = (time.time() - start_time) * 1000

        # 5. Extract results
        response_text = agent_result.get("response", "")
        findings = agent_result.get("findings", [])
        tool_activity = agent_result.get("tool_activity", [])
        tools_called = [t.get("tool") for t in tool_activity if t.get("tool")]

        # Record successful activity
        integration_manager.record_activity(
            platform=req.platform,
            query=req.message,
            execution_status="completed",
            execution_duration_ms=elapsed_ms,
            workspace_id=req.workspace_id,
            workspace_name=getattr(req, "workspace_name", None) or req.workspace_id,
            user_id=req.user_id,
            user_name=req.user_name,
            conversation_id=req.conversation_id,
            tools_called=tools_called,
            findings_count=len(findings),
        )

        # 6. Record turn in session state for future thread continuity
        turn_idx = session.add_turn(
            user_message=req.message.strip(),
            agent_response=response_text,
            tools_called=tools_called,
            findings=findings,
            runtime_ms=elapsed_ms
        )

        # Log security and compliance audit record
        security_auditor.log_event(
            event_type="QUERY_EXECUTED",
            execution_status="SUCCESS",
            org_id=req.workspace_id,
            platform=req.platform,
            user_id=req.user_id,
            user_role=get_user_role(user_id=req.user_id),
            execution_duration_ms=elapsed_ms,
            delivery_status="DELIVERED",
            details={"turn": turn_idx, "tools": tools_called, "findings_count": len(findings)},
        )

        # 7. Construct and return normalized response
        response = CommonResponseHandler.create_response(
            req=req,
            session=session,
            agent_result=agent_result,
            execution_time_ms=elapsed_ms,
            turn_index=turn_idx
        )

        logger.info(
            "Successfully processed message in %.2fms (Turn #%d, %d tools called)",
            elapsed_ms, turn_idx, len(tools_called)
        )
        return response

    async def process_message_async(self, req: CommonMessageRequest) -> CommonMessageResponse:
        """Asynchronously process an incoming message request in a worker thread."""
        return await asyncio.to_thread(self.process_message, req)


# Default singleton processor
common_message_processor = CommonMessageProcessor()
