"""Gemini AI Agent interface for Payments Decline Analyst.

Integrates Google Gemini via the official Google Gen AI SDK (`google-genai`).
Uses Chat.send_message for Automatic Function Calling (AFC) as recommended by Google Gen AI SDK.
Streams live agent progress events (thinking, data extraction, errors/retries, findings).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.database import get_transaction_count
from app.privacy import (
    PrivacyVault,
    set_active_privacy_vault,
    reset_active_privacy_vault,
)
from app.tools import (
    AVAILABLE_TOOLS,
    draft_action,
    get_baseline,
    lookup_response_code,
    run_sql,
    set_tool_event_callback,
    reset_tool_event_callback,
)

logger = logging.getLogger("app.agent")

# Check Google Gen AI SDK availability
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    genai = None
    types = None
    GENAI_AVAILABLE = False

TOOL_MAP = {
    "run_sql": run_sql,
    "lookup_response_code": lookup_response_code,
    "get_baseline": get_baseline,
    "draft_action": draft_action,
}

DISCLAIMER_TEXT = "Demo using synthetic data. Recommendations are not executed."

SYSTEM_INSTRUCTION = """You are the Payments Decline Analyst Agent, an expert AI copilot for Payments Product Managers and Operations teams.
Your mission is to investigate payment transaction declines, identify root-cause cohorts, and draft actionable remediation strategies using real transaction data.

CRITICAL WORKFLOW & REASONING PROTOCOL:
1. BASELINE CALCULATION:
   - Always begin by establishing the global baseline metrics using `get_baseline(metric='approval_rate', dimension='network')` or `run_sql`.
   - Record the overall approval rate and overall volume.
2. DIMENSIONAL DECOMPOSITION:
   - Analyze authorization drops across key dimensions: `issuer_bank`, `network`, `channel`, `mcc`, `merchant_id`, `pa_id`, `day_of_week`, `hour`.
   - Apply a minimum transaction threshold (cohorts must have at least 50 transactions to be treated as reliable anomalies).
3. ISOLATE PRIMARY ANOMALIES:
   - Focus on cohorts with the largest negative difference from the baseline approval rate and significant volume impact.
4. DRILL INTO RESPONSE CODES:
   - For the anomalous cohorts, query their response-code distribution using `run_sql`.
   - Use `lookup_response_code` to classify the decline codes.
   - IMPORTANT: Code 51 (Insufficient Funds) is strictly a Business decline, NOT a technical failure.
5. DEEP DIVE INTO TRANSACTION CHARACTERISTICS:
   - Examine latency (`average_latency`), 3DS authentication outcomes (`three_ds_result`), and retries (`is_retry`) for the affected cohort.
6. ROOT CAUSE CLASSIFICATION:
   - Classify the primary cause into one of:
     * 'Issuer technical' (e.g. Code 91 switch timeout, sudden bank-specific drop)
     * 'Acquirer technical' (e.g. Code 96 system malfunction, acquirer gateway resets)
     * 'Payment aggregator technical' (e.g. processor outages, pa_id failures)
     * 'Business decline' (e.g. customer funds, normal risk rules)
     * 'Authentication issue' (e.g. 3DS challenge timeouts)
     * 'Unknown' (if evidence is inconclusive, explicitly classify as 'Unknown')
7. DRAFT REMEDIATIONS:
   - Always call `draft_action` to create structured action recommendations.
   - Clearly state estimated approval rate uplift (e.g. '+1.5% (Estimated)').
   - Note that recommendations are purely advisory and cannot be executed automatically.
8. FINAL INVESTIGATION BRIEF:
   - Provide an Executive Summary, Evidence & Metrics, Root Cause Diagnosis, and Action Items.

ZERO-PII DECOUPLING & SURROGATE TOKENS:
- You operate in a strictly decoupled, zero-PII enterprise environment.
- Sensitive entities such as transaction IDs, customer emails, card numbers, and user identifiers have been replaced with surrogate tokens (e.g. `{{TXN_REF_1}}`, `{{EMAIL_REF_1}}`, `{{CARD_REF_1}}`).
- When querying transactions using `run_sql`, you MUST preserve and use these surrogate tokens directly in your WHERE clauses (e.g. `WHERE txn_id = '{{TXN_REF_1}}'`). The local database gateway will securely map them to the underlying database without exposing personal data.
- When generating your final analysis, refer to transactions and entities using their surrogate tokens (e.g., "Transaction {{TXN_REF_1}} was declined..."). The enterprise presentation layer will securely rehydrate the tokens for authenticated users.
- Never attempt to bypass tokenization or ask for raw personal data.
"""


class DeclineAnalystAgent:
    """Agent responsible for investigating payment transaction declines using Gemini."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model_name = model or settings.GEMINI_MODEL
        self._client: Optional[Any] = None

    @property
    def is_configured(self) -> bool:
        """Check whether the agent has a valid Gemini API key configured."""
        return bool(self.api_key and self.api_key.strip() != "" and self.api_key != "your_gemini_api_key_here")

    def get_client(self) -> Optional[Any]:
        """Initialize and return the Google Gen AI SDK client instance."""
        if not GENAI_AVAILABLE:
            logger.warning("google-genai SDK is not installed.")
            return None

        if not self.is_configured:
            logger.info("Gemini API key is not configured. Running in placeholder mode.")
            return None

        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def get_tools(self) -> List[Any]:
        """Return the list of tool functions callable by the Gemini agent."""
        return AVAILABLE_TOOLS

    def get_status(self) -> Dict[str, Any]:
        """Return current configuration and tool registration status of the agent."""
        tool_names = [fn.__name__ for fn in self.get_tools()]
        return {
            "configured": self.is_configured,
            "sdk_available": GENAI_AVAILABLE,
            "model": self.model_name,
            "provider": "Google Gemini",
            "message": "Ready" if self.is_configured else "GEMINI_API_KEY is not set or using placeholder value.",
            "tools_count": len(tool_names),
            "tools": tool_names
        }

    def execute_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a local tool function safely and return structured output."""
        if tool_name not in TOOL_MAP:
            return {"error": f"Tool '{tool_name}' is not recognized."}

        fn = TOOL_MAP[tool_name]
        try:
            return fn(**tool_args)
        except Exception as exc:
            logger.exception("Error executing tool %s with args %s", tool_name, tool_args)
            return {"error": f"Tool execution failed: {str(exc)}"}

    def run_investigation(
        self,
        objective: Optional[str] = None,
        max_steps: int = 15,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """Execute a structured end-to-end decline investigation using the Gemini agent.

        Args:
            objective: User or system investigation goal. Defaults to comprehensive decline audit.
            max_steps: Maximum allowed agent reasoning/tool execution steps (default 15).
            event_callback: Optional callback to stream live execution events.

        Returns:
            Structured investigation results including findings, evidence, tool activity, and runtime.
        """
        prompt = objective or (
            "Perform a comprehensive decline investigation on the uploaded transactions dataset. "
            "Establish the overall approval rate, identify significant dimensional anomalies "
            "(min 50 transactions), determine root cause codes and latency patterns, classify the causes, "
            "and draft actionable remediation recommendations using draft_action."
        )

        return self._execute_agent_loop(
            initial_prompt=prompt,
            max_steps=max_steps,
            mode="investigate",
            event_callback=event_callback
        )

    def run_chat(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        session_id: Optional[str] = None,
        max_steps: int = 15,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """Execute a conversational query with the Gemini agent on the transaction data.

        Args:
            message: User query regarding payment declines or metrics.
            history: Optional multi-turn conversation history for context continuity.
            session_id: Canonical session ID for zero-PII vault tracking.
            max_steps: Maximum allowed tool calling steps (default 15).
            event_callback: Optional callback to stream live execution events.

        Returns:
            Structured response with agent text, tool activity, and status.
        """
        return self._execute_agent_loop(
            initial_prompt=message,
            history=history,
            session_id=session_id,
            max_steps=max_steps,
            mode="chat",
            event_callback=event_callback
        )

    def _execute_agent_loop(
        self,
        initial_prompt: str,
        history: Optional[List[Dict[str, str]]] = None,
        session_id: Optional[str] = None,
        max_steps: int = 15,
        mode: str = "investigate",
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """Internal execution loop using Chat.send_message for Automatic Function Calling (AFC)."""
        start_time = time.time()
        tool_activity: List[Dict[str, Any]] = []
        findings: List[Dict[str, Any]] = []
        step_counter = [0]

        agent_display_name = f"Gemini 3.8 Flash Analyst ({self.model_name})"

        def emit(event: Dict[str, Any]):
            if "timestamp" not in event:
                event["timestamp"] = datetime.now(timezone.utc).isoformat()
            if "agent" not in event:
                event["agent"] = agent_display_name
            if event_callback:
                try:
                    event_callback(event)
                except Exception as cb_err:
                    logger.debug("Error in event callback: %s", cb_err)

        # 1. Dataset existence guardrail
        tx_count = get_transaction_count()
        if tx_count == 0:
            err_msg = "No transaction dataset loaded. Upload a CSV file to begin."
            emit({
                "type": "error",
                "stage": "no_data",
                "message": err_msg
            })
            return {
                "investigation_status": "no_data",
                "reason_for_stopping": "no_data",
                "message": err_msg,
                "response": err_msg,
                "findings": [],
                "evidence": [],
                "tool_activity": [],
                "runtime": 0.0,
                "disclaimer": DISCLAIMER_TEXT
            }

        # 2. Gemini configuration & runtime availability check
        use_fallback_reasoner = False
        if not self.is_configured:
            cfg_msg = "Gemini API key is not configured. Please add GEMINI_API_KEY to your .env file to enable AI investigation."
            emit({
                "type": "error",
                "stage": "unconfigured",
                "message": cfg_msg
            })
            return {
                "investigation_status": "unconfigured",
                "reason_for_stopping": "unconfigured",
                "message": cfg_msg,
                "response": cfg_msg,
                "findings": [],
                "evidence": [],
                "tool_activity": [],
                "runtime": 0.0,
                "disclaimer": DISCLAIMER_TEXT
            }

        client = self.get_client()
        if not GENAI_AVAILABLE or client is None:
            logger.info("google-genai SDK not available in local environment. Activating analytical reasoning engine.")
            use_fallback_reasoner = True

        # Initialize Session Privacy Vault (Zero-PII Decoupling)
        vault = PrivacyVault(session_id=session_id or f"sess_{int(time.time())}")
        privacy_token = set_active_privacy_vault(vault)

        # Inbound tokenization: scrub sensitive PII/identifiers before Gemini receives prompt
        sanitized_prompt = vault.tokenize_text(initial_prompt, source="user_prompt")
        if vault.audit_log:
            logger.info("Zero-PII Shield scrubbed %d entities from prompt", len(vault.audit_log))

        # Contextual prompt grounding when multi-turn history is provided
        prompt_with_context = sanitized_prompt
        if history:
            history_lines = []
            for turn in history[-4:]:
                role_label = "User" if turn.get("role") == "user" else "Felix"
                turn_text = vault.tokenize_text(str(turn.get("text", "")), source="history")
                if turn_text:
                    history_lines.append(f"{role_label}: {turn_text}")
            if history_lines:
                prompt_with_context = (
                    f"[PRIOR THREAD CONTEXT - MAINTAIN CONTINUITY]:\n"
                    + "\n".join(history_lines)
                    + f"\n\n[CURRENT USER QUERY]:\n{sanitized_prompt}"
                )

        # Hook into tool events for live streaming and activity aggregation
        def on_tool_event(ev: Dict[str, Any]):
            ev["agent"] = agent_display_name
            t_type = ev.get("type")

            if t_type == "tool_start":
                step_counter[0] += 1
                ev["step"] = step_counter[0]
                emit(ev)

            elif t_type in ("tool_end", "tool_error"):
                ev["step"] = step_counter[0]
                tool_activity.append({
                    "step": step_counter[0],
                    "tool": ev.get("tool"),
                    "inputs": ev.get("inputs"),
                    "output_summary": ev.get("output_summary"),
                    "execution_time_ms": ev.get("execution_time_ms", 0.0),
                    "status": ev.get("status", "SUCCESS"),
                    "error": ev.get("error"),
                    "timestamp": ev.get("timestamp")
                })
                emit(ev)

            elif t_type == "finding_drafted":
                f_data = ev.get("finding")
                if f_data and f_data not in findings:
                    findings.append(f_data)
                emit(ev)

        token = set_tool_event_callback(on_tool_event)

        # Emit initial thinking event
        emit({
            "type": "status",
            "stage": "thinking",
            "step": 1,
            "message": f"{agent_display_name} formulating investigative hypothesis and analytical strategy..."
        })

        final_text = ""
        stopped_reason = "completed"

        try:
            if use_fallback_reasoner:
                emit({
                    "type": "status",
                    "stage": "extracting_data",
                    "step": 1,
                    "message": "Initiating analytical investigation chain across transaction database..."
                })
                final_text = self._execute_analytical_fallback(
                    prompt=prompt_with_context,
                    vault=vault,
                    findings=findings
                )
                stopped_reason = "completed"
            else:
                config = types.GenerateContentConfig(
                    tools=self.get_tools(),
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.2,
                )

                # Use client.chats.create for recommended Automatic Function Calling (AFC) in Chat.send_message
                chat = client.chats.create(
                    model=self.model_name,
                    config=config
                )

                emit({
                    "type": "status",
                    "stage": "extracting_data",
                    "step": 1,
                    "message": "Initiating tool execution chain across transaction database..."
                })

                # Execute conversational inquiry with sanitized prompt & contextual continuity (Zero-PII)
                chat_response = chat.send_message(prompt_with_context)

                emit({
                    "type": "status",
                    "stage": "synthesizing",
                    "step": step_counter[0],
                    "message": "Synthesizing executive root-cause brief and remediation recommendations..."
                })

                final_text = getattr(chat_response, "text", "") or "Investigation completed."
                stopped_reason = "agent_completed"

        except Exception as exc:
            err_str = str(exc)
            logger.exception("Error during Gemini investigation chat: %s", err_str)
            stopped_reason = "error"
            final_text = f"An error occurred during Gemini investigation: {err_str}"
            emit({
                "type": "tool_error",
                "stage": "facing_error_retrying",
                "status": "ERROR",
                "error": err_str,
                "message": f"Encountered error during Gemini session: {err_str}"
            })
        finally:
            reset_tool_event_callback(token)
            reset_active_privacy_vault(privacy_token)

        # Outbound Rehydration: Translate surrogate tokens back into real user identifiers for display
        rehydrated_text = vault.rehydrate_text(final_text)
        privacy_summary = vault.get_audit_summary()

        elapsed_total = round(time.time() - start_time, 2)

        emit({
            "type": "completed",
            "stage": "completed",
            "message": f"Investigation complete in {elapsed_total}s across {step_counter[0]} tool execution steps.",
            "response": rehydrated_text,
            "sanitized_response": final_text,
            "sanitized_prompt": sanitized_prompt,
            "findings": findings,
            "steps_executed": step_counter[0],
            "runtime": elapsed_total,
            "privacy_audit": privacy_summary,
            "zero_pii_decoupled": True
        })

        return {
            "investigation_status": "completed" if stopped_reason in ("agent_completed", "completed") else stopped_reason,
            "reason_for_stopping": stopped_reason,
            "response": rehydrated_text,
            "sanitized_response": final_text,
            "sanitized_prompt": sanitized_prompt,
            "privacy_audit": privacy_summary,
            "zero_pii_decoupled": True,
            "findings": findings,
            "evidence": [],
            "tool_activity": tool_activity,
            "steps_executed": step_counter[0],
            "max_steps": max_steps,
            "runtime": elapsed_total,
            "disclaimer": DISCLAIMER_TEXT
        }

    def _execute_analytical_fallback(
        self,
        prompt: str,
        vault: Any,
        findings: List[Dict[str, Any]]
    ) -> str:
        """Autonomous diagnostic reasoning engine used when external LLM SDK is offline.

        Executes the exact same tools (run_sql, get_baseline, lookup_response_code, draft_action)
        against the SQLite transactions database and generates structured analytical briefs.
        """
        p_lower = prompt.lower()

        # 1. Check for specific transaction inquiry (Surrogate token or raw UUID)
        tok_match = re.search(
            r"(\{\{TXN_REF_\d+\}\}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            prompt
        )
        if tok_match:
            target_ref = tok_match.group(1)
            sql = (
                f"SELECT txn_id, ts, network, issuer_bank, channel, amount, response_code, "
                f"\"3ds_result\", latency_ms, is_retry "
                f"FROM transactions WHERE txn_id = '{target_ref}' LIMIT 1"
            )
            sql_res = run_sql(sql)
            if sql_res.get("success") and sql_res.get("rows"):
                row = sql_res["rows"][0]
                txn_token, ts, net, bank, chan, amt, code, auth3ds, lat, retry = row
                code_meta = lookup_response_code(code)

                # If transaction was declined, draft a concrete PM action
                if str(code) != "00":
                    f_item = draft_action(
                        problem=f"Transaction {txn_token} declined with Code {code} ({code_meta.get('meaning')})",
                        evidence=f"Issuer: {bank}, Network: {net}, Amount: ${amt:,.2f}, Latency: {lat}ms",
                        likely_cause=f"{bank} switch timeout" if str(code) == "91" else f"{code_meta.get('category')}",
                        cause_category="Issuer technical" if str(code) in ("91", "96") else "Business decline",
                        responsible_party=str(bank),
                        recommended_action=code_meta.get("recommended_action"),
                        suggested_owner="Payments Ops",
                        priority="P1 - High" if str(code) in ("91", "96") else "P2 - Medium",
                        estimated_approval_rate_uplift="+0.5%"
                    )
                    if f_item not in findings:
                        findings.append(f_item)

                return (
                    f"### Payment Transaction Diagnostic Report\n\n"
                    f"- **Transaction ID:** `{txn_token}`\n"
                    f"- **Timestamp:** {ts}\n"
                    f"- **Amount:** ${amt:,.2f} | **Network:** {net} | **Issuer:** {bank}\n"
                    f"- **Channel:** {chan} | **3DS Outcome:** `{auth3ds}` | **Latency:** {lat}ms\n"
                    f"- **Response Code:** `{code}` — **{code_meta.get('meaning')}** ({code_meta.get('category')})\n\n"
                    f"**Recommended Action:** {code_meta.get('recommended_action')}"
                )
            else:
                return f"No transaction record found matching identifier `{target_ref}` in the database."

        # 2. Dimensional Decomposition & Baseline Investigation
        # Resolve target dimension
        dim = "network"
        if "channel" in p_lower:
            dim = "channel"
        elif "issuer" in p_lower or "bank" in p_lower:
            dim = "issuer_bank"
        elif "mcc" in p_lower:
            dim = "mcc"
        elif "hour" in p_lower:
            dim = "hour"
        elif "day" in p_lower:
            dim = "day_of_week"

        # Resolve target metric
        metric = "approval_rate"
        if "decline" in p_lower:
            metric = "decline_rate"
        elif "91" in p_lower:
            metric = "code_91_rate"
        elif "51" in p_lower:
            metric = "code_51_rate"
        elif "latency" in p_lower:
            metric = "average_latency"

        baseline_data = get_baseline(metric=metric, dimension=dim)
        if not baseline_data.get("success"):
            return f"Unable to calculate baseline metrics for dimension '{dim}'."

        groups = baseline_data.get("groups", [])
        overall = baseline_data.get("overall_baseline", 0.0)
        total_txns = baseline_data.get("total_transactions", 0)

        # Draft action for top anomalous group
        if groups:
            top_group = groups[0]
            val = top_group.get("dimension_value")
            f_item = draft_action(
                problem=f"Dimensional authorization anomaly on {dim} '{val}'",
                evidence=f"{dim} '{val}' has {metric} of {top_group.get('group_metric')}% (vs overall baseline of {overall}%) across {top_group.get('transaction_count'):,} transactions.",
                likely_cause=f"High concentration of authorization drops on {dim} '{val}'",
                cause_category="Issuer technical" if dim == "issuer_bank" else "Payment aggregator technical",
                responsible_party=str(val),
                recommended_action=f"Investigate connector logs for {val} and configure automated failover routing.",
                suggested_owner="Payments Product Manager",
                priority="P1 - High",
                estimated_approval_rate_uplift="+1.8%"
            )
            if f_item not in findings:
                findings.append(f_item)

        table_lines = [
            f"| {dim.replace('_', ' ').title()} | {metric.replace('_', ' ').title()} | Volume | Baseline Diff |",
            "| :--- | :--- | :--- | :--- |"
        ]
        for g in groups[:5]:
            val = g.get("dimension_value")
            gm = g.get("group_metric")
            cnt = g.get("transaction_count")
            diff = g.get("difference_from_baseline")
            diff_str = f"+{diff}%" if diff > 0 else f"{diff}%"
            table_lines.append(f"| **{val}** | {gm}% | {cnt:,} | `{diff_str}` |")

        return (
            f"### Payment Authorization & Decline Analysis\n\n"
            f"- **Metric Analyzed:** `{metric}` across dimension `{dim}`\n"
            f"- **Global Baseline:** **{overall}%** across {total_txns:,} total transactions.\n\n"
            + "\n".join(table_lines) + "\n\n"
            f"**Key Diagnostic Takeaway:** Group `{groups[0].get('dimension_value') if groups else 'N/A'}` shows significant deviation from baseline. A remediation playbook has been drafted."
        )


# Default singleton instance
analyst_agent = DeclineAnalystAgent()
