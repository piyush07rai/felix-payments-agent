"""Slack Integration Layer for Felix Enterprise AI Assistant."""

from app.slack.client import SlackClient, slack_client, SENT_SLACK_MESSAGES
from app.slack.security import verify_slack_signature, compute_slack_signature, EventDeduplicator, slack_deduplicator
from app.slack.service import SlackEventService, slack_service
from app.slack.router import router as slack_router

__all__ = [
    "SlackClient",
    "slack_client",
    "SENT_SLACK_MESSAGES",
    "verify_slack_signature",
    "compute_slack_signature",
    "EventDeduplicator",
    "slack_deduplicator",
    "SlackEventService",
    "slack_service",
    "slack_router",
]
