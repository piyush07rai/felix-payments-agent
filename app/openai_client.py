"""OpenAI ChatGPT Client & Function-Calling Runner for Felix.

Supports OpenAI models (e.g. gpt-4o-mini, gpt-4o) with autonomous multi-turn tool calling.
Emits real-time progress callbacks for every step (running SQL, ISO code lookup, baselines),
logs request/response back-and-forth for log drains, and works seamlessly with either
the official `openai` Python SDK or standard library HTTP fallback.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.audit_logging import log_lifecycle_event
from app.config import settings

logger = logging.getLogger("app.openai")

try:
    import openai
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    openai = None
    OPENAI_SDK_AVAILABLE = False


OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Execute a read-only SQL query on the transactions table in SQLite. Returns columns, rows, and execution time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL SELECT query string to run on the 'transactions' table."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_response_code",
            "description": "Lookup ISO 8583 payment response code definition, category, and classification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "2-digit ISO 8583 response code (e.g. '05', '51', '91')."
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_baseline",
            "description": "Calculate baseline payment authorization rates across dimensions (network, issuer_bank, channel).",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Metric to calculate baseline for (e.g. 'approval_rate', 'volume')."
                    },
                    "dimension": {
                        "type": "string",
                        "description": "Dimension to group by (e.g. 'network', 'issuer_bank', 'channel')."
                    }
                },
                "required": ["metric", "dimension"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "draft_action",
            "description": "Draft a structured remediation recommendation for Payment Product Managers and Operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string", "description": "Concise statement of payment authorization problem identified."},
                    "evidence": {"type": "string", "description": "Supporting metric evidence (drop in %, affected volume, cohort)."},
                    "likely_cause": {"type": "string", "description": "Diagnosed root cause explanation."},
                    "cause_category": {
                        "type": "string",
                        "description": "Category: 'Issuer technical', 'Acquirer technical', 'Payment aggregator technical', 'Business decline', 'Authentication issue', or 'Unknown'."
                    },
                    "responsible_party": {"type": "string", "description": "Entity responsible: e.g. 'Issuer Bank', 'Acquirer', 'Aggregator'."},
                    "recommended_action": {"type": "string", "description": "Concrete operational or engineering action step."},
                    "suggested_owner": {"type": "string", "description": "Team to own remediation: 'Payments Ops', 'Eng', 'Risk', 'PM'."},
                    "priority": {"type": "string", "description": "Priority: 'P0', 'P1', 'P2', 'P3'."},
                    "estimated_approval_rate_uplift": {"type": "string", "description": "Estimated uplift, e.g. '+1.5% (Estimated)'."}
                },
                "required": ["problem", "likely_cause", "recommended_action"]
            }
        }
    }
]


class OpenAIAnalystClient:
    """Client for executing Decline Analyst reasoning using OpenAI models."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model_name = model or settings.OPENAI_MODEL or "gpt-4o-mini"

    @property
    def is_configured(self) -> bool:
        """Check whether a valid OpenAI API key is configured."""
        return bool(self.api_key and self.api_key.strip() != "" and self.api_key != "your_openai_api_key_here")

    def _call_chat_api(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: float = 18.0
    ) -> Dict[str, Any]:
        """Make HTTP request to OpenAI Chat Completions API."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key.strip()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            body = http_err.read().decode("utf-8") if http_err.fp else ""
            err_msg = f"OpenAI HTTP {http_err.code}: {body or http_err.reason}"
            logger.error(err_msg)
            raise RuntimeError(err_msg) from http_err
        except Exception as exc:
            logger.error("OpenAI request error: %s", exc)
            raise RuntimeError(f"OpenAI connection error: {str(exc)}") from exc

    def execute_reasoning_loop(
        self,
        system_instruction: str,
        user_prompt: str,
        history: Optional[List[Dict[str, str]]] = None,
        tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        max_steps: int = 15,
        step_counter: Optional[List[int]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        findings: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Run the autonomous OpenAI function calling loop.

        Returns (final_response_text, tool_activity_list).
        """
        if not self.is_configured:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        step_counter = step_counter or [0]
        tool_activity: List[Dict[str, Any]] = []
        findings = findings if findings is not None else []

        def emit(event: Dict[str, Any]):
            if "agent" not in event:
                event["agent"] = f"OpenAI Analyst ({self.model_name})"
            if "agent_name" not in event:
                event["agent_name"] = event["agent"]
            if "action_text" not in event and "message" in event:
                event["action_text"] = event["message"]
            if event_callback:
                try:
                    event_callback(event)
                except Exception as cb_err:
                    logger.debug("Error in event callback: %s", cb_err)

        # Build initial messages
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_instruction}
        ]

        if history:
            for turn in history[-4:]:
                r = "user" if turn.get("role") == "user" else "assistant"
                messages.append({"role": r, "content": str(turn.get("text", ""))})

        messages.append({"role": "user", "content": user_prompt})

        log_lifecycle_event(
            "LLM_DISPATCH",
            f"Dispatching query to OpenAI ({self.model_name})",
            {"model": self.model_name, "prompt_preview": user_prompt[:80]}
        )

        loop_count = 0
        final_text = ""

        while loop_count < max_steps:
            loop_count += 1
            start_req_time = time.time()

            api_resp = self._call_chat_api(
                messages=messages,
                tools=OPENAI_TOOLS,
                timeout=settings.LLM_TIMEOUT_SECONDS
            )

            req_latency = round((time.time() - start_req_time) * 1000, 1)
            choices = api_resp.get("choices") or []
            if not choices:
                raise RuntimeError("OpenAI returned empty choices in completion.")

            choice = choices[0]
            message_obj = choice.get("message") or {}
            content = message_obj.get("content") or ""
            tool_calls = message_obj.get("tool_calls") or []

            log_lifecycle_event(
                "LLM_BACK_AND_FORTH",
                f"OpenAI returned turn #{loop_count} in {req_latency}ms (tools requested: {len(tool_calls)})",
                {"turn": loop_count, "latency_ms": req_latency, "has_content": bool(content), "tool_count": len(tool_calls)}
            )

            # If no tools requested, we have reached the final synthesized answer
            if not tool_calls:
                final_text = content
                break

            # Append assistant message with tool calls to history
            messages.append(message_obj)

            # Execute requested tools
            for tc in tool_calls:
                call_id = tc.get("id")
                fn_meta = tc.get("function") or {}
                fn_name = fn_meta.get("name")
                fn_args_raw = fn_meta.get("arguments") or "{}"

                try:
                    fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                except Exception:
                    fn_args = {}

                step_counter[0] += 1
                curr_step = step_counter[0]

                # Generate human-readable action description
                if fn_name == "run_sql":
                    q_prev = str(fn_args.get("query", "")).strip()[:90]
                    human_action = f"Running SQL query on transactions: {q_prev}..."
                elif fn_name == "lookup_response_code":
                    human_action = f"Looking up ISO 8583 response code '{fn_args.get('code')}'..."
                elif fn_name == "get_baseline":
                    human_action = f"Extracting dimensional baseline for {fn_args.get('metric')} vs {fn_args.get('dimension')}..."
                elif fn_name == "draft_action":
                    human_action = f"Drafting PM remediation action for '{fn_args.get('problem', '')[:60]}...' "
                else:
                    human_action = f"Executing analytical tool '{fn_name}'..."

                emit({
                    "type": "tool_start",
                    "step": curr_step,
                    "max_steps": max_steps,
                    "stage": "extracting_data",
                    "tool": fn_name,
                    "inputs": fn_args,
                    "action_text": human_action,
                    "message": human_action
                })

                log_lifecycle_event(
                    "TOOL_EXECUTION",
                    f"Executing tool {fn_name} for OpenAI agent (Step {curr_step})",
                    {"step": curr_step, "tool": fn_name, "args": fn_args}
                )

                tool_start_t = time.time()
                tool_result: Dict[str, Any] = {}
                tool_error: Optional[str] = None

                try:
                    if tool_executor:
                        tool_result = tool_executor(fn_name, fn_args)
                    else:
                        tool_error = f"No tool executor registered for {fn_name}"
                except Exception as t_err:
                    tool_error = str(t_err)
                    tool_result = {"error": tool_error}

                tool_elapsed_ms = round((time.time() - tool_start_t) * 1000, 2)

                # Record activity
                activity_item = {
                    "step": curr_step,
                    "tool": fn_name,
                    "inputs": fn_args,
                    "output_summary": tool_result if not isinstance(tool_result, dict) else {
                        k: (v if k not in ("rows", "groups") else f"[{len(v)} items]")
                        for k, v in tool_result.items()
                    },
                    "raw_result": tool_result,
                    "execution_time_ms": tool_elapsed_ms,
                    "status": "ERROR" if tool_error else "SUCCESS",
                    "error": tool_error,
                    "action_text": f"Completed {fn_name} in {tool_elapsed_ms}ms."
                }
                tool_activity.append(activity_item)

                if fn_name == "draft_action" and not tool_error:
                    findings.append(tool_result)
                    emit({
                        "type": "finding_drafted",
                        "step": curr_step,
                        "stage": "drafting_action",
                        "finding": tool_result,
                        "action_text": "PM Remediation Action Proposal drafted.",
                        "message": "PM Remediation Action Proposal drafted."
                    })

                emit({
                    "type": "tool_end" if not tool_error else "tool_error",
                    "step": curr_step,
                    "max_steps": max_steps,
                    "stage": "analyzing_results" if not tool_error else "facing_error_retrying",
                    "tool": fn_name,
                    "status": "ERROR" if tool_error else "SUCCESS",
                    "execution_time_ms": tool_elapsed_ms,
                    "error": tool_error,
                    "action_text": f"Finished {fn_name} in {tool_elapsed_ms}ms.",
                    "message": f"Finished {fn_name} in {tool_elapsed_ms}ms."
                })

                # Append tool response message for OpenAI
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(tool_result)
                })

        if not final_text:
            # Request explicit synthesis turn using gathered tool evidence
            try:
                messages.append({
                    "role": "user",
                    "content": (
                        "You have completed all tool queries. Now provide a crisp, dense Executive Analysis for the Payments PM: "
                        "1. Executive Summary: State the bottom-line finding in 1-2 punchy sentences. "
                        "2. Metrics & Evidence: Formatted table comparing key cohorts against baseline. "
                        "3. Root Cause Diagnosis: Classify the failure (e.g. Issuer technical, Acquirer, Business decline) and explain why. "
                        "4. Recommended Actions: Give 1-2 prioritized remediation playbooks with expected uplift. "
                        "Be concise, avoid repetitive fluff or robotic filler."
                    )
                })
                synth_resp = self._call_chat_api(messages=messages, tools=None, timeout=settings.LLM_TIMEOUT_SECONDS)
                s_choices = synth_resp.get("choices") or []
                if s_choices:
                    final_text = (s_choices[0].get("message", {}).get("content", "") or "").strip()
            except Exception as s_err:
                logger.warning("Error in OpenAI synthesis turn: %s", s_err)

        if not final_text and choices:
            final_text = choices[0].get("message", {}).get("content", "") or "Analysis complete."

        return final_text, tool_activity
