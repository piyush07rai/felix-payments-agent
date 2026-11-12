"""Microsoft Teams Bot Integration Layer for Felix."""

from app.teams.client import TeamsBotClient, teams_client, SENT_TEAMS_ACTIVITIES
from app.teams.security import ActivityDeduplicator, teams_deduplicator, validate_teams_activity
from app.teams.service import TeamsEventService, teams_service
from app.teams.router import router as teams_router

__all__ = [
    "TeamsBotClient",
    "teams_client",
    "SENT_TEAMS_ACTIVITIES",
    "ActivityDeduplicator",
    "teams_deduplicator",
    "validate_teams_activity",
    "TeamsEventService",
    "teams_service",
    "teams_router",
]
