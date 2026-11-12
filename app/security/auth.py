"""Enterprise Access Control & Role-Based Authorization for Felix (Phase 6).

Enforces:
1. Two-tier permissions: 'Organisation Administrator' vs 'Standard User'.
2. Role extraction from headers (X-User-Role), API keys (X-Admin-Key), and platform User IDs.
3. Strict organisation-level boundary isolation between workspaces/tenants.
4. Protection of administrative endpoints against unauthorized modifications.
"""

import enum
import logging
from typing import Optional

try:
    from fastapi import Header, HTTPException, Request, status
    FASTAPI_AVAILABLE = True
except ImportError:
    Header = lambda *args, **kwargs: None
    HTTPException = Exception
    Request = None
    status = None
    FASTAPI_AVAILABLE = False

from app.config import settings

logger = logging.getLogger("app.security.auth")


class UserRole(str, enum.Enum):
    """Supported enterprise authorization roles."""
    ADMIN = "admin"
    USER = "user"


def get_user_role(
    role_header: Optional[str] = None,
    user_id: Optional[str] = None,
    admin_key_header: Optional[str] = None,
) -> UserRole:
    """Determine the effective user role based on headers, API keys, or user IDs.
    
    Default is UserRole.USER (Standard User) following the principle of least privilege.
    """
    # 1. Direct Admin API Key verification
    expected_key = getattr(settings, "ADMIN_API_KEY", "felix-admin-key-2026")
    if admin_key_header and admin_key_header.strip() == expected_key:
        return UserRole.ADMIN

    # 2. Configured admin user IDs allowlist (e.g. for Slack/Teams admin handles)
    admin_users = [u.strip().lower() for u in getattr(settings, "ADMIN_USER_IDS", "").split(",") if u.strip()]
    if user_id and str(user_id).strip().lower() in admin_users:
        return UserRole.ADMIN

    # 3. Explicit role header (from web session / API client)
    if role_header:
        normalized = role_header.strip().lower()
        if normalized in ("admin", "administrator", "org_admin", "organisation_administrator"):
            return UserRole.ADMIN

    # Default to Standard User
    return UserRole.USER


def is_admin(
    role_header: Optional[str] = None,
    user_id: Optional[str] = None,
    admin_key_header: Optional[str] = None,
) -> bool:
    """Check if the requesting identity holds the Organisation Administrator role."""
    return get_user_role(role_header, user_id, admin_key_header) == UserRole.ADMIN


def verify_admin_access(
    x_user_role: Optional[str] = None,
    x_admin_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """FastAPI helper to enforce Organisation Administrator permission.
    
    Raises HTTPException(403) if the request is not from an authorized administrator.
    """
    if not is_admin(role_header=x_user_role, user_id=user_id, admin_key_header=x_admin_key):
        logger.warning(
            "Access Denied: Standard user attempted administrative action (role_header=%s, user_id=%s)",
            x_user_role, user_id
        )
        if FASTAPI_AVAILABLE and HTTPException:
            raise HTTPException(
                status_code=403,
                detail="Access Denied: Organisation Administrator role required to perform this action.",
            )
        else:
            raise PermissionError("Access Denied: Organisation Administrator role required.")


def verify_organization_isolation(caller_org_id: Optional[str], target_org_id: Optional[str]) -> bool:
    """Ensure data requests are strictly isolated within the caller's enterprise organization."""
    if not caller_org_id or not target_org_id:
        return True  # Local or non-multi-tenant mode
    
    clean_caller = str(caller_org_id).strip()
    clean_target = str(target_org_id).strip()
    
    if clean_caller != clean_target:
        logger.warning(
            "ORGANISATION ISOLATION BREACH PREVENTED: Caller org '%s' attempted to access data for org '%s'",
            clean_caller, clean_target
        )
        return False
    return True
