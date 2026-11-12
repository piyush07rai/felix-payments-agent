"""Session and Conversation State Management for Enterprise Felix.

Provides multi-turn conversational context, thread memory, and strict isolation
between different workspaces, channels, and users across all messaging platforms.
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.integration.schemas import CommonMessageRequest


class SessionState:
    """Represents an isolated multi-turn conversational session on a messaging platform."""

    def __init__(
        self,
        session_id: str,
        platform: str,
        workspace_id: str,
        conversation_id: str,
        thread_id: Optional[str] = None,
        user_id: str = "",
        user_name: Optional[str] = None,
    ):
        self.session_id = session_id
        self.platform = platform
        self.workspace_id = workspace_id
        self.conversation_id = conversation_id
        self.thread_id = thread_id
        self.user_id = user_id
        self.user_name = user_name
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at
        self.turns: List[Dict[str, Any]] = []
        self.accumulated_findings: List[Dict[str, Any]] = []
        self.metadata: Dict[str, Any] = {}

    def add_turn(
        self,
        user_message: str,
        agent_response: str,
        tools_called: Optional[List[str]] = None,
        findings: Optional[List[Dict[str, Any]]] = None,
        runtime_ms: float = 0.0,
    ) -> int:
        """Record a conversation turn and update timestamps."""
        turn_idx = len(self.turns) + 1
        now = datetime.now(timezone.utc).isoformat()
        self.updated_at = now

        # Add unique findings to accumulated findings ledger
        if findings:
            for f in findings:
                if f not in self.accumulated_findings:
                    self.accumulated_findings.append(f)

        turn_record = {
            "turn_index": turn_idx,
            "timestamp": now,
            "user_message": user_message,
            "agent_response": agent_response,
            "tools_called": tools_called or [],
            "findings_count": len(findings or []),
            "runtime_ms": round(runtime_ms, 2),
        }
        self.turns.append(turn_record)
        return turn_idx

    def get_history(self, max_turns: int = 6) -> List[Dict[str, str]]:
        """Retrieve recent turns formatted for Gemini chat context.

        Returns:
            List of dictionaries with 'role' ('user' or 'model') and 'text'.
        """
        history: List[Dict[str, str]] = []
        recent_turns = self.turns[-max_turns:]
        for turn in recent_turns:
            if turn.get("user_message"):
                history.append({"role": "user", "text": turn["user_message"]})
            if turn.get("agent_response"):
                history.append({"role": "model", "text": turn["agent_response"]})
        return history

    def to_dict(self) -> Dict[str, Any]:
        """Serialize session state for API inspection."""
        return {
            "session_id": self.session_id,
            "platform": self.platform,
            "workspace_id": self.workspace_id,
            "conversation_id": self.conversation_id,
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": len(self.turns),
            "findings_count": len(self.accumulated_findings),
            "turns": self.turns,
            "accumulated_findings": self.accumulated_findings,
        }


class SessionManager:
    """Thread-safe session repository providing complete workspace and channel isolation."""

    def __init__(self):
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def build_session_id(
        platform: str,
        workspace_id: str,
        conversation_id: str,
        thread_id: Optional[str] = None
    ) -> str:
        """Construct deterministic canonical session key guaranteeing cross-platform isolation."""
        p = str(platform).strip().lower()
        w = str(workspace_id).strip()
        c = str(conversation_id).strip()
        t = str(thread_id).strip() if thread_id else c
        return f"{p}:{w}:{c}:{t}"

    def get_or_create_session(self, req: CommonMessageRequest) -> SessionState:
        """Retrieve an existing session or initialize a new one from a CommonMessageRequest."""
        session_id = self.build_session_id(
            platform=req.platform,
            workspace_id=req.workspace_id,
            conversation_id=req.conversation_id,
            thread_id=req.thread_id,
        )

        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState(
                    session_id=session_id,
                    platform=req.platform,
                    workspace_id=req.workspace_id,
                    conversation_id=req.conversation_id,
                    thread_id=req.thread_id,
                    user_id=req.user_id,
                    user_name=req.user_name,
                )
            return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Fetch session by canonical ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List summary of active sessions, optionally filtered by workspace/organization."""
        with self._lock:
            results = []
            for s in self._sessions.values():
                if workspace_id is None or s.workspace_id == workspace_id:
                    results.append({
                        "session_id": s.session_id,
                        "platform": s.platform,
                        "workspace_id": s.workspace_id,
                        "conversation_id": s.conversation_id,
                        "thread_id": s.thread_id,
                        "turn_count": len(s.turns),
                        "findings_count": len(s.accumulated_findings),
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                    })
            return sorted(results, key=lambda x: x["updated_at"], reverse=True)

    def clear_session(self, session_id: str) -> bool:
        """Remove a session from memory."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def clear_all(self) -> None:
        """Reset all in-memory sessions (used for testing or maintenance)."""
        with self._lock:
            self._sessions.clear()


# Default singleton session manager
session_manager = SessionManager()
