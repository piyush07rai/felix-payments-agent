"""Zero-PII Privacy, Decoupling, and Reversible Tokenization Layer for Felix.

Ensures that no Personally Identifiable Information (PII), sensitive identifiers,
or payment secrets ever reach external LLMs (Google Gemini, OpenAI, Claude).

Key Architecture Components:
1. Reversible Surrogate Tokenization Vault (In-memory, session-scoped).
2. Entity Pattern Matchers (Credit Cards/PANs, Emails, Phone Numbers, SSNs, UUID/Txn IDs).
3. Schema-Level Column Deny-List (Blocks CVV, Passwords, PINs, Keys).
4. SQL Query Desanitizer (Translates surrogate tokens -> real values for local DB).
5. Tool Output Sanitizer (Translates DB rows -> surrogate tokens before LLM sees them).
6. Outbound Rehydrator (Restores real values for human presentation on Web, Slack, and Teams).
7. Live Audit Trail & Metrics for enterprise compliance.
"""

import contextvars
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("app.privacy")

# ---------------------------------------------------------------------------
# 1. Schema-Level Column Deny-List (Strictly Blocked from all SQL)
# ---------------------------------------------------------------------------
FORBIDDEN_COLUMNS: Set[str] = {
    "password",
    "password_hash",
    "passwd",
    "cvv",
    "cvc",
    "cvv2",
    "pin",
    "ssn",
    "social_security",
    "social_security_number",
    "secret",
    "private_key",
    "secret_key",
    "auth_token",
    "access_token",
    "api_key",
    "encryption_key",
}

# Compiled regex for detecting forbidden columns in SQL strings
FORBIDDEN_COLUMNS_REGEX = re.compile(
    r"\b(" + "|".join(FORBIDDEN_COLUMNS) + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 2. Entity Detection Regular Expressions & Validators
# ---------------------------------------------------------------------------
UUID_REGEX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
CUSTOM_TXN_REGEX = re.compile(
    r"(?<!\{\{)\b(?:txn|TXN|tx|TX)[-_](?!REF)[A-Za-z0-9_-]+\b(?!\}\})"
)
EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
SSN_REGEX = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
)
CARD_CANDIDATE_REGEX = re.compile(
    r"\b(?:\d[ -]*?){13,19}\b"
)


def _luhn_check(card_str: str) -> bool:
    """Validate numeric string using the standard Luhn algorithm (mod 10)."""
    digits = [int(c) for c in card_str if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            d = d * 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def mask_card_preview(card_str: str) -> str:
    """Return a safe masked display preview: e.g. '************1234'."""
    digits = [c for c in card_str if c.isdigit()]
    if len(digits) < 4:
        return "****"
    last4 = "".join(digits[-4:])
    return f"{'*' * (len(digits) - 4)}{last4}"


def mask_bin(bin_str: Any) -> str:
    """Mask 6-digit Bank Identification Number (BIN) to preserve range prefix (e.g. '4814**')."""
    s = str(bin_str).strip()
    if len(s) == 6 and s.isdigit():
        return f"{s[:4]}**"
    return s


# ---------------------------------------------------------------------------
# 3. Global In-Memory Audit Buffer (for UI and Compliance Monitoring)
# ---------------------------------------------------------------------------
GLOBAL_PRIVACY_AUDIT: deque = deque(maxlen=300)


# ---------------------------------------------------------------------------
# 4. PrivacyVault Class (Session-Scoped Bi-Directional Tokenizer)
# ---------------------------------------------------------------------------
class PrivacyVault:
    """Manages two-way reversible tokenization between real enterprise values and LLM surrogate tokens.

    Maintains complete isolation so that external LLMs only ever see synthetic tokens,
    while local database executions and user-facing presentations use the real values.
    """

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or "default"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.token_to_real: Dict[str, str] = {}
        self.real_to_token: Dict[str, str] = {}
        self.entity_types: Dict[str, str] = {}
        self.counters: Dict[str, int] = {
            "TXN_REF": 0,
            "EMAIL_REF": 0,
            "CARD_REF": 0,
            "PHONE_REF": 0,
            "SSN_REF": 0,
            "USER_REF": 0,
        }
        self.audit_log: List[Dict[str, Any]] = []

    def get_or_create_token(
        self,
        entity_type: str,
        real_value: str,
        masked_preview: Optional[str] = None,
        source: str = "auto"
    ) -> str:
        """Retrieve existing surrogate token or allocate a new deterministic token for this session."""
        clean_val = str(real_value).strip()
        if not clean_val:
            return ""

        if clean_val in self.real_to_token:
            return self.real_to_token[clean_val]

        # Allocate new token
        current_count = self.counters.get(entity_type, 0) + 1
        self.counters[entity_type] = current_count
        token = f"{{{{{entity_type}_{current_count}}}}}"

        self.real_to_token[clean_val] = token
        self.token_to_real[token] = clean_val
        self.entity_types[token] = entity_type

        # Compute safe display preview
        preview = masked_preview
        if preview is None:
            if entity_type == "CARD_REF":
                preview = mask_card_preview(clean_val)
            elif entity_type == "EMAIL_REF" and "@" in clean_val:
                parts = clean_val.split("@")
                preview = f"{parts[0][:2]}***@{parts[1]}"
            elif entity_type == "TXN_REF":
                preview = f"{clean_val[:8]}...{clean_val[-4:]}" if len(clean_val) > 12 else clean_val
            else:
                preview = f"***{clean_val[-4:]}" if len(clean_val) > 4 else "***"

        audit_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "entity_type": entity_type,
            "surrogate_token": token,
            "masked_preview": preview,
            "source": source,
            "action": "TOKENIZED_FOR_LLM"
        }
        self.audit_log.append(audit_entry)
        GLOBAL_PRIVACY_AUDIT.append(audit_entry)
        logger.debug("Privacy Vault created token %s for %s (%s)", token, entity_type, preview)
        return token

    def tokenize_text(self, text: str, source: str = "inbound_prompt") -> str:
        """Scan input text for all PII and sensitive identifiers, replacing them with surrogate tokens."""
        if not text or not isinstance(text, str):
            return text

        scrubbed = text

        # 1. Credit Card Numbers (validated by Luhn algorithm)
        for match in CARD_CANDIDATE_REGEX.finditer(text):
            candidate = match.group(0).strip()
            # Only treat as credit card if passes Luhn checksum
            if _luhn_check(candidate):
                tok = self.get_or_create_token(
                    "CARD_REF",
                    candidate,
                    masked_preview=mask_card_preview(candidate),
                    source=source
                )
                scrubbed = scrubbed.replace(candidate, tok)

        # 2. Email Addresses
        for match in EMAIL_REGEX.finditer(scrubbed):
            email = match.group(0).strip()
            tok = self.get_or_create_token("EMAIL_REF", email, source=source)
            scrubbed = scrubbed.replace(email, tok)

        # 3. Social Security Numbers (SSN)
        for match in SSN_REGEX.finditer(scrubbed):
            ssn = match.group(0).strip()
            tok = self.get_or_create_token("SSN_REF", ssn, masked_preview="***-**-****", source=source)
            scrubbed = scrubbed.replace(ssn, tok)

        # 4. Phone Numbers
        for match in PHONE_REGEX.finditer(scrubbed):
            phone = match.group(0).strip()
            # Avoid single numbers or short numeric fragments
            if sum(c.isdigit() for c in phone) >= 10:
                tok = self.get_or_create_token("PHONE_REF", phone, source=source)
                scrubbed = scrubbed.replace(phone, tok)

        # 5. UUIDs and Transaction Identifiers
        for match in UUID_REGEX.finditer(scrubbed):
            uid = match.group(0).strip()
            tok = self.get_or_create_token("TXN_REF", uid, source=source)
            scrubbed = scrubbed.replace(uid, tok)

        for match in CUSTOM_TXN_REGEX.finditer(scrubbed):
            tx = match.group(0).strip()
            tok = self.get_or_create_token("TXN_REF", tx, source=source)
            scrubbed = scrubbed.replace(tx, tok)

        return scrubbed

    def desanitize_query(self, query: str) -> str:
        """Translate surrogate tokens in an LLM-generated SQL query back into real database values."""
        if not query or not isinstance(query, str):
            return query

        clean_query = query
        # Replace each surrogate token with its real value
        for token, real_val in self.token_to_real.items():
            if token in clean_query:
                clean_query = clean_query.replace(token, real_val)
        return clean_query

    def check_deny_list(self, query: str) -> Tuple[bool, Optional[str]]:
        """Verify that the SQL query does not touch any column on the enterprise deny-list."""
        if not query:
            return True, None

        match = FORBIDDEN_COLUMNS_REGEX.search(query)
        if match:
            col = match.group(0)
            msg = (
                f"Privacy Violation: Column '{col}' is on the strict enterprise deny-list "
                "and cannot be queried. All payment secrets and personal passwords are blocked."
            )
            return False, msg
        return True, None

    def sanitize_sql_result(
        self,
        columns: List[str],
        rows: List[List[Any]]
    ) -> Tuple[List[str], List[List[Any]]]:
        """Scrub, tokenize, and mask database rows before returning tool output to the LLM."""
        if not rows or not columns:
            return columns, rows

        # Map column indices to special treatment
        txn_indices: List[int] = []
        bin_indices: List[int] = []
        email_indices: List[int] = []
        name_indices: List[int] = []

        for idx, col in enumerate(columns):
            c_lower = col.lower()
            if c_lower in ("txn_id", "transaction_id", "order_id", "id"):
                txn_indices.append(idx)
            elif c_lower in ("bin", "card_bin"):
                bin_indices.append(idx)
            elif "email" in c_lower:
                email_indices.append(idx)
            elif "name" in c_lower or "customer" in c_lower:
                name_indices.append(idx)

        sanitized_rows: List[List[Any]] = []

        for row in rows:
            new_row = list(row)

            # 1. Transaction IDs -> Surrogate Tokens
            for idx in txn_indices:
                if idx < len(new_row) and new_row[idx] is not None:
                    raw_val = str(new_row[idx])
                    new_row[idx] = self.get_or_create_token("TXN_REF", raw_val, source="db_result_txn_id")

            # 2. Card BIN -> Masked Prefix (e.g. 4814**)
            for idx in bin_indices:
                if idx < len(new_row) and new_row[idx] is not None:
                    new_row[idx] = mask_bin(new_row[idx])

            # 3. Emails
            for idx in email_indices:
                if idx < len(new_row) and new_row[idx] is not None:
                    raw_val = str(new_row[idx])
                    new_row[idx] = self.get_or_create_token("EMAIL_REF", raw_val, source="db_result_email")

            # 4. Names
            for idx in name_indices:
                if idx < len(new_row) and new_row[idx] is not None:
                    raw_val = str(new_row[idx])
                    new_row[idx] = self.get_or_create_token("USER_REF", raw_val, source="db_result_name")

            # 5. General string scan for any stray emails or cards in other columns
            for idx in range(len(new_row)):
                val = new_row[idx]
                if isinstance(val, str) and idx not in txn_indices and idx not in bin_indices:
                    new_row[idx] = self.tokenize_text(val, source="db_result_cell")

            sanitized_rows.append(new_row)

        return columns, sanitized_rows

    def rehydrate_text(self, text: str) -> str:
        """Substitute surrogate tokens in LLM output back to original real values for display."""
        if not text or not isinstance(text, str):
            return text

        rehydrated = text
        for token, real_val in self.token_to_real.items():
            if token in rehydrated:
                rehydrated = rehydrated.replace(token, real_val)
        return rehydrated

    def get_audit_summary(self) -> Dict[str, Any]:
        """Return structured summary of privacy operations performed during this session."""
        total_entities = sum(self.counters.values())
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "shield_status": "ACTIVE_ENFORCED",
            "total_entities_scrubbed": total_entities,
            "breakdown": dict(self.counters),
            "recent_audit_events": self.audit_log[-20:],
            "active_tokens_count": len(self.token_to_real)
        }


# ---------------------------------------------------------------------------
# 5. ContextVar for Active Privacy Session Management
# ---------------------------------------------------------------------------
_current_privacy_vault: contextvars.ContextVar[Optional[PrivacyVault]] = contextvars.ContextVar(
    "current_privacy_vault", default=None
)

# Fallback global vault for stateless calls
_fallback_global_vault = PrivacyVault(session_id="global_fallback")


def get_active_privacy_vault() -> PrivacyVault:
    """Return the active session's PrivacyVault, or fallback to the global vault."""
    v = _current_privacy_vault.get()
    return v if v is not None else _fallback_global_vault


def set_active_privacy_vault(vault: PrivacyVault) -> Any:
    """Bind a PrivacyVault instance to the current asynchronous execution context."""
    return _current_privacy_vault.set(vault)


def reset_active_privacy_vault(token: Any) -> None:
    """Reset the context-bound PrivacyVault."""
    _current_privacy_vault.reset(token)


def get_global_privacy_status() -> Dict[str, Any]:
    """Return system-wide privacy shield metrics and configuration."""
    recent_events = list(GLOBAL_PRIVACY_AUDIT)[-50:]
    return {
        "status": "active",
        "mode": "zero_pii_decoupled",
        "shield_name": "Felix Enterprise Zero-PII Shield",
        "algorithm": "Reversible Surrogate Tokenization & Local Rehydration Vault",
        "forbidden_columns": sorted(list(FORBIDDEN_COLUMNS)),
        "supported_pii_entities": [
            "Credit Card Numbers / PAN (Luhn validated)",
            "Bank Identification Numbers (BIN - masked to 4814**)",
            "UUIDs & Custom Transaction IDs (Surrogate Tokenized)",
            "Email Addresses",
            "Phone Numbers",
            "Social Security Numbers (SSN)",
            "Customer Names & User Identifiers"
        ],
        "total_global_scrub_events": len(GLOBAL_PRIVACY_AUDIT),
        "recent_audit_events": recent_events
    }
