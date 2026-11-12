"""Database connection, schema management, and transaction data loading for Payments Decline Analyst.

Supports high-performance SQLite operations, CSV schema validation, batch loading,
and read-only connections for agent safety.
"""

import csv
import io
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Expected CSV columns specification
EXPECTED_COLUMNS = [
    "txn_id",
    "ts",
    "network",
    "issuer_bank",
    "bin",
    "channel",
    "mcc",
    "merchant_id",
    "pa_id",
    "amount",
    "response_code",
    "3ds_result",
    "latency_ms",
    "is_retry",
]

# In-memory record of the most recent loading error or validation message
_last_loading_errors: List[str] = []


def get_db_path() -> Path:
    """Return the resolved Path to the SQLite database file."""
    path = Path(settings.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection(read_only: bool = False) -> sqlite3.Connection:
    """Provide a connection to the SQLite database.
    
    If read_only is True, sets PRAGMA query_only = ON to prevent modifications.
    """
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if read_only:
        conn.execute("PRAGMA query_only = ON;")
    return conn


def init_database() -> None:
    """Initialize database schema, performance indexes, audit tables, and immutability triggers."""
    with get_connection() as conn:
        cursor = conn.cursor()

        # Transactions table definition
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                txn_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                network TEXT,
                issuer_bank TEXT,
                bin TEXT,
                channel TEXT,
                mcc TEXT,
                merchant_id TEXT,
                pa_id TEXT,
                amount REAL,
                response_code TEXT,
                "3ds_result" TEXT,
                latency_ms REAL,
                is_retry INTEGER
            );
        """)

        # Metadata table to track dataset ingestion status & administrative flags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dataset_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );
        """)

        # Ingestion batch audit log table (financial WORM tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_batches (
                batch_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                rows_in_file INTEGER NOT NULL,
                rows_appended INTEGER NOT NULL,
                duplicates_ignored INTEGER NOT NULL,
                duration_seconds REAL NOT NULL
            );
        """)

        # Enterprise Integration Activity Log (Phase 5 Dashboard)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS integration_activities (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                platform TEXT NOT NULL,
                workspace_id TEXT,
                workspace_name TEXT,
                user_id TEXT,
                user_name TEXT,
                conversation_id TEXT,
                query TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                execution_duration_ms REAL NOT NULL,
                tools_called TEXT,
                findings_count INTEGER DEFAULT 0,
                error_message TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_int_act_platform ON integration_activities(platform);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_int_act_ts ON integration_activities(ts);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_int_act_status ON integration_activities(execution_status);")

        # Phase 6: Human Approval Framework Action Proposals
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS action_proposals (
                proposal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                org_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                title TEXT NOT NULL,
                action_type TEXT NOT NULL,
                problem TEXT,
                evidence TEXT,
                likely_cause TEXT,
                expected_impact TEXT,
                recommended_action TEXT NOT NULL,
                suggested_owner TEXT,
                priority TEXT,
                status TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                rejection_reason TEXT,
                execution_locked INTEGER DEFAULT 1
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_proposals_org ON action_proposals(org_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON action_proposals(status);")

        # Phase 6: Enterprise Security Audit Log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS security_audit_logs (
                log_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                org_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                execution_duration_ms REAL NOT NULL,
                delivery_status TEXT NOT NULL,
                sanitized_details TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sec_audit_org ON security_audit_logs(org_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sec_audit_ts ON security_audit_logs(ts);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sec_audit_event ON security_audit_logs(event_type);")

        # Analytical indexes on commonly filtered decline dimensions
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_network ON transactions(network);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_issuer ON transactions(issuer_bank);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_response ON transactions(response_code);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_mcc ON transactions(mcc);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(merchant_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_channel ON transactions(channel);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_retry ON transactions(is_retry);")

        # -------------------------------------------------------------
        # Financial Immutability Triggers (Enforced at SQLite Engine Level)
        # -------------------------------------------------------------
        # 1. Strictly block in-place UPDATE modifications on transactions
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_transactions_update
            BEFORE UPDATE ON transactions
            BEGIN
                SELECT RAISE(ABORT, 'FINANCIAL_IMMUTABILITY_VIOLATION: Transaction records are strictly immutable. UPDATE operations are prohibited in financial ledgers.');
            END;
        """)

        # 2. Strictly block DELETE operations during normal operation.
        # Only an explicit administrative demo reset (which sets allow_demo_reset = 'true') is permitted.
        cursor.execute("DROP TRIGGER IF EXISTS prevent_transactions_delete;")
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_transactions_delete
            BEFORE DELETE ON transactions
            WHEN (SELECT COUNT(*) FROM dataset_metadata WHERE key = 'allow_demo_reset' AND value = 'true') = 0
            BEGIN
                SELECT RAISE(ABORT, 'FINANCIAL_IMMUTABILITY_VIOLATION: Individual transaction record deletion is prohibited in financial ledgers.');
            END;
        """)



def get_transaction_count() -> int:
    """Return total number of loaded transactions in SQLite."""
    db_path = get_db_path()
    if not db_path.exists():
        return 0
    try:
        with get_connection(read_only=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM transactions;")
            result = cursor.fetchone()
            return int(result[0]) if result else 0
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return 0


def get_metadata(key: str) -> Optional[str]:
    """Retrieve metadata entry by key."""
    db_path = get_db_path()
    if not db_path.exists():
        return None
    try:
        with get_connection(read_only=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM dataset_metadata WHERE key = ?;", (key,))
            row = cursor.fetchone()
            return str(row[0]) if row else None
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return None


def set_metadata(key: str, value: str) -> None:
    """Store or update metadata entry."""
    with get_connection() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO dataset_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """, (key, value, now))


def clear_transactions() -> None:
    """Administrative demo reset: clear loaded transactions and reset metadata for next presentation.
    
    Temporarily asserts allow_demo_reset flag, performs clean purge, and re-arms immutability.
    """
    init_database()
    with get_connection() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        # Authorize demo reset via admin flag
        cursor.execute("""
            INSERT INTO dataset_metadata (key, value, updated_at)
            VALUES ('allow_demo_reset', 'true', ?)
            ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = excluded.updated_at;
        """, (now,))

        cursor.execute("DELETE FROM transactions;")
        cursor.execute("DELETE FROM ingestion_batches;")
        cursor.execute("DELETE FROM dataset_metadata WHERE key != 'allow_demo_reset';")

        # Re-arm immutability trigger immediately
        cursor.execute("UPDATE dataset_metadata SET value = 'false', updated_at = ? WHERE key = 'allow_demo_reset';", (now,))


def check_database_status() -> dict:
    """Return database status, transaction count, and metadata."""
    global _last_loading_errors
    db_path = get_db_path()
    exists = db_path.exists()
    count = get_transaction_count() if exists else 0
    dataset_loaded = count > 0

    status_str = "ready" if dataset_loaded else ("empty" if exists else "not_initialized")
    last_loaded = get_metadata("last_loaded_at")
    filename = get_metadata("filename")
    last_batch_mode = get_metadata("last_batch_mode") or "append_only"

    return {
        "dataset_loaded": dataset_loaded,
        "transaction_count": count,
        "database_status": status_str,
        "database_path": str(db_path),
        "immutability_enforced": True,
        "required_columns": EXPECTED_COLUMNS,
        "loading_errors": _last_loading_errors if _last_loading_errors else None,
        "metadata": {
            "last_loaded_at": last_loaded,
            "filename": filename,
            "mode": last_batch_mode
        } if last_loaded else None
    }


def parse_boolean(val: Any) -> int:
    """Convert boolean or string representations to integer 0 or 1."""
    if isinstance(val, bool):
        return 1 if val else 0
    if val is None:
        return 0
    s = str(val).strip().lower()
    if s in ("1", "true", "t", "yes", "y"):
        return 1
    if s in ("0", "false", "f", "no", "n", ""):
        return 0
    raise ValueError(f"Invalid boolean value: '{val}'")


def validate_csv_header(header: List[str]) -> Tuple[bool, List[str]]:
    """Verify that all required columns are present in the CSV header."""
    normalized_header = [c.strip().lower() for c in header if c is not None]
    missing = [col for col in EXPECTED_COLUMNS if col.lower() not in normalized_header]
    if missing:
        return False, [f"Missing required column(s): {', '.join(missing)}"]
    return True, []


def load_csv_data(
    file_content: str,
    filename: str = "transactions.csv",
    replace: bool = True,
    batch_size: int = 10000
) -> Dict[str, Any]:
    """Validate and load CSV data into the transactions table.
    
    Performs full column validation, type checks, and atomic batch insertion.
    Rolls back entirely if validation fails.
    """
    global _last_loading_errors
    start_time = time.time()
    errors: List[str] = []

    if not file_content or not file_content.strip():
        errors.append("File is empty.")
        _last_loading_errors = errors
        return {
            "success": False,
            "message": "Empty file received.",
            "rows_loaded": 0,
            "errors": errors
        }

    # Initialize tables if not already initialized
    init_database()

    stream = io.StringIO(file_content.strip())
    try:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            errors.append("CSV file has no rows.")
            _last_loading_errors = errors
            return {
                "success": False,
                "message": "CSV file has no header row.",
                "rows_loaded": 0,
                "errors": errors
            }

        # Normalize header indices
        header_map = {col.strip().lower(): idx for idx, col in enumerate(header)}
        is_valid_header, header_errors = validate_csv_header(header)
        if not is_valid_header:
            errors.extend(header_errors)
            _last_loading_errors = errors
            return {
                "success": False,
                "message": "Header validation failed.",
                "rows_loaded": 0,
                "errors": errors
            }

        batch: List[Tuple] = []
        rows_processed = 0
        seen_txn_ids = set()

        for line_num, row in enumerate(reader, start=2):
            if not row or all(field.strip() == "" for field in row):
                continue  # skip completely blank lines

            if len(row) < len(EXPECTED_COLUMNS):
                errors.append(f"Row {line_num}: Malformed row has {len(row)} columns, expected at least {len(EXPECTED_COLUMNS)}.")
                if len(errors) >= 20:
                    errors.append("Too many malformed rows. Aborting validation.")
                    break
                continue

            try:
                txn_id = row[header_map["txn_id"]].strip()
                if not txn_id:
                    errors.append(f"Row {line_num}: 'txn_id' cannot be empty.")
                elif txn_id in seen_txn_ids:
                    errors.append(f"Row {line_num}: Duplicate 'txn_id' found: '{txn_id}'.")
                seen_txn_ids.add(txn_id)

                ts = row[header_map["ts"]].strip()
                if not ts:
                    errors.append(f"Row {line_num}: 'ts' timestamp cannot be empty.")

                network = row[header_map["network"]].strip()
                issuer_bank = row[header_map["issuer_bank"]].strip()
                bin_num = row[header_map["bin"]].strip()
                channel = row[header_map["channel"]].strip()
                mcc = row[header_map["mcc"]].strip()
                merchant_id = row[header_map["merchant_id"]].strip()
                pa_id = row[header_map["pa_id"]].strip()

                # Numeric validations
                amount_str = row[header_map["amount"]].strip()
                try:
                    amount = float(amount_str) if amount_str else 0.0
                except ValueError:
                    errors.append(f"Row {line_num}: Invalid amount '{amount_str}'.")

                response_code = row[header_map["response_code"]].strip()
                three_ds_result = row[header_map["3ds_result"]].strip()

                latency_str = row[header_map["latency_ms"]].strip()
                try:
                    latency_ms = float(latency_str) if latency_str else 0.0
                except ValueError:
                    errors.append(f"Row {line_num}: Invalid latency_ms '{latency_str}'.")

                retry_str = row[header_map["is_retry"]].strip()
                try:
                    is_retry = parse_boolean(retry_str)
                except ValueError:
                    errors.append(f"Row {line_num}: Invalid is_retry value '{retry_str}'.")

                if len(errors) >= 20:
                    errors.append("Maximum validation error threshold reached (20 errors). Aborting.")
                    break

                batch.append((
                    txn_id,
                    ts,
                    network,
                    issuer_bank,
                    bin_num,
                    channel,
                    mcc,
                    merchant_id,
                    pa_id,
                    amount,
                    response_code,
                    three_ds_result,
                    latency_ms,
                    is_retry
                ))
                rows_processed += 1

            except (IndexError, KeyError) as e:
                errors.append(f"Row {line_num}: Column indexing error ({str(e)}).")
                if len(errors) >= 20:
                    break

        if errors:
            _last_loading_errors = errors
            return {
                "success": False,
                "message": f"Data validation failed with {len(errors)} error(s).",
                "rows_loaded": 0,
                "errors": errors
            }

        # If user explicitly requested Demo Reset, wipe clean before loading
        if replace:
            clear_transactions()

        pre_count = get_transaction_count()

        # Insert into SQLite database using append-only INSERT OR IGNORE
        # Preserves financial immutability: existing transactions are never mutated
        with get_connection() as conn:
            cursor = conn.cursor()
            insert_sql = """
                INSERT OR IGNORE INTO transactions (
                    txn_id, ts, network, issuer_bank, bin, channel, mcc,
                    merchant_id, pa_id, amount, response_code, "3ds_result",
                    latency_ms, is_retry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """
            cursor.executemany(insert_sql, batch)

        post_count = get_transaction_count()
        rows_appended = post_count - pre_count
        duplicates_ignored = len(batch) - rows_appended
        elapsed = round(time.time() - start_time, 2)

        # Audit log the ingestion batch
        batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ingestion_batches (
                    batch_id, filename, uploaded_at, mode,
                    rows_in_file, rows_appended, duplicates_ignored, duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                batch_id,
                filename,
                datetime.now(timezone.utc).isoformat(),
                "demo_reset" if replace else "append_only",
                len(batch),
                rows_appended,
                duplicates_ignored,
                elapsed
            ))

        # Update metadata
        set_metadata("filename", filename)
        set_metadata("last_loaded_at", datetime.now(timezone.utc).isoformat())
        set_metadata("transaction_count", str(post_count))
        set_metadata("last_batch_mode", "demo_reset" if replace else "append_only")

        _last_loading_errors = []

        if replace:
            success_msg = f"Demo environment reset: Loaded {rows_appended:,} transactions from {filename} in {elapsed}s."
        else:
            success_msg = f"Appended {rows_appended:,} new transactions from {filename} ({duplicates_ignored:,} duplicates ignored) in {elapsed}s. Total transactions: {post_count:,}."

        return {
            "success": True,
            "message": success_msg,
            "filename": filename,
            "rows_loaded": post_count,
            "rows_appended": rows_appended,
            "duplicates_ignored": duplicates_ignored,
            "mode": "demo_reset" if replace else "append_only",
            "duration_seconds": elapsed,
            "errors": []
        }

    except Exception as exc:
        logger.exception("Unexpected error while loading CSV")
        err_msg = f"Unexpected failure loading dataset: {str(exc)}"
        _last_loading_errors = [err_msg]
        return {
            "success": False,
            "message": err_msg,
            "rows_loaded": 0,
            "errors": [err_msg]
        }
