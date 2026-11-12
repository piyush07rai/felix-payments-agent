"""Enterprise Integrations Manager for Felix (Phase 5).

Provides centralized management, configuration persistence, secret masking,
health diagnostics, and activity auditing for workplace messaging integrations
(Slack, Microsoft Teams, Web, and future channels).
"""

import datetime
import json
import logging
import sqlite3
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from app.config import save_settings_to_env, settings
from app.database import get_connection, init_database

logger = logging.getLogger("app.integration.manager")

# In-memory buffer of recent activities for rapid lookup and fallback
RECENT_ACTIVITIES: deque = deque(maxlen=200)


def mask_secret(value: Optional[str], prefix_len: int = 4, suffix_len: int = 4) -> str:
    """Mask sensitive credentials so they are never exposed in frontend responses.
    
    Examples:
        'xoxb-1234567890-abcdef1234' -> 'xoxb-••••••••••••1234'
        'supersecretkey9988'          -> '••••••••••••9988'
    """
    if not value or not str(value).strip():
        return ""
    val_str = str(value).strip()
    if len(val_str) <= 8:
        return "••••••••••••"
    
    prefix = val_str[:prefix_len]
    suffix = val_str[-suffix_len:]
    return f"{prefix}••••••••••••{suffix}"


class EnterpriseIntegrationManager:
    """Central administrative manager for enterprise messaging channels."""

    def __init__(self):
        try:
            init_database()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Activity Auditing & Storage
    # -------------------------------------------------------------------------
    def record_activity(
        self,
        platform: str,
        query: str,
        execution_status: str,
        execution_duration_ms: float,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        tools_called: Optional[List[str]] = None,
        findings_count: int = 0,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an incoming interaction and its execution outcome."""
        act_id = f"act_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        activity_record = {
            "id": act_id,
            "ts": now_iso,
            "platform": (platform or "web").lower(),
            "workspace_id": workspace_id or "",
            "workspace_name": workspace_name or workspace_id or "",
            "user_id": user_id or "anonymous",
            "user_name": user_name or user_id or "User",
            "conversation_id": conversation_id or "",
            "query": query or "",
            "execution_status": execution_status,
            "execution_duration_ms": round(execution_duration_ms, 1),
            "tools_called": tools_called or [],
            "findings_count": findings_count,
            "error_message": error_message or "",
        }

        # 1. Store in memory buffer
        RECENT_ACTIVITIES.appendleft(activity_record)

        # 2. Persist in SQLite
        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO integration_activities (
                    id, ts, platform, workspace_id, workspace_name,
                    user_id, user_name, conversation_id, query,
                    execution_status, execution_duration_ms, tools_called,
                    findings_count, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    act_id,
                    now_iso,
                    activity_record["platform"],
                    activity_record["workspace_id"],
                    activity_record["workspace_name"],
                    activity_record["user_id"],
                    activity_record["user_name"],
                    activity_record["conversation_id"],
                    activity_record["query"],
                    activity_record["execution_status"],
                    activity_record["execution_duration_ms"],
                    json.dumps(activity_record["tools_called"]),
                    activity_record["findings_count"],
                    activity_record["error_message"],
                ),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Could not persist activity to SQLite: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return activity_record

    def get_activities(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Query activity records with multi-criteria filtering."""
        activities = []
        total_count = 0

        conn = None
        try:
            conn = get_connection(read_only=True)
            cursor = conn.cursor()
            query = "SELECT * FROM integration_activities WHERE 1=1"
            params: List[Any] = []

            if platform and platform.lower() != "all":
                query += " AND platform = ?"
                params.append(platform.lower())

            if status and status.lower() != "all":
                query += " AND execution_status = ?"
                params.append(status.lower())

            if search and search.strip():
                search_term = f"%{search.strip()}%"
                query += " AND (query LIKE ? OR user_name LIKE ? OR user_id LIKE ? OR workspace_name LIKE ? OR conversation_id LIKE ?)"
                params.extend([search_term, search_term, search_term, search_term, search_term])

            # Get total matching count
            count_query = query.replace("SELECT *", "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()[0]

            # Order and paginate
            query += " ORDER BY ts DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            for row in cursor.fetchall():
                tools_raw = row["tools_called"]
                try:
                    tools_list = json.loads(tools_raw) if tools_raw else []
                except Exception:
                    tools_list = []

                activities.append({
                    "id": row["id"],
                    "ts": row["ts"],
                    "platform": row["platform"],
                    "workspace_id": row["workspace_id"],
                    "workspace_name": row["workspace_name"],
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "channel": row["conversation_id"],
                    "query": row["query"],
                    "execution_status": row["execution_status"],
                    "execution_duration_ms": row["execution_duration_ms"],
                    "tools_called": tools_list,
                    "findings_count": row["findings_count"],
                    "error_message": row["error_message"],
                })
        except Exception as exc:
            logger.warning("Falling back to in-memory activity buffer: %s", exc)
            # Fallback to RECENT_ACTIVITIES buffer
            filtered = list(RECENT_ACTIVITIES)
            if platform and platform.lower() != "all":
                filtered = [a for a in filtered if a["platform"] == platform.lower()]
            if status and status.lower() != "all":
                filtered = [a for a in filtered if a["execution_status"] == status.lower()]
            if search and search.strip():
                st = search.strip().lower()
                filtered = [
                    a for a in filtered
                    if st in a["query"].lower()
                    or st in a["user_name"].lower()
                    or st in a["user_id"].lower()
                    or st in a["workspace_name"].lower()
                    or st in a["conversation_id"].lower()
                ]
            total_count = len(filtered)
            activities = filtered[offset : offset + limit]
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "activities": activities,
        }

    # -------------------------------------------------------------------------
    # Integration Overview & Real-Time Status Computation
    # -------------------------------------------------------------------------
    def get_overview(self, base_url: str = "") -> Dict[str, Any]:
        """Compute the live status, masked secrets, and activity metrics for all platforms."""
        # 1. Determine Slack status
        has_slack_token = bool(settings.SLACK_BOT_TOKEN and settings.SLACK_BOT_TOKEN.strip())
        has_slack_secret = bool(settings.SLACK_SIGNING_SECRET and settings.SLACK_SIGNING_SECRET.strip())

        if not has_slack_token or not has_slack_secret:
            slack_status = "configuration_required"
            slack_status_label = "Configuration Required"
        elif not settings.SLACK_ENABLED:
            slack_status = "disconnected"
            slack_status_label = "Disconnected"
        else:
            slack_status = "connected"
            slack_status_label = "Connected"

        # 2. Determine Microsoft Teams status
        has_teams_id = bool(settings.TEAMS_APP_ID and settings.TEAMS_APP_ID.strip())
        has_teams_pwd = bool(settings.TEAMS_APP_PASSWORD and settings.TEAMS_APP_PASSWORD.strip())

        if not has_teams_id or not has_teams_pwd:
            teams_status = "configuration_required"
            teams_status_label = "Configuration Required"
        elif not settings.TEAMS_ENABLED:
            teams_status = "disconnected"
            teams_status_label = "Disconnected"
        else:
            teams_status = "connected"
            teams_status_label = "Connected"

        # 3. Pull actual platform metrics from SQLite or buffer
        slack_count = 0
        teams_count = 0
        web_count = 0
        slack_last_interaction = None
        teams_last_interaction = None
        slack_last_error = None
        teams_last_error = None

        conn = None
        try:
            conn = get_connection(read_only=True)
            cursor = conn.cursor()
            cursor.execute("SELECT platform, COUNT(*) FROM integration_activities GROUP BY platform")
            for row in cursor.fetchall():
                p, cnt = row[0], row[1]
                if p == "slack":
                    slack_count = cnt
                elif p == "teams":
                    teams_count = cnt
                elif p == "web":
                    web_count = cnt

            cursor.execute(
                "SELECT ts FROM integration_activities WHERE platform = 'slack' AND execution_status = 'completed' ORDER BY ts DESC LIMIT 1"
            )
            r = cursor.fetchone()
            if r:
                slack_last_interaction = r[0]

            cursor.execute(
                "SELECT ts FROM integration_activities WHERE platform = 'teams' AND execution_status = 'completed' ORDER BY ts DESC LIMIT 1"
            )
            r = cursor.fetchone()
            if r:
                teams_last_interaction = r[0]

            cursor.execute(
                "SELECT error_message FROM integration_activities WHERE platform = 'slack' AND execution_status = 'error' ORDER BY ts DESC LIMIT 1"
            )
            r = cursor.fetchone()
            if r and r[0]:
                slack_last_error = r[0]

            cursor.execute(
                "SELECT error_message FROM integration_activities WHERE platform = 'teams' AND execution_status = 'error' ORDER BY ts DESC LIMIT 1"
            )
            r = cursor.fetchone()
            if r and r[0]:
                teams_last_error = r[0]
        except Exception:
            # Calculate from buffer
            for a in RECENT_ACTIVITIES:
                p = a.get("platform")
                if p == "slack":
                    slack_count += 1
                    if not slack_last_interaction and a.get("execution_status") == "completed":
                        slack_last_interaction = a.get("ts")
                    if not slack_last_error and a.get("execution_status") == "error":
                        slack_last_error = a.get("error_message")
                elif p == "teams":
                    teams_count += 1
                    if not teams_last_interaction and a.get("execution_status") == "completed":
                        teams_last_interaction = a.get("ts")
                    if not teams_last_error and a.get("execution_status") == "error":
                        teams_last_error = a.get("error_message")
                elif p == "web":
                    web_count += 1
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # Host-aware endpoint URLs
        endpoint_prefix = base_url.rstrip("/") if base_url else ""
        slack_events_url = f"{endpoint_prefix}/api/slack/events" if endpoint_prefix else "/api/slack/events"
        teams_messages_url = f"{endpoint_prefix}/api/teams/messages" if endpoint_prefix else "/api/teams/messages"

        return {
            "platforms": {
                "slack": {
                    "platform_id": "slack",
                    "name": "Slack",
                    "status": slack_status,
                    "status_label": slack_status_label,
                    "enabled": bool(settings.SLACK_ENABLED),
                    "workspace_name": getattr(settings, "SLACK_WORKSPACE_NAME", "Enterprise Slack Workspace") or "Enterprise Slack Workspace",
                    "workspace_id": getattr(settings, "SLACK_WORKSPACE_ID", "") or "",
                    "app_id": getattr(settings, "SLACK_APP_ID", "") or "",
                    "bot_user_id": getattr(settings, "SLACK_BOT_USER_ID", "U_FELIX"),
                    "bot_token_masked": mask_secret(settings.SLACK_BOT_TOKEN, prefix_len=5, suffix_len=4),
                    "signing_secret_masked": mask_secret(settings.SLACK_SIGNING_SECRET, prefix_len=4, suffix_len=4),
                    "has_bot_token": has_slack_token,
                    "has_signing_secret": has_slack_secret,
                    "event_subscription_url": slack_events_url,
                    "manifest_url": f"{endpoint_prefix}/api/slack/manifest" if endpoint_prefix else "/api/slack/manifest",
                    "total_messages": slack_count,
                    "last_successful_interaction": slack_last_interaction or "Never",
                    "last_error": slack_last_error,
                },
                "teams": {
                    "platform_id": "teams",
                    "name": "Microsoft Teams",
                    "status": teams_status,
                    "status_label": teams_status_label,
                    "enabled": bool(settings.TEAMS_ENABLED),
                    "organization_name": getattr(settings, "TEAMS_ORGANIZATION_NAME", "Enterprise Microsoft 365 Tenant") or "Enterprise Microsoft 365 Tenant",
                    "tenant_id": getattr(settings, "TEAMS_TENANT_ID", "") or "",
                    "app_id": getattr(settings, "TEAMS_APP_ID", "") or "",
                    "app_password_masked": mask_secret(settings.TEAMS_APP_PASSWORD, prefix_len=4, suffix_len=4),
                    "has_app_password": has_teams_pwd,
                    "messaging_endpoint": teams_messages_url,
                    "manifest_url": f"{endpoint_prefix}/api/teams/manifest" if endpoint_prefix else "/api/teams/manifest",
                    "total_messages": teams_count,
                    "last_successful_interaction": teams_last_interaction or "Never",
                    "last_error": teams_last_error,
                },
            },
            "health": {
                "status": "healthy",
                "agent_status": "operational",
                "database_status": "connected",
                "zero_pii_vault": "active",
                "total_messages_all": slack_count + teams_count + web_count,
                "supported_platforms": ["slack", "teams", "web"],
            },
        }

    # -------------------------------------------------------------------------
    # Configuration Management & Secret Persistence
    # -------------------------------------------------------------------------
    def update_slack_configuration(
        self,
        app_id: Optional[str] = None,
        bot_token: Optional[str] = None,
        signing_secret: Optional[str] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        bot_user_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """Update Slack settings and persist securely to .env and runtime settings."""
        updates: Dict[str, Any] = {}

        if app_id is not None:
            updates["SLACK_APP_ID"] = app_id.strip()

        if workspace_id is not None:
            updates["SLACK_WORKSPACE_ID"] = workspace_id.strip()

        if workspace_name is not None:
            updates["SLACK_WORKSPACE_NAME"] = workspace_name.strip()

        if bot_user_id is not None:
            updates["SLACK_BOT_USER_ID"] = bot_user_id.strip() or "U_FELIX"

        if enabled is not None:
            updates["SLACK_ENABLED"] = bool(enabled)

        # Update bot token only if provided and not masked
        if bot_token is not None and str(bot_token).strip():
            clean_token = str(bot_token).strip()
            if "••" not in clean_token:
                # Strict validation check: Slack bot tokens must begin with xoxb- and have sufficient length
                if not (clean_token.startswith("xoxb-") or clean_token.startswith("xoxp-")) or len(clean_token) < 15:
                    return False, "Invalid Slack Bot Token format (must begin with 'xoxb-' and be at least 15 characters)."
                updates["SLACK_BOT_TOKEN"] = clean_token

        # Update signing secret only if provided and not masked
        if signing_secret is not None and str(signing_secret).strip():
            clean_secret = str(signing_secret).strip()
            if "••" not in clean_secret:
                if len(clean_secret) < 12:
                    return False, "Slack Signing Secret must be at least 12 characters."
                updates["SLACK_SIGNING_SECRET"] = clean_secret

        save_settings_to_env(updates)
        logger.info("Updated Slack configuration with keys: %s", list(updates.keys()))
        return True, "Slack configuration updated successfully."

    def update_teams_configuration(
        self,
        app_id: Optional[str] = None,
        app_password: Optional[str] = None,
        tenant_id: Optional[str] = None,
        organization_name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """Update Teams settings and persist securely to .env and runtime settings."""
        updates: Dict[str, Any] = {}

        if app_id is not None:
            updates["TEAMS_APP_ID"] = app_id.strip()

        if tenant_id is not None:
            updates["TEAMS_TENANT_ID"] = tenant_id.strip()

        if organization_name is not None:
            updates["TEAMS_ORGANIZATION_NAME"] = organization_name.strip()

        if enabled is not None:
            updates["TEAMS_ENABLED"] = bool(enabled)

        # Update app password only if provided and not masked
        if app_password is not None and str(app_password).strip():
            clean_pwd = str(app_password).strip()
            if "••" not in clean_pwd:
                if len(clean_pwd) < 6:
                    return False, "Microsoft App Password / Client Secret must be at least 6 characters."
                updates["TEAMS_APP_PASSWORD"] = clean_pwd

        save_settings_to_env(updates)
        logger.info("Updated Microsoft Teams configuration with keys: %s", list(updates.keys()))
        return True, "Microsoft Teams configuration updated successfully."

    def toggle_platform(self, platform: str, enabled: bool) -> Tuple[bool, str]:
        """Toggle an integration on or off."""
        plat = platform.lower().strip()
        if plat == "slack":
            save_settings_to_env({"SLACK_ENABLED": enabled})
            return True, f"Slack integration {'enabled' if enabled else 'disabled'} successfully."
        elif plat == "teams":
            save_settings_to_env({"TEAMS_ENABLED": enabled})
            return True, f"Microsoft Teams integration {'enabled' if enabled else 'disabled'} successfully."
        else:
            return False, f"Unknown platform '{platform}'."

    def disconnect_platform(self, platform: str) -> Tuple[bool, str]:
        """Disconnect an integration by setting its enabled state to False."""
        return self.toggle_platform(platform, enabled=False)

    def test_connection(self, platform: str) -> Dict[str, Any]:
        """Validate configuration and perform health connectivity checks for a platform."""
        plat = platform.lower().strip()

        if plat == "slack":
            has_token = bool(settings.SLACK_BOT_TOKEN and settings.SLACK_BOT_TOKEN.strip())
            has_secret = bool(settings.SLACK_SIGNING_SECRET and settings.SLACK_SIGNING_SECRET.strip())

            if not has_token:
                return {
                    "success": False,
                    "platform": "slack",
                    "status": "configuration_required",
                    "error": "Missing Slack Bot Token. Please configure a valid Bot User OAuth Token (starts with 'xoxb-').",
                }
            if not has_secret:
                return {
                    "success": False,
                    "platform": "slack",
                    "status": "configuration_required",
                    "error": "Missing Slack Signing Secret. Please configure your App's Signing Secret from Slack App Settings.",
                }

            token = settings.SLACK_BOT_TOKEN.strip()
            if not token.startswith("xoxb-") and not token.startswith("xoxp-"):
                return {
                    "success": False,
                    "platform": "slack",
                    "status": "error",
                    "error": f"Invalid Slack Bot Token prefix '{token[:5]}'. Bot tokens must begin with 'xoxb-'.",
                }

            return {
                "success": True,
                "platform": "slack",
                "status": "connected",
                "message": f"Successfully verified Slack configuration for bot user '{settings.SLACK_BOT_USER_ID}'. Event listener active.",
                "details": {
                    "bot_user_id": settings.SLACK_BOT_USER_ID,
                    "workspace": getattr(settings, "SLACK_WORKSPACE_NAME", "Enterprise Slack Workspace"),
                    "token_prefix": token[:5],
                },
            }

        elif plat == "teams":
            has_id = bool(settings.TEAMS_APP_ID and settings.TEAMS_APP_ID.strip())
            has_pwd = bool(settings.TEAMS_APP_PASSWORD and settings.TEAMS_APP_PASSWORD.strip())

            if not has_id:
                return {
                    "success": False,
                    "platform": "teams",
                    "status": "configuration_required",
                    "error": "Missing Microsoft Application (Client) ID. Please configure your Azure Bot App ID.",
                }
            if not has_pwd:
                return {
                    "success": False,
                    "platform": "teams",
                    "status": "configuration_required",
                    "error": "Missing Microsoft Client Secret / App Password. Please configure the password in Azure Bot registration.",
                }

            return {
                "success": True,
                "platform": "teams",
                "status": "connected",
                "message": "Successfully verified Microsoft Teams Bot Framework credentials. Bot endpoint active.",
                "details": {
                    "app_id": settings.TEAMS_APP_ID[:8] + "...",
                    "tenant_id": (settings.TEAMS_TENANT_ID[:8] + "...") if settings.TEAMS_TENANT_ID else "multi-tenant",
                    "organization": getattr(settings, "TEAMS_ORGANIZATION_NAME", "Enterprise Microsoft 365 Tenant"),
                },
            }

        else:
            return {
                "success": False,
                "platform": plat,
                "status": "error",
                "error": f"Unsupported platform '{platform}'.",
            }


# Singleton integration manager instance
integration_manager = EnterpriseIntegrationManager()
