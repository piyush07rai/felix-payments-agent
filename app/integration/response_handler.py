"""Common Response Handler for Enterprise Felix Integration.

Transforms agent execution outputs into normalized CommonMessageResponse objects
and formats rich, platform-native presentation payloads (Slack Block Kit, Teams Adaptive Cards).
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional

from app.integration.schemas import CommonMessageRequest, CommonMessageResponse
from app.integration.session import SessionState


def convert_tables_to_codeblocks(text: str) -> str:
    """Convert pipe tables to clean monospace ASCII blocks for platforms that do not render GFM tables."""
    lines = text.split('\n')
    new_lines = []
    table_lines = []
    in_table = False

    def format_table(tbl):
        rows = []
        for l in tbl:
            clean = l.strip()
            if clean.startswith('|'):
                clean = clean[1:]
            if clean.endswith('|'):
                clean = clean[:-1]
            cols = [re.sub(r'[*_`]', '', c).strip() for c in clean.split('|')]
            rows.append(cols)
        if len(rows) < 2:
            return '\n'.join(tbl)

        header = rows[0]
        data_rows = rows[2:] if len(rows) > 1 and all(set(c).issubset({'-', ':', ' '}) for c in rows[1]) else rows[1:]
        all_rows = [header] + data_rows

        col_count = max(len(r) for r in all_rows)
        widths = [0] * col_count
        for r in all_rows:
            for i, c in enumerate(r):
                if len(c) > widths[i]:
                    widths[i] = len(c)

        out = ['```']
        header_str = ' | '.join(header[i].ljust(widths[i]) if i < len(header) else ''.ljust(widths[i]) for i in range(col_count))
        out.append(header_str)
        out.append('-+-'.join('-' * widths[i] for i in range(col_count)))
        for r in data_rows:
            row_str = ' | '.join(r[i].ljust(widths[i]) if i < len(r) else ''.ljust(widths[i]) for i in range(col_count))
            out.append(row_str)
        out.append('```')
        return '\n'.join(out)

    for line in lines:
        if line.strip().startswith('|') and line.strip().endswith('|'):
            table_lines.append(line)
            in_table = True
        else:
            if in_table:
                new_lines.append(format_table(table_lines))
                table_lines = []
                in_table = False
            new_lines.append(line)
    if in_table:
        new_lines.append(format_table(table_lines))

    return '\n'.join(new_lines)


def convert_markdown_to_slack_mrkdwn(text: str) -> str:
    """Translate standard Markdown into Slack-native mrkdwn format:
    - Converts **bold** and __bold__ to *bold*
    - Converts ### Headers to *Headers*
    - Converts markdown tables into aligned monospace code blocks
    - Strips distracting raw pipe/dash syntax
    """
    if not text:
        return ""

    # 1. Convert headers ### Header to *Header*
    text = re.sub(r'^(#{1,6})\s+(.+)$', r'*\2*', text, flags=re.MULTILINE)

    # 2. Convert bold **text** or __text__ to *text*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'__(.+?)__', r'*\1*', text)

    # 3. Horizontal rules to divider line
    text = re.sub(r'^(?:---+|\*\*\*+|___+)\s*$', '────────────────────────────────────', text, flags=re.MULTILINE)

    # 4. Convert markdown tables into aligned monospace code blocks
    return convert_tables_to_codeblocks(text)


class CommonResponseHandler:
    """Formats normalized responses and platform-specific rich payloads."""

    @classmethod
    def format_slack_blocks(
        cls,
        text: str,
        findings: List[Dict[str, Any]],
        tools_executed: List[str],
        execution_time_ms: float,
        session_id: str,
        turn_index: int,
    ) -> List[Dict[str, Any]]:
        """Construct Slack Block Kit blocks structure."""
        slack_formatted_text = convert_markdown_to_slack_mrkdwn(text)
        blocks: List[Dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Felix • Payments Decline Analyst",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": slack_formatted_text[:2900]  # Slack section limit is 3000 chars
                }
            }
        ]

        # Add structured findings summary if findings were generated
        if findings:
            blocks.append({"type": "divider"})
            finding_fields = []
            for f in findings[:4]:
                prob = f.get("problem", "Decline Anomaly")[:60]
                uplift = f.get("estimated_approval_rate_uplift", "+0.0%")
                action = f.get("recommended_action", "")[:100]
                finding_fields.append({
                    "type": "mrkdwn",
                    "text": f"*Action:* {prob}\n*Est. Uplift:* `{uplift}`\n_{action}_"
                })

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Drafted Remediation Playbooks ({len(findings)}):*"
                },
                "fields": finding_fields[:8]
            })

        # Context footer
        tools_str = ", ".join(tools_executed) if tools_executed else "analytics"
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"⏱️ *{round(execution_time_ms, 1)}ms* | 🛠️ *Tools:* `{tools_str}` | 🛡️ *Zero-PII Active* | Turn #{turn_index}"
                }
            ]
        })

        return blocks

    @classmethod
    def format_teams_card(
        cls,
        text: str,
        findings: List[Dict[str, Any]],
        tools_executed: List[str],
        execution_time_ms: float,
        session_id: str,
        turn_index: int,
    ) -> Dict[str, Any]:
        """Construct Microsoft Teams Adaptive Card 1.5 schema."""
        facts = [
            {"title": "Execution Time", "value": f"{round(execution_time_ms, 1)}ms"},
            {"title": "Tools Used", "value": ", ".join(tools_executed) if tools_executed else "direct"},
            {"title": "Privacy Shield", "value": "Zero-PII Active (Local Vault)"},
            {"title": "Turn", "value": f"#{turn_index}"},
        ]

        teams_text = convert_tables_to_codeblocks(text)
        body: List[Dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": "Felix • Payments Decline Analyst",
                "weight": "Bolder",
                "size": "Medium",
                "color": "Accent"
            },
            {
                "type": "TextBlock",
                "text": teams_text,
                "wrap": True
            },
            {
                "type": "FactSet",
                "facts": facts
            }
        ]

        if findings:
            body.append({
                "type": "TextBlock",
                "text": f"Remediation Findings ({len(findings)})",
                "weight": "Bolder",
                "separator": True
            })
            for f in findings[:3]:
                body.append({
                    "type": "Container",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": f"**{f.get('problem', 'Decline Issue')}** (Est. Uplift: {f.get('estimated_approval_rate_uplift', '+0.0%')})",
                            "wrap": True,
                            "color": "Good"
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Recommended Action: {f.get('recommended_action', '')}",
                            "wrap": True,
                            "size": "Small",
                            "isSubtle": True
                        }
                    ]
                })

        return {
            "type": "AdaptiveCard",
            "version": "1.5",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": body
        }

    @classmethod
    def create_response(
        cls,
        req: CommonMessageRequest,
        session: SessionState,
        agent_result: Dict[str, Any],
        execution_time_ms: float,
        turn_index: int,
        error: Optional[str] = None,
    ) -> CommonMessageResponse:
        """Construct the complete CommonMessageResponse with multi-platform payloads."""
        response_text = agent_result.get("response", "") or (f"Error: {error}" if error else "No response generated.")
        findings = agent_result.get("findings", [])
        tool_activity = agent_result.get("tool_activity", [])
        privacy_metadata = agent_result.get("privacy_audit", {})
        status = agent_result.get("investigation_status", "completed" if not error else "error")

        tools_executed = [
            t.get("tool", "") for t in tool_activity if t.get("tool")
        ]

        # Generate platform-native representations
        slack_blocks = cls.format_slack_blocks(
            text=response_text,
            findings=findings,
            tools_executed=tools_executed,
            execution_time_ms=execution_time_ms,
            session_id=session.session_id,
            turn_index=turn_index,
        )

        teams_card = cls.format_teams_card(
            text=response_text,
            findings=findings,
            tools_executed=tools_executed,
            execution_time_ms=execution_time_ms,
            session_id=session.session_id,
            turn_index=turn_index,
        )

        platform_payload = {
            "markdown": response_text,
            "slack": {"blocks": slack_blocks},
            "teams": {"adaptive_card": teams_card},
            "tools_count": len(tools_executed),
            "findings_count": len(findings),
        }

        return CommonMessageResponse(
            request_id=req.request_id,
            platform=req.platform,
            workspace_id=req.workspace_id,
            conversation_id=req.conversation_id,
            thread_id=req.thread_id,
            user_id=req.user_id,
            session_id=session.session_id,
            status=status,
            response_text=response_text,
            platform_payload=platform_payload,
            findings=findings,
            tool_activity=tool_activity,
            privacy_metadata=privacy_metadata,
            turn_index=turn_index,
            execution_time_ms=round(execution_time_ms, 2),
            timestamp=datetime.now(timezone.utc).isoformat(),
            error=error,
        )
