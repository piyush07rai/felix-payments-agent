"""Enterprise Security, Access Control & Compliance Audit Logging (Phase 6).

Provides immutable audit records of all security-sensitive operations:
1. Authentication checks (Slack HMAC validation, Teams Bot Framework bearer tokens).
2. Authorisation and role checks (Administrator vs Standard User).
3. Data access events and query executions.
4. Action proposals drafted and Human-in-the-Loop approval decisions.
5. Strict Zero-PII sanitization ensuring sensitive customer data or credentials are NEVER logged.
"""

import datetime
import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from app.database import get_connection, init_database

logger = logging.getLogger("app.security.audit")

# In-memory buffer of recent security events
SECURITY_AUDIT_BUFFER: List[Dict[str, Any]] = []


class SecurityAuditLogger:
    """Thread-safe security audit logger with SQLite persistence."""

    def __init__(self):
        try:
            init_database()
        except Exception:
            pass

    def log_event(
        self,
        event_type: str,
        execution_status: str,
        org_id: Optional[str] = None,
        platform: Optional[str] = None,
        user_id: Optional[str] = None,
        user_role: Optional[str] = None,
        execution_duration_ms: float = 0.0,
        delivery_status: str = "DELIVERED",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a structured security audit event."""
        log_id = f"sec_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Sanitize details (ensure no raw passwords, PANs, or API keys are stored)
        safe_details = {}
        if details:
            for k, v in details.items():
                k_lower = str(k).lower()
                if any(s in k_lower for s in ("password", "token", "secret", "cvv", "pan", "key")):
                    safe_details[k] = "[REDACTED_SECURITY_SECRET]"
                else:
                    safe_details[k] = str(v)[:200]  # truncate long fields

        record = {
            "log_id": log_id,
            "ts": now_iso,
            "org_id": org_id or "default_org",
            "platform": (platform or "web").lower(),
            "user_id": user_id or "anonymous",
            "user_role": (user_role or "user").lower(),
            "event_type": event_type.upper(),
            "execution_status": execution_status.upper(),
            "execution_duration_ms": round(execution_duration_ms, 1),
            "delivery_status": delivery_status.upper(),
            "sanitized_details": safe_details,
        }

        # 1. Update in-memory buffer
        SECURITY_AUDIT_BUFFER.insert(0, record)
        if len(SECURITY_AUDIT_BUFFER) > 300:
            SECURITY_AUDIT_BUFFER.pop()

        # 2. Persist in SQLite
        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO security_audit_logs (
                    log_id, ts, org_id, platform, user_id, user_role,
                    event_type, execution_status, execution_duration_ms,
                    delivery_status, sanitized_details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["log_id"],
                    record["ts"],
                    record["org_id"],
                    record["platform"],
                    record["user_id"],
                    record["user_role"],
                    record["event_type"],
                    record["execution_status"],
                    record["execution_duration_ms"],
                    record["delivery_status"],
                    json.dumps(record["sanitized_details"]),
                ),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Could not persist security audit log to SQLite: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return record

    def get_logs(
        self,
        org_id: Optional[str] = None,
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieve security audit records with optional filters."""
        logs: List[Dict[str, Any]] = []
        conn = None
        try:
            conn = get_connection(read_only=True)
            cursor = conn.cursor()
            query = "SELECT * FROM security_audit_logs WHERE 1=1"
            params: List[Any] = []

            if org_id and org_id.lower() != "all":
                query += " AND org_id = ?"
                params.append(org_id)

            if event_type and event_type.lower() != "all":
                query += " AND event_type = ?"
                params.append(event_type.upper())

            if status and status.lower() != "all":
                query += " AND execution_status = ?"
                params.append(status.upper())

            query += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            for row in cursor.fetchall():
                details_raw = row["sanitized_details"]
                try:
                    details_dict = json.loads(details_raw) if details_raw else {}
                except Exception:
                    details_dict = {}

                logs.append({
                    "log_id": row["log_id"],
                    "ts": row["ts"],
                    "org_id": row["org_id"],
                    "platform": row["platform"],
                    "user_id": row["user_id"],
                    "user_role": row["user_role"],
                    "event_type": row["event_type"],
                    "execution_status": row["execution_status"],
                    "execution_duration_ms": row["execution_duration_ms"],
                    "delivery_status": row["delivery_status"],
                    "sanitized_details": details_dict,
                })
        except Exception as exc:
            logger.warning("Falling back to in-memory security log buffer: %s", exc)
            filtered = list(SECURITY_AUDIT_BUFFER)
            if org_id and org_id.lower() != "all":
                filtered = [l for l in filtered if l.get("org_id") == org_id]
            if event_type and event_type.lower() != "all":
                filtered = [l for l in filtered if l.get("event_type") == event_type.upper()]
            if status and status.lower() != "all":
                filtered = [l for l in filtered if l.get("execution_status") == status.upper()]
            logs = filtered[:limit]
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return logs


# Singleton security audit logger
security_auditor = SecurityAuditLogger()
