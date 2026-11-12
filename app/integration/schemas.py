"""Common Message Schemas for Enterprise Felix Integration.

Defines the normalized request and response contracts used by all communication
platforms (Slack, Microsoft Teams, Web, API, Discord, etc.) to interact with Felix.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, Field

    class CommonMessageRequest(BaseModel):
        """Unified request schema accepted from all communication platforms."""
        platform: str = Field(..., description="Source platform: 'slack', 'teams', 'web', 'api'")
        workspace_id: str = Field(..., description="Organization or workspace identifier (e.g. 'workspace_123')")
        user_id: str = Field(..., description="Platform user identifier (e.g. 'user_456')")
        conversation_id: str = Field(..., description="Channel or direct chat identifier (e.g. 'channel_789')")
        thread_id: Optional[str] = Field(None, description="Thread timestamp or thread identifier (e.g. 'thread_101')")
        message: str = Field(..., description="User query or message text")
        user_name: Optional[str] = Field(None, description="Display name of the user if provided by platform")
        request_id: Optional[str] = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:10]}")
        timestamp: Optional[str] = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
        metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Platform-specific metadata")

        class Config:
            extra = "ignore"

    class CommonMessageResponse(BaseModel):
        """Unified response schema returned to the calling messaging platform."""
        request_id: str
        platform: str
        workspace_id: str
        conversation_id: str
        thread_id: Optional[str] = None
        user_id: str
        session_id: str
        status: str = Field("completed", description="Execution status: 'completed', 'error', 'no_data', 'unconfigured'")
        response_text: str = Field(..., description="Rehydrated cleartext response for the user")
        platform_payload: Dict[str, Any] = Field(default_factory=dict, description="Formatted payloads: Slack blocks, Teams cards, Markdown")
        findings: List[Dict[str, Any]] = Field(default_factory=list, description="Structured remediation findings")
        tool_activity: List[Dict[str, Any]] = Field(default_factory=list, description="Tools executed by Felix")
        privacy_metadata: Dict[str, Any] = Field(default_factory=dict, description="Zero-PII decoupling and scrubbing summary")
        turn_index: int = Field(1, description="Index of this conversational turn in the thread")
        execution_time_ms: float = Field(0.0, description="End-to-end processing duration in milliseconds")
        timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
        error: Optional[str] = None

        class Config:
            extra = "ignore"

except ImportError:
    # Pure Python fallback when pydantic is not available in local test environment
    class CommonMessageRequest:
        def __init__(
            self,
            platform: str,
            workspace_id: str,
            user_id: str,
            conversation_id: str,
            message: str,
            thread_id: Optional[str] = None,
            user_name: Optional[str] = None,
            request_id: Optional[str] = None,
            timestamp: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            **kwargs
        ):
            self.platform = str(platform).lower()
            self.workspace_id = str(workspace_id)
            self.user_id = str(user_id)
            self.conversation_id = str(conversation_id)
            self.thread_id = str(thread_id) if thread_id else None
            self.message = str(message)
            self.user_name = str(user_name) if user_name else None
            self.request_id = request_id or f"req_{uuid.uuid4().hex[:10]}"
            self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
            self.metadata = metadata or {}

        def model_dump(self) -> Dict[str, Any]:
            return {
                "platform": self.platform,
                "workspace_id": self.workspace_id,
                "user_id": self.user_id,
                "conversation_id": self.conversation_id,
                "thread_id": self.thread_id,
                "message": self.message,
                "user_name": self.user_name,
                "request_id": self.request_id,
                "timestamp": self.timestamp,
                "metadata": self.metadata,
            }

        def dict(self) -> Dict[str, Any]:
            return self.model_dump()

    class CommonMessageResponse:
        def __init__(
            self,
            request_id: str,
            platform: str,
            workspace_id: str,
            conversation_id: str,
            user_id: str,
            session_id: str,
            response_text: str,
            thread_id: Optional[str] = None,
            status: str = "completed",
            platform_payload: Optional[Dict[str, Any]] = None,
            findings: Optional[List[Dict[str, Any]]] = None,
            tool_activity: Optional[List[Dict[str, Any]]] = None,
            privacy_metadata: Optional[Dict[str, Any]] = None,
            turn_index: int = 1,
            execution_time_ms: float = 0.0,
            timestamp: Optional[str] = None,
            error: Optional[str] = None,
            **kwargs
        ):
            self.request_id = request_id
            self.platform = platform
            self.workspace_id = workspace_id
            self.conversation_id = conversation_id
            self.thread_id = thread_id
            self.user_id = user_id
            self.session_id = session_id
            self.status = status
            self.response_text = response_text
            self.platform_payload = platform_payload or {}
            self.findings = findings or []
            self.tool_activity = tool_activity or []
            self.privacy_metadata = privacy_metadata or {}
            self.turn_index = turn_index
            self.execution_time_ms = execution_time_ms
            self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
            self.error = error

        def model_dump(self) -> Dict[str, Any]:
            return {
                "request_id": self.request_id,
                "platform": self.platform,
                "workspace_id": self.workspace_id,
                "conversation_id": self.conversation_id,
                "thread_id": self.thread_id,
                "user_id": self.user_id,
                "session_id": self.session_id,
                "status": self.status,
                "response_text": self.response_text,
                "platform_payload": self.platform_payload,
                "findings": self.findings,
                "tool_activity": self.tool_activity,
                "privacy_metadata": self.privacy_metadata,
                "turn_index": self.turn_index,
                "execution_time_ms": self.execution_time_ms,
                "timestamp": self.timestamp,
                "error": self.error,
            }

        def dict(self) -> Dict[str, Any]:
            return self.model_dump()
