"""Enterprise Messaging Integration Layer for Felix.

Exposes unified messaging contracts, session management, and processing services
for multi-platform integration (Slack, Microsoft Teams, Web, API).
"""

from app.integration.schemas import CommonMessageRequest, CommonMessageResponse
from app.integration.session import SessionManager, SessionState, session_manager
from app.integration.response_handler import CommonResponseHandler
from app.integration.processor import CommonMessageProcessor, common_message_processor
from app.integration.manager import EnterpriseIntegrationManager, integration_manager
from app.integration.router import router as integrations_router

__all__ = [
    "CommonMessageRequest",
    "CommonMessageResponse",
    "SessionManager",
    "SessionState",
    "session_manager",
    "CommonResponseHandler",
    "CommonMessageProcessor",
    "common_message_processor",
    "EnterpriseIntegrationManager",
    "integration_manager",
    "integrations_router",
]
