"""Agent tools for Payments Decline Analyst.

Operates on real transaction data loaded in SQLite.
Provides four tools for Google Gemini function-calling:
1. run_sql: Safe, read-only SQL execution on transactions with timeout & limits.
2. lookup_response_code: ISO 8583 response code semantic lookup and classification.
3. get_baseline: Statistical baseline and dimensional decline rate calculations.
4. draft_action: Actionable remediation recommendation drafter (no operational execution).
"""

import contextvars
import json
import logging
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from app.config import settings
from app.database import (
    get_connection,
    get_db_path,
    get_transaction_count,
)
from app.privacy import get_active_privacy_vault

logger = logging.getLogger("app.tools")

# Directory for persistent tool execution logs
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TOOL_LOG_FILE = LOGS_DIR / "agent_tools.log"

# In-memory execution history for fast inspection
TOOL_EXECUTION_LOGS: deque = deque(maxlen=200)

# Disallowed SQL command keywords to prevent data modification or schema tampering
DISALLOWED_SQL_PATTERNS = [
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bDROP\b",
    r"\bALTER\b",
    r"\bCREATE\b",
    r"\bATTACH\b",
    r"\bDETACH\b",
    r"\bPRAGMA\b",
    r"\bREPLACE\b",
    r"\bVACUUM\b",
]

# Supported response codes specification
RESPONSE_CODES_SPEC = {
    "00": {
        "meaning": "Approved / Authorization Successful",
        "category": "Approved",
        "likely_responsible_party": "Issuer / Cardholder",
        "recommended_action": "Proceed with order fulfillment and capture."
    },
    "05": {
        "meaning": "Do Not Honor (Generic Decline)",
        "category": "Business decline",
        "likely_responsible_party": "Issuing Bank / Cardholder",
        "recommended_action": "Prompt customer to contact card issuing bank or retry with an alternate card payment method."
    },
    "51": {
        "meaning": "Insufficient Funds",
        "category": "Business decline",  # Strictly Business decline, never Technical failure
        "likely_responsible_party": "Cardholder",
        "recommended_action": "Prompt customer to fund account or switch payment method. For recurring billing, schedule smart retry."
    },
    "91": {
        "meaning": "Issuer System Unavailable / Switch Timeout",
        "category": "Technical failure",
        "likely_responsible_party": "Issuing Bank / Network Switch",
        "recommended_action": "Retry transaction after automated exponential backoff or dynamic failover routing to alternate processor."
    },
    "96": {
        "meaning": "System Malfunction / Processing Error",
        "category": "Technical failure",
        "likely_responsible_party": "Payment Aggregator / Acquirer / Network",
        "recommended_action": "Investigate network logs and connector status. Route traffic through backup payment gateway."
    }
}

# Valid metrics for get_baseline
SUPPORTED_METRICS = [
    "approval_rate",
    "decline_rate",
    "code_91_rate",
    "code_51_rate",
    "three_ds_timeout_rate",
    "average_latency",
    "transaction_volume",
]

# Valid dimensions for get_baseline
SUPPORTED_DIMENSIONS = [
    "issuer_bank",
    "network",
    "channel",
    "mcc",
    "merchant_id",
    "pa_id",
    "response_code",
    "day_of_week",
    "hour",
]

# Valid cause categories for draft_action
VALID_CAUSE_CATEGORIES = [
    "Issuer technical",
    "Acquirer technical",
    "Payment aggregator technical",
    "Business decline",
    "Authentication issue",
    "Unknown",
]


# Context variable for live event callbacks (used by SSE streaming)
current_tool_event_callback: contextvars.ContextVar = contextvars.ContextVar(
    "current_tool_event_callback", default=None
)


def set_tool_event_callback(callback: Optional[Callable[[Dict[str, Any]], None]]) -> Any:
    """Set contextual callback to receive real-time tool execution events."""
    return current_tool_event_callback.set(callback)


def reset_tool_event_callback(token: Any) -> None:
    """Reset contextual callback."""
    current_tool_event_callback.reset(token)


def _emit_tool_event(event: Dict[str, Any]) -> None:
    """Internal helper to dispatch live event if callback is active."""
    cb = current_tool_event_callback.get()
    if cb is not None:
        try:
            if "timestamp" not in event:
                event["timestamp"] = datetime.now(timezone.utc).isoformat()
            cb(event)
        except Exception as exc:
            logger.debug("Error in live event callback: %s", exc)


def _log_tool_execution(
    tool_name: str,
    inputs: Dict[str, Any],
    status: str,
    execution_time_ms: float,
    output: Any,
    error: Optional[str] = None
) -> None:
    """Record structured log of tool execution to logger and persistent file."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "inputs": inputs,
        "status": status,
        "execution_time_ms": round(execution_time_ms, 2),
        "output_summary": output if not isinstance(output, dict) else {
            k: (v if k not in ("rows", "groups") else f"[{len(v)} items]")
            for k, v in output.items()
        },
        "error": error
    }

    # In-memory buffer
    TOOL_EXECUTION_LOGS.append(record)

    # Logger output
    if status == "SUCCESS":
        logger.info(
            "Tool %s executed successfully in %.2fms",
            tool_name, execution_time_ms
        )
        _emit_tool_event({
            "type": "tool_end",
            "agent": "Gemini 3.8 Flash Analyst",
            "tool": tool_name,
            "stage": "analyzing_results",
            "status": "SUCCESS",
            "inputs": inputs,
            "execution_time_ms": round(execution_time_ms, 2),
            "output_summary": record["output_summary"],
            "message": f"Completed {tool_name} successfully in {round(execution_time_ms, 1)}ms."
        })
    else:
        logger.warning(
            "Tool %s failed in %.2fms: %s",
            tool_name, execution_time_ms, error
        )
        _emit_tool_event({
            "type": "tool_error",
            "agent": "Gemini 3.8 Flash Analyst",
            "tool": tool_name,
            "stage": "facing_error_retrying",
            "status": "ERROR",
            "inputs": inputs,
            "error": error,
            "message": f"Tool {tool_name} encountered an issue: {error}. Gemini agent evaluating and adjusting query/plan..."
        })

    # Persistent log file
    try:
        with open(TOOL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error("Failed to write tool log to disk: %s", e)


def get_tool_execution_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve recent in-memory tool execution logs."""
    return list(TOOL_EXECUTION_LOGS)[-limit:]


# ============================================================================
# TOOL 1: run_sql
# ============================================================================

def run_sql(query: str) -> Dict[str, Any]:
    """Execute a read-only SQL query on the transactions database.

    Operates on SQLite with a read-only connection.
    Enforces SELECT statements only, prevents modifications, and limits results to 500 rows.

    Args:
        query: SQL SELECT query string to run on the 'transactions' table.

    Returns:
        Structured dictionary with columns, rows, row count, execution time, and status.
    """
    start_time = time.time()
    inputs = {"query": query}

    _emit_tool_event({
        "type": "tool_start",
        "agent": "Gemini 3.8 Flash Analyst",
        "tool": "run_sql",
        "stage": "extracting_data",
        "inputs": inputs,
        "message": f"Executing SQL on transactions: {str(query).strip()[:140]}..."
    })

    # 1. Query string sanitization & validation
    if not query or not query.strip():
        err = "Query string cannot be empty."
        _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err, "columns": [], "rows": [], "row_count": 0}

    clean_query = query.strip()

    # Retrieve active Zero-PII privacy vault
    vault = get_active_privacy_vault()

    # Block columns on the enterprise deny-list (cvv, password, ssn, pin, etc.)
    allowed, deny_err = vault.check_deny_list(clean_query)
    if not allowed:
        _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, deny_err)
        return {"success": False, "error": deny_err, "columns": [], "rows": [], "row_count": 0}

    # Block multiple statements (semicolon inside or multiple queries)
    # Strip one optional trailing semicolon
    query_without_trailing_semi = re.sub(r";\s*$", "", clean_query)
    if ";" in query_without_trailing_semi:
        err = "Multiple SQL statements are not permitted for security reasons."
        _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err, "columns": [], "rows": [], "row_count": 0}

    # Ensure query starts with SELECT or WITH (for CTEs)
    if not re.match(r"^(SELECT|WITH)\b", clean_query, re.IGNORECASE):
        err = "Only read-only SELECT queries are allowed. Modification or schema queries are blocked."
        _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err, "columns": [], "rows": [], "row_count": 0}

    # Check for disallowed modification keywords
    for pattern in DISALLOWED_SQL_PATTERNS:
        if re.search(pattern, clean_query, re.IGNORECASE):
            disallowed_word = pattern.replace(r"\b", "")
            err = f"Security block: SQL keyword '{disallowed_word}' is prohibited."
            _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, err)
            return {"success": False, "error": err, "columns": [], "rows": [], "row_count": 0}

    # 2. Check if dataset exists
    db_path = get_db_path()
    if not db_path.exists() or get_transaction_count() == 0:
        err = "No transaction dataset loaded. Please upload a CSV file before running queries."
        _log_tool_execution("run_sql", inputs, "ERROR", 0.0, None, err)
        return {
            "success": False,
            "error": err,
            "columns": [],
            "rows": [],
            "row_count": 0
        }

    # 3. Execute query with read-only connection and timeout
    max_rows = 500
    timeout_seconds = 5.0

    try:
        conn = get_connection(read_only=True)

        # Set progress handler to enforce query execution timeout
        def progress_check():
            if time.time() - start_time > timeout_seconds:
                return 1  # Interrupts query
            return 0

        conn.set_progress_handler(progress_check, 1000)

        # Desanitize surrogate tokens back to real enterprise database values
        executable_query = vault.desanitize_query(clean_query)

        cursor = conn.cursor()
        cursor.execute(executable_query)

        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        # Fetch up to 501 rows to detect truncation
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        raw_rows = [list(r) for r in fetched[:max_rows]]

        conn.close()

        # Sanitize result rows: tokenize identifiers (txn_id) and mask BINs before sending to LLM
        sanitized_cols, sanitized_rows = vault.sanitize_sql_result(columns, raw_rows)

        elapsed_ms = (time.time() - start_time) * 1000
        result = {
            "success": True,
            "columns": sanitized_cols,
            "rows": sanitized_rows,
            "row_count": len(sanitized_rows),
            "truncated": truncated,
            "max_allowed_rows": max_rows,
            "execution_time_ms": round(elapsed_ms, 2),
            "zero_pii_sanitized": True
        }
        _log_tool_execution("run_sql", inputs, "SUCCESS", elapsed_ms, result)
        return result

    except sqlite3.OperationalError as op_err:
        elapsed_ms = (time.time() - start_time) * 1000
        err_str = str(op_err)
        if "interrupted" in err_str.lower():
            err_msg = f"Query timed out after {timeout_seconds}s limit."
        else:
            err_msg = f"SQL execution error: {err_str}"
        _log_tool_execution("run_sql", inputs, "ERROR", elapsed_ms, None, err_msg)
        return {"success": False, "error": err_msg, "columns": [], "rows": [], "row_count": 0}

    except Exception as exc:
        elapsed_ms = (time.time() - start_time) * 1000
        err_msg = f"Unexpected query error: {str(exc)}"
        _log_tool_execution("run_sql", inputs, "ERROR", elapsed_ms, None, err_msg)
        return {"success": False, "error": err_msg, "columns": [], "rows": [], "row_count": 0}


# ============================================================================
# TOOL 2: lookup_response_code
# ============================================================================

def lookup_response_code(code: str) -> Dict[str, Any]:
    """Look up ISO 8583 payment response code semantics, classification, and recommended actions.

    Identifies whether a decline is an approved transaction, a business decline, or a technical failure.
    Code 51 is strictly classified as a Business decline (never technical failure).

    Args:
        code: 2-digit ISO 8583 response code string (e.g. '00', '05', '51', '91', '96').

    Returns:
        Structured response with response code, meaning, category, responsible party, and action.
    """
    start_time = time.time()
    raw_code = str(code).strip() if code is not None else ""
    # Normalize 1-digit code (e.g. '0' or '5' -> '00', '05')
    clean_code = raw_code.zfill(2) if raw_code.isdigit() and len(raw_code) == 1 else raw_code
    inputs = {"code": code}

    _emit_tool_event({
        "type": "tool_start",
        "agent": "Gemini 3.8 Flash Analyst",
        "tool": "lookup_response_code",
        "stage": "extracting_data",
        "inputs": inputs,
        "message": f"Looking up ISO 8583 response code '{clean_code}' classification..."
    })

    if clean_code in RESPONSE_CODES_SPEC:
        spec = RESPONSE_CODES_SPEC[clean_code]
        result = {
            "response_code": clean_code,
            "meaning": spec["meaning"],
            "category": spec["category"],
            "likely_responsible_party": spec["likely_responsible_party"],
            "recommended_action": spec["recommended_action"],
            "is_known_code": True
        }
        elapsed_ms = (time.time() - start_time) * 1000
        _log_tool_execution("lookup_response_code", inputs, "SUCCESS", elapsed_ms, result)
        return result
    else:
        result = {
            "response_code": raw_code,
            "meaning": f"Unrecognized ISO 8583 response code '{raw_code}'",
            "category": "Unknown",
            "likely_responsible_party": "Unknown / Gateway Provider",
            "recommended_action": "Inspect gateway specification documents or raw transaction logs for proprietary response code mappings.",
            "is_known_code": False
        }
        elapsed_ms = (time.time() - start_time) * 1000
        _log_tool_execution("lookup_response_code", inputs, "SUCCESS", elapsed_ms, result)
        return result


# ============================================================================
# TOOL 3: get_baseline
# ============================================================================

def get_baseline(metric: str, dimension: str) -> Dict[str, Any]:
    """Calculate actual statistical baselines and dimensional decline metrics using SQL.

    Computes overall baseline across all transactions and group-level metrics across the specified dimension,
    including transaction counts and differences from baseline.

    Args:
        metric: One of:
            - 'approval_rate' (percentage of response_code = '00')
            - 'decline_rate' (percentage of response_code != '00')
            - 'code_91_rate' (percentage of response_code = '91')
            - 'code_51_rate' (percentage of response_code = '51')
            - 'three_ds_timeout_rate' (percentage of 3DS timeout outcomes)
            - 'average_latency' (mean latency in milliseconds)
            - 'transaction_volume' (transaction count)
        dimension: One of:
            - 'issuer_bank', 'network', 'channel', 'mcc', 'merchant_id', 'pa_id',
              'response_code', 'day_of_week', 'hour'

    Returns:
        Structured breakdown with overall baseline, total transactions, and group-level metrics.
    """
    start_time = time.time()
    inputs = {"metric": metric, "dimension": dimension}

    _emit_tool_event({
        "type": "tool_start",
        "agent": "Gemini 3.8 Flash Analyst",
        "tool": "get_baseline",
        "stage": "extracting_data",
        "inputs": inputs,
        "message": f"Extracting dimensional baseline for '{metric}' vs '{dimension}'..."
    })

    # 1. Validation of metric and dimension
    clean_metric = str(metric).strip().lower() if metric else ""
    clean_dimension = str(dimension).strip().lower() if dimension else ""

    if clean_metric not in SUPPORTED_METRICS:
        err = f"Unsupported metric '{metric}'. Supported metrics: {', '.join(SUPPORTED_METRICS)}"
        _log_tool_execution("get_baseline", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err}

    if clean_dimension not in SUPPORTED_DIMENSIONS:
        err = f"Unsupported dimension '{dimension}'. Supported dimensions: {', '.join(SUPPORTED_DIMENSIONS)}"
        _log_tool_execution("get_baseline", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err}

    # 2. Dataset existence check
    db_path = get_db_path()
    if not db_path.exists() or get_transaction_count() == 0:
        err = "No transaction dataset loaded. Please upload a dataset to compute baselines."
        _log_tool_execution("get_baseline", inputs, "ERROR", 0.0, None, err)
        return {"success": False, "error": err}

    # 3. Construct metric SQL expression
    if clean_metric == "approval_rate":
        metric_expr = "ROUND(100.0 * SUM(CASE WHEN response_code = '00' THEN 1 ELSE 0 END) / COUNT(*), 2)"
        unit = "%"
    elif clean_metric == "decline_rate":
        metric_expr = "ROUND(100.0 * SUM(CASE WHEN response_code != '00' THEN 1 ELSE 0 END) / COUNT(*), 2)"
        unit = "%"
    elif clean_metric == "code_91_rate":
        metric_expr = "ROUND(100.0 * SUM(CASE WHEN response_code = '91' THEN 1 ELSE 0 END) / COUNT(*), 2)"
        unit = "%"
    elif clean_metric == "code_51_rate":
        metric_expr = "ROUND(100.0 * SUM(CASE WHEN response_code = '51' THEN 1 ELSE 0 END) / COUNT(*), 2)"
        unit = "%"
    elif clean_metric == "three_ds_timeout_rate":
        metric_expr = "ROUND(100.0 * SUM(CASE WHEN UPPER(\"3ds_result\") LIKE '%TIMEOUT%' OR UPPER(\"3ds_result\") = 'TO' THEN 1 ELSE 0 END) / COUNT(*), 2)"
        unit = "%"
    elif clean_metric == "average_latency":
        metric_expr = "ROUND(AVG(latency_ms), 2)"
        unit = "ms"
    elif clean_metric == "transaction_volume":
        metric_expr = "COUNT(*)"
        unit = "txns"
    else:
        metric_expr = "COUNT(*)"
        unit = ""

    # 4. Construct dimension SQL expression
    if clean_dimension == "day_of_week":
        dim_expr = (
            "CASE CAST(strftime('%w', ts) AS INTEGER) "
            "WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday' "
            "WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday' "
            "WHEN 6 THEN 'Saturday' ELSE 'Unknown' END"
        )
    elif clean_dimension == "hour":
        dim_expr = "strftime('%H:00', ts)"
    elif clean_dimension == "response_code":
        dim_expr = "COALESCE(response_code, 'Unknown')"
    else:
        dim_expr = f"COALESCE({clean_dimension}, 'Unknown')"

    try:
        with get_connection(read_only=True) as conn:
            cursor = conn.cursor()

            # Query 1: Calculate overall baseline
            overall_sql = f"SELECT {metric_expr} AS overall_metric, COUNT(*) AS total_txns FROM transactions;"
            cursor.execute(overall_sql)
            overall_row = cursor.fetchone()
            overall_val = float(overall_row[0]) if overall_row and overall_row[0] is not None else 0.0
            total_txns = int(overall_row[1]) if overall_row and overall_row[1] is not None else 0

            # Query 2: Calculate group-level metrics
            group_sql = f"""
                SELECT {dim_expr} AS dim_value,
                       {metric_expr} AS group_metric,
                       COUNT(*) AS txn_count
                FROM transactions
                GROUP BY {dim_expr}
                ORDER BY txn_count DESC
                LIMIT 100;
            """
            cursor.execute(group_sql)
            group_rows = cursor.fetchall()

            groups = []
            for r in group_rows:
                dim_val = str(r[0]) if r[0] is not None else "Unknown"
                grp_metric = float(r[1]) if r[1] is not None else 0.0
                grp_count = int(r[2]) if r[2] is not None else 0
                diff = round(grp_metric - overall_val, 2)

                groups.append({
                    "dimension_value": dim_val,
                    "group_metric": grp_metric,
                    "transaction_count": grp_count,
                    "difference_from_baseline": diff
                })

            elapsed_ms = (time.time() - start_time) * 1000
            result = {
                "success": True,
                "metric": clean_metric,
                "dimension": clean_dimension,
                "unit": unit,
                "overall_baseline": overall_val,
                "total_transactions": total_txns,
                "groups": groups,
                "execution_time_ms": round(elapsed_ms, 2)
            }
            _log_tool_execution("get_baseline", inputs, "SUCCESS", elapsed_ms, result)
            return result

    except Exception as exc:
        elapsed_ms = (time.time() - start_time) * 1000
        err_msg = f"Error computing baseline: {str(exc)}"
        _log_tool_execution("get_baseline", inputs, "ERROR", elapsed_ms, None, err_msg)
        return {"success": False, "error": err_msg}


# ============================================================================
# TOOL 4: draft_action
# ============================================================================

def draft_action(
    problem: Optional[str] = None,
    evidence: Optional[str] = None,
    likely_cause: Optional[str] = None,
    cause_category: Optional[str] = None,
    responsible_party: Optional[str] = None,
    recommended_action: Optional[str] = None,
    suggested_owner: Optional[str] = None,
    priority: Optional[str] = None,
    estimated_approval_rate_uplift: Optional[str] = None,
    confidence: Optional[str] = None,
    validation_required: Optional[str] = None,
    item: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Draft an actionable remediation recommendation for an identified payment decline issue.

    This tool drafts structured recommendations with estimated uplifts and validation steps.
    It does NOT execute operational changes (read-only advisory output).

    Args:
        problem: Description of the decline anomaly or authorization problem.
        evidence: Data evidence, metrics, or decline code distributions supporting the finding.
        likely_cause: Root cause hypothesis identified by the analyst.
        cause_category: Classification of the cause. Must be one of:
            - 'Issuer technical'
            - 'Acquirer technical'
            - 'Payment aggregator technical'
            - 'Business decline'
            - 'Authentication issue'
            - 'Unknown'
        responsible_party: External entity or partner responsible (e.g., Chase, Visa, Gateway).
        recommended_action: Specific technical or operational change proposed.
        suggested_owner: Internal team or role to execute the action (e.g. Payments Eng, Risk Ops).
        priority: Priority level ('P0 - Critical', 'P1 - High', 'P2 - Medium', 'P3 - Low').
        estimated_approval_rate_uplift: Projected uplift in approval rate (e.g. '+1.5% (Estimated)').
        confidence: Confidence score or rating (e.g. 'High (85%)', 'Medium', 'Low').
        validation_required: Verification steps or A/B tests needed before full deployment.
        item: Optional dictionary containing all above fields.

    Returns:
        Structured recommendation draft dictionary.
    """
    start_time = time.time()

    # Extract from item dictionary if provided
    data = item or {}
    prob = problem or data.get("problem", "Unspecified payment decline anomaly")
    evid = evidence or data.get("evidence", "No statistical evidence provided")
    cause = likely_cause or data.get("likely_cause", "Pending diagnostic analysis")
    category_raw = cause_category or data.get("cause_category", "Unknown")
    resp_party = responsible_party or data.get("responsible_party", "Unassigned")
    action = recommended_action or data.get("recommended_action", "Investigate decline logs")
    owner = suggested_owner or data.get("suggested_owner", "Payments Product Manager")
    prio = priority or data.get("priority", "P1 - High")
    uplift_raw = estimated_approval_rate_uplift or data.get("estimated_approval_rate_uplift", "+0.0%")
    conf = confidence or data.get("confidence", "Medium")
    val_req = validation_required or data.get("validation_required", "Monitor authorization rates for 24 hours post-change")

    # Match cause category to supported set
    matched_category = "Unknown"
    for cat in VALID_CAUSE_CATEGORIES:
        if str(category_raw).strip().lower() == cat.lower():
            matched_category = cat
            break

    # Clearly label uplift as an estimate
    uplift_str = str(uplift_raw).strip()
    if not uplift_str.endswith("(Estimated)"):
        formatted_uplift = f"{uplift_str} (Estimated)"
    else:
        formatted_uplift = uplift_str

    result = {
        "status": "draft",
        "execution_mode": "advisory_only_no_operational_execution",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "problem": str(prob),
        "evidence": str(evid),
        "likely_cause": str(cause),
        "cause_category": matched_category,
        "responsible_party": str(resp_party),
        "recommended_action": str(action),
        "suggested_owner": str(owner),
        "priority": str(prio),
        "estimated_approval_rate_uplift": formatted_uplift,
        "is_estimate": True,
        "confidence": str(conf),
        "validation_required": str(val_req)
    }

    # Register in Human Approval Framework (Phase 6 Governance)
    try:
        from app.security.approval import approval_store
        proposal = approval_store.create_proposal(
            title=str(prob),
            recommended_action=str(action),
            problem=str(prob),
            evidence=str(evid),
            likely_cause=str(cause),
            expected_impact=formatted_uplift,
            suggested_owner=str(owner),
            priority=str(prio),
        )
        result["proposal_id"] = proposal.get("proposal_id")
        result["approval_status"] = proposal.get("status")
        result["governance_note"] = proposal.get("governance_note")
    except Exception as exc:
        pass

    _emit_tool_event({
        "type": "finding_drafted",
        "agent": "Gemini 3.8 Flash Analyst",
        "tool": "draft_action",
        "stage": "drafting_action",
        "finding": result,
        "message": f"Drafted action playbook: {prob} (Est. uplift: {formatted_uplift})"
    })

    elapsed_ms = (time.time() - start_time) * 1000
    _log_tool_execution("draft_action", {"problem": prob, "category": matched_category}, "SUCCESS", elapsed_ms, result)
    return result


# Export list of callable functions for the Google Gen AI SDK
AVAILABLE_TOOLS = [
    run_sql,
    lookup_response_code,
    get_baseline,
    draft_action,
]
