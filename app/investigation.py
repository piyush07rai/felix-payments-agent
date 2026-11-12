"""Investigation workflow orchestration for payment declines.

Coordinates end-to-end AI investigations, maintains investigation history,
and interfaces with the Gemini agent with real-time event streaming.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.agent import analyst_agent
from app.database import get_transaction_count

logger = logging.getLogger("app.investigation")


class InvestigationManager:
    """Manages investigation execution, state, and historical records."""

    def __init__(self):
        self.history: Dict[str, Dict[str, Any]] = {}

    def run_investigation(
        self,
        objective: Optional[str] = None,
        max_steps: int = 15,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """Execute a structured payment decline investigation."""
        if get_transaction_count() == 0:
            err_msg = "No transaction dataset loaded. Upload a CSV file to begin."
            if event_callback:
                event_callback({"type": "error", "stage": "no_data", "message": err_msg})
            return {
                "investigation_status": "no_data",
                "reason_for_stopping": "no_data",
                "message": err_msg,
                "response": err_msg,
                "findings": [],
                "evidence": [],
                "tool_activity": [],
                "runtime": 0.0,
                "disclaimer": "Demo using synthetic data. Recommendations are not executed."
            }

        investigation_id = f"inv_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        logger.info("Starting investigation %s: %s", investigation_id, objective or "Default Objective")

        result = analyst_agent.run_investigation(
            objective=objective,
            max_steps=max_steps,
            event_callback=event_callback
        )
        result["investigation_id"] = investigation_id
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        result["objective"] = objective or "Standard decline investigation"

        self.history[investigation_id] = result
        return result

    def run_chat(
        self,
        message: str,
        max_steps: int = 15,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """Execute conversational query with the agent."""
        if get_transaction_count() == 0:
            err_msg = "No transaction dataset loaded. Upload a CSV file to begin."
            if event_callback:
                event_callback({"type": "error", "stage": "no_data", "message": err_msg})
            return {
                "investigation_status": "no_data",
                "reason_for_stopping": "no_data",
                "message": err_msg,
                "response": err_msg,
                "findings": [],
                "evidence": [],
                "tool_activity": [],
                "runtime": 0.0,
                "disclaimer": "Demo using synthetic data. Recommendations are not executed."
            }

        return analyst_agent.run_chat(
            message=message,
            max_steps=max_steps,
            event_callback=event_callback
        )

    def get_investigation(self, investigation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a past investigation by ID."""
        return self.history.get(investigation_id)

    def list_investigations(self) -> List[Dict[str, Any]]:
        """Return a summary list of all completed investigations."""
        summaries = []
        for inv_id, data in self.history.items():
            summaries.append({
                "investigation_id": inv_id,
                "timestamp": data.get("timestamp"),
                "objective": data.get("objective"),
                "status": data.get("investigation_status"),
                "findings_count": len(data.get("findings", [])),
                "steps_executed": data.get("steps_executed", 0),
                "runtime": data.get("runtime", 0.0)
            })
        return summaries


# Global singleton instance
investigation_manager = InvestigationManager()
