"""Evaluation module for Payments Decline Analyst Agent.

Compares completed agent findings against ground-truth benchmarks.
STRICT ISOLATION: The AI agent never reads, queries, or accesses this module or ground_truth.json.
Only this module reads ground truth to calculate diagnostic precision, recall, and cost metrics.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("app.evaluation")

GROUND_TRUTH_FILENAME = "ground_truth.json"

# Pricing estimates for Gemini 3.8 Flash per 1M tokens (advisory cost tracking)
GEMINI_FLASH_INPUT_COST_PER_M = 0.15
GEMINI_FLASH_OUTPUT_COST_PER_M = 0.60
ESTIMATED_TOKENS_PER_STEP = 1200


def get_ground_truth_path() -> Path:
    """Return the filesystem path to the ground truth JSON file."""
    db_path = Path(settings.DATABASE_PATH)
    return db_path.parent / GROUND_TRUTH_FILENAME


class AgentEvaluator:
    """Evaluates agent investigation findings against verified ground-truth datasets."""

    def __init__(self):
        self.latest_evaluation: Optional[Dict[str, Any]] = None

    def is_ground_truth_available(self) -> bool:
        """Check if ground_truth.json exists and is readable."""
        gt_path = get_ground_truth_path()
        return gt_path.exists() and gt_path.is_file() and gt_path.stat().st_size > 0

    def load_ground_truth(self) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
        """Load and normalize ground-truth issues from disk.
        
        Supports list of issue dicts, or wrapped dict {"issues": [...]} / {"anomalies": [...]}.
        """
        if not self.is_ground_truth_available():
            return False, [], "Evaluation unavailable. Upload the ground-truth file to evaluate the investigation."

        gt_path = get_ground_truth_path()
        try:
            with open(gt_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                issues = data
            elif isinstance(data, dict):
                issues = data.get("issues") or data.get("anomalies") or data.get("cases") or [data]
            else:
                return False, [], "Ground-truth file must contain a JSON array or object."

            normalized = []
            for idx, item in enumerate(issues, start=1):
                if isinstance(item, dict):
                    normalized.append({
                        "id": item.get("id") or item.get("issue_id") or f"GT_{idx}",
                        "title": item.get("title") or item.get("anomaly") or item.get("problem") or f"Issue {idx}",
                        "dimension": str(item.get("dimension", "")).strip().lower(),
                        "dimension_value": str(item.get("dimension_value") or item.get("cohort", "")).strip().lower(),
                        "cause_category": str(item.get("cause_category") or item.get("category", "")).strip().lower(),
                        "primary_code": str(item.get("primary_code") or item.get("response_code", "")).strip(),
                        "recommended_action": item.get("recommended_action") or item.get("action", "")
                    })

            return True, normalized, None

        except json.JSONDecodeError as jde:
            return False, [], f"Ground truth file contains invalid JSON: {str(jde)}"
        except Exception as exc:
            return False, [], f"Failed to read ground-truth file: {str(exc)}"

    def save_ground_truth(self, raw_content: str) -> Dict[str, Any]:
        """Validate and securely write uploaded ground truth JSON."""
        if not raw_content or not raw_content.strip():
            return {"success": False, "message": "Uploaded ground-truth file is empty."}

        try:
            parsed = json.loads(raw_content)
            issues_count = len(parsed) if isinstance(parsed, list) else len(parsed.get("issues", [parsed]))

            gt_path = get_ground_truth_path()
            gt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(gt_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2)

            return {
                "success": True,
                "message": f"Successfully loaded ground-truth file containing {issues_count} verified issue(s).",
                "issues_count": issues_count,
                "path": str(gt_path)
            }
        except json.JSONDecodeError as jde:
            return {"success": False, "message": f"Invalid JSON format: {str(jde)}"}
        except Exception as exc:
            return {"success": False, "message": f"Error saving ground truth: {str(exc)}"}

    def evaluate_investigation(
        self,
        investigation: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Perform quantitative evaluation of an investigation against ground truth.

        Calculates:
        - Issue detection rate
        - Precision & Recall
        - False positive count
        - Correct dimensions & cause classifications
        - Runtime, SQL query count, agent steps
        - Estimated API cost
        """
        is_loaded, gt_issues, err = self.load_ground_truth()
        if not is_loaded:
            return {
                "evaluation_status": "unavailable",
                "message": "Evaluation unavailable. Upload the ground-truth file to evaluate the investigation.",
                "metrics": None
            }

        findings = investigation.get("findings", [])
        tool_activity = investigation.get("tool_activity", [])
        runtime = float(investigation.get("runtime", 0.0))
        steps = int(investigation.get("steps_executed", len(tool_activity)))

        # Count actual SQL queries executed by the agent
        sql_query_count = sum(1 for ev in tool_activity if ev.get("tool") == "run_sql")

        # Estimate API Cost for Gemini 3.8 Flash
        total_estimated_tokens = steps * ESTIMATED_TOKENS_PER_STEP
        estimated_cost = round(
            (total_estimated_tokens * 0.7 * GEMINI_FLASH_INPUT_COST_PER_M / 1_000_000) +
            (total_estimated_tokens * 0.3 * GEMINI_FLASH_OUTPUT_COST_PER_M / 1_000_000),
            4
        )

        matched_gt_ids = set()
        matched_finding_indices = set()
        dimension_matches = 0
        category_matches = 0

        # Match each finding against ground truth issues
        for f_idx, finding in enumerate(findings):
            prob_text = f"{finding.get('problem', '')} {finding.get('evidence', '')} {finding.get('likely_cause', '')}".lower()
            f_cat = str(finding.get("cause_category", "")).strip().lower()

            for gt in gt_issues:
                gt_id = gt["id"]
                dim_val = gt["dimension_value"]
                dim_key = gt["dimension"]
                gt_cat = gt["cause_category"]
                code = gt["primary_code"]

                # Check if this finding matches the ground truth anomaly
                is_match = False
                if dim_val and dim_val in prob_text:
                    is_match = True
                elif code and f"code {code}" in prob_text or f" {code} " in prob_text:
                    is_match = True
                elif gt["title"].lower() in prob_text:
                    is_match = True

                if is_match:
                    matched_gt_ids.add(gt_id)
                    matched_finding_indices.add(f_idx)

                    # Check dimensional accuracy
                    if (dim_val and dim_val in prob_text) or (dim_key and dim_key in prob_text):
                        dimension_matches += 1

                    # Check cause classification accuracy
                    if f_cat and gt_cat and (f_cat == gt_cat or gt_cat in f_cat):
                        category_matches += 1
                    break

        total_gt = len(gt_issues)
        true_positives = len(matched_gt_ids)
        false_negatives = max(0, total_gt - true_positives)
        false_positives = max(0, len(findings) - len(matched_finding_indices))

        detection_rate = round((true_positives / total_gt) * 100.0, 1) if total_gt > 0 else 0.0
        recall = detection_rate
        total_reported = true_positives + false_positives
        precision = round((true_positives / total_reported) * 100.0, 1) if total_reported > 0 else 0.0
        dimension_accuracy = round((dimension_matches / total_gt) * 100.0, 1) if total_gt > 0 else 0.0
        category_accuracy = round((category_matches / total_gt) * 100.0, 1) if total_gt > 0 else 0.0

        eval_result = {
            "evaluation_status": "evaluated",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "message": "Evaluation completed successfully against uploaded ground truth.",
            "metrics": {
                "detection_rate": detection_rate,
                "precision": precision,
                "recall": recall,
                "true_positives": true_positives,
                "false_positives": false_positives,
                "false_negatives": false_negatives,
                "ground_truth_total": total_gt,
                "dimension_accuracy": dimension_accuracy,
                "cause_category_accuracy": category_accuracy,
                "runtime_seconds": runtime,
                "sql_query_count": sql_query_count,
                "agent_steps": steps,
                "estimated_cost_usd": estimated_cost
            },
            "ground_truth_issues": [
                {
                    "id": gt["id"],
                    "title": gt["title"],
                    "dimension": gt["dimension"],
                    "dimension_value": gt["dimension_value"],
                    "cause_category": gt["cause_category"],
                    "detected": gt["id"] in matched_gt_ids
                }
                for gt in gt_issues
            ]
        }

        self.latest_evaluation = eval_result
        return eval_result

    def get_summary_metrics(self) -> Dict[str, Any]:
        """Return cached summary or readiness status."""
        gt_avail = self.is_ground_truth_available()
        return {
            "ground_truth_loaded": gt_avail,
            "status": "ready" if gt_avail else "pending_ground_truth",
            "latest_evaluation": self.latest_evaluation
        }


# Global evaluator singleton
evaluator = AgentEvaluator()
