"""FastAPI application entrypoint for Payments Decline Analyst Agent."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Query, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.agent import analyst_agent
from app.investigation import investigation_manager
from app.database import (
    check_database_status,
    get_connection,
    get_transaction_count,
    load_csv_data,
    clear_transactions,
    init_database,
    EXPECTED_COLUMNS,
)
from app.tools import (
    AVAILABLE_TOOLS,
    get_tool_execution_logs,
    SUPPORTED_METRICS,
    SUPPORTED_DIMENSIONS,
    RESPONSE_CODES_SPEC,
    VALID_CAUSE_CATEGORIES,
)
from app.evaluation import evaluator
from app.privacy import (
    get_global_privacy_status,
    GLOBAL_PRIVACY_AUDIT,
    PrivacyVault,
)
from app.integration import (
    CommonMessageRequest,
    CommonMessageResponse,
    common_message_processor,
    session_manager,
    integrations_router,
)
from app.slack import slack_router
from app.teams import teams_router
from app.security import security_router

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("payments-decline-analyst")

app = FastAPI(
    title=settings.APP_NAME,
    description="Product Manager Portfolio: AI Agent for Payment Decline Investigation",
    version="1.0.0"
)

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Mount static assets
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount Slack Integration Router
app.include_router(slack_router)

# Mount Microsoft Teams Integration Router
app.include_router(teams_router)

# Mount Enterprise Integrations Dashboard Router (Phase 5)
app.include_router(integrations_router)

# Mount Enterprise Security, Governance & Human Approval Router (Phase 6)
app.include_router(security_router)


@app.on_event("startup")
async def on_startup():
    """Ensure database schema is ready and auto-seed demo data if empty."""
    init_database()
    try:
        if get_transaction_count() == 0:
            logger.info("Empty database detected on startup. Auto-seeding 5,000 demo transactions...")
            from scripts.seed_demo_data import generate_transactions
            csv_content = generate_transactions(count=5000)
            load_csv_data(csv_content, filename="auto_startup_seed.csv", replace=True)
            logger.info("Auto-seeded 5,000 demo transactions successfully.")
    except Exception as exc:
        logger.warning(f"Startup auto-seeding bypassed: {exc}")


@app.get("/", response_class=FileResponse)
async def serve_index():
    """Serve the single-page dashboard shell."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        status_code=404,
        content={"message": "Frontend index.html not found"}
    )


@app.get("/health")
async def root_health_check():
    """Standard Kubernetes / Load Balancer health check."""
    return {"status": "ok", "app": settings.APP_NAME}


@app.get("/api/health")
async def health_check():
    """Return application system and integration health status."""
    db_status = check_database_status()
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "gemini": analyst_agent.get_status(),
        "database": db_status,
        "privacy_shield": {
            "status": "active",
            "mode": "zero_pii_decoupled",
            "enforced": True
        },
        "integration_layer": {
            "status": "active",
            "supported_platforms": ["slack", "teams", "web", "api"],
            "active_sessions": len(session_manager.list_sessions())
        }
    }


@app.get("/api/system/status")
async def system_status():
    """Return overview status of application subsystems."""
    db_status = check_database_status()
    return {
        "agent": analyst_agent.get_status(),
        "database": db_status,
        "data_ready": db_status["dataset_loaded"],
        "privacy_shield": {
            "status": "active",
            "mode": "zero_pii_decoupled",
            "total_scrubs": len(GLOBAL_PRIVACY_AUDIT)
        },
        "integration_layer": {
            "status": "active",
            "active_sessions": len(session_manager.list_sessions())
        },
        "message": "Ready for investigation (Zero-PII Decoupling Active)" if db_status["dataset_loaded"] else "No transaction dataset loaded. Upload a CSV file to begin."
    }


# ==========================================
# Block 2: Data Loading & Management APIs
# ==========================================

@app.get("/api/data/status")
async def get_data_status():
    """Return current dataset loading and database status."""
    return check_database_status()


@app.post("/api/data/upload")
async def upload_transactions(
    file: UploadFile = File(...),
    replace: bool = Query(False, description="Reset database before loading (Demo Reset Mode). Default is false (Financial Append-Only).")
):
    """Upload, validate, and load transaction CSV data into SQLite.
    
    Defaults to Append-Only ingestion (financial immutability).
    If replace=True, performs an explicit demo environment reset before loading.
    """
    filename = file.filename or "uploaded.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "message": "Invalid file format. Only .csv files are supported.",
                "rows_loaded": 0,
                "errors": [f"File '{filename}' does not have a .csv extension."]
            }
        )

    try:
        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "success": False,
                    "message": "The uploaded file is empty (0 bytes).",
                    "rows_loaded": 0,
                    "errors": ["Uploaded file is 0 bytes."]
                }
            )

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = raw_bytes.decode("latin-1")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "message": f"Failed to read file content: {str(exc)}",
                "rows_loaded": 0,
                "errors": [str(exc)]
            }
        )

    result = load_csv_data(content, filename=filename, replace=replace)

    if not result.get("success", False):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=result
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result
    )


@app.delete("/api/data/clear")
async def clear_data():
    """Clear all loaded transactions and reset database for the next demo presentation."""
    clear_transactions()
    return {
        "success": True,
        "message": "Demo database reset successfully. Immutability protections re-armed for next presentation."
    }


@app.post("/api/data/seed")
async def seed_data(
    count: int = Query(5000, ge=100, le=50000, description="Number of synthetic transactions to generate"),
    replace: bool = Query(True, description="Replace existing dataset or append")
):
    """Seed synthetic enterprise transactions showcasing normal baselines and decline anomalies."""
    try:
        from scripts.seed_demo_data import generate_transactions
        csv_content = generate_transactions(count=count)
        result = load_csv_data(csv_content, filename="synthetic_seed.csv", replace=replace)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": f"Successfully seeded {result.get('rows_loaded', count)} transactions with decline anomaly cohorts.",
                "rows_loaded": result.get("rows_loaded", 0),
                "details": result
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"success": False, "message": f"Seeding failed: {str(exc)}"}
        )



# ==========================================
# Block 3: Agent Tools APIs
# ==========================================

@app.get("/api/tools")
async def list_tools():
    """List available agent tools and their specification."""
    tools_info = []
    for tool_fn in AVAILABLE_TOOLS:
        tools_info.append({
            "name": tool_fn.__name__,
            "description": (tool_fn.__doc__ or "").strip().split("\n\n")[0],
        })

    return {
        "tools_count": len(AVAILABLE_TOOLS),
        "tools": tools_info,
        "supported_response_codes": list(RESPONSE_CODES_SPEC.keys()),
        "supported_metrics": SUPPORTED_METRICS,
        "supported_dimensions": SUPPORTED_DIMENSIONS,
        "valid_cause_categories": VALID_CAUSE_CATEGORIES
    }


@app.get("/api/tools/logs")
async def get_tool_logs(limit: int = Query(50, ge=1, le=200)):
    """Retrieve recent agent tool execution logs."""
    logs = get_tool_execution_logs(limit=limit)
    return {
        "count": len(logs),
        "logs": logs
    }


# ==========================================
# Block 4: Gemini Investigation Agent APIs
# ==========================================

class ChatRequest(BaseModel):
    message: Optional[str] = Field(None, description="User prompt or question")
    question: Optional[str] = Field(None, description="Alternative field for user question")
    max_steps: Optional[int] = Field(15, ge=1, le=15, description="Max reasoning/tool steps")


class InvestigateRequest(BaseModel):
    objective: Optional[str] = Field(None, description="Custom objective for decline investigation")
    max_steps: Optional[int] = Field(15, ge=1, le=15, description="Max reasoning/tool steps")


async def stream_investigation_events(query: str, mode: str = "chat", max_steps: int = 15):
    """Asynchronous generator yielding real-time SSE progress events to the browser."""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def sync_event_callback(event: dict):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as e:
            logger.debug("Queue error in SSE streaming: %s", e)

    async def run_agent_in_thread():
        try:
            if mode == "investigate":
                res = await asyncio.to_thread(
                    investigation_manager.run_investigation,
                    objective=query,
                    max_steps=max_steps,
                    event_callback=sync_event_callback
                )
            else:
                res = await asyncio.to_thread(
                    investigation_manager.run_chat,
                    message=query,
                    max_steps=max_steps,
                    event_callback=sync_event_callback
                )
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "completed_payload", "payload": res})
        except Exception as exc:
            logger.exception("Error in agent thread execution: %s", exc)
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "tool_error",
                "stage": "facing_error",
                "status": "ERROR",
                "message": f"Execution error: {str(exc)}",
                "error": str(exc)
            })
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(run_agent_in_thread())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield f"data: {json.dumps(item)}\n\n"
        await asyncio.sleep(0.01)

    await task


@app.post("/api/chat")
async def chat_with_agent(
    req: ChatRequest,
    stream: bool = Query(True, description="Stream live investigation events via Server-Sent Events")
):
    """Run interactive question against the Gemini Decline Analyst Agent."""
    if get_transaction_count() == 0:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "No transaction dataset loaded. Upload a CSV file to begin.",
                "message": "No transaction dataset loaded. Upload a CSV file to begin.",
                "investigation_status": "no_data",
                "response": "No transaction dataset loaded. Upload a CSV file to begin.",
                "tool_activity": [],
                "disclaimer": "Demo using synthetic data. Recommendations are not executed."
            }
        )

    user_query = req.message or req.question
    if not user_query or not user_query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A 'message' or 'question' field is required."
        )

    steps = req.max_steps or 15

    if stream:
        return StreamingResponse(
            stream_investigation_events(query=user_query.strip(), mode="chat", max_steps=steps),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    result = investigation_manager.run_chat(message=user_query.strip(), max_steps=steps)
    return result


@app.post("/api/investigate")
async def run_investigation(
    req: InvestigateRequest = InvestigateRequest(),
    stream: bool = Query(True, description="Stream live investigation events via Server-Sent Events")
):
    """Run structured decline investigation using the Gemini agent."""
    if get_transaction_count() == 0:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "No transaction dataset loaded. Upload a CSV file to begin.",
                "message": "No transaction dataset loaded. Upload a CSV file to begin.",
                "investigation_status": "no_data",
                "findings": [],
                "evidence": [],
                "tool_activity": [],
                "runtime": 0.0,
                "disclaimer": "Demo using synthetic data. Recommendations are not executed."
            }
        )

    steps = req.max_steps or 15
    obj = req.objective or "Perform comprehensive decline anomaly investigation across networks, issuers, channels, and response codes."

    if stream:
        return StreamingResponse(
            stream_investigation_events(query=obj, mode="investigate", max_steps=steps),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    result = investigation_manager.run_investigation(objective=req.objective, max_steps=steps)
    return result


@app.get("/api/investigate/history")
async def get_investigation_history():
    """Retrieve history of completed decline investigations."""
    return {
        "count": len(investigation_manager.history),
        "investigations": investigation_manager.list_investigations()
    }


@app.get("/api/investigate/{investigation_id}")
async def get_investigation_detail(investigation_id: str):
    """Retrieve full details of a specific investigation run."""
    inv = investigation_manager.get_investigation(investigation_id)
    if not inv:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation with ID '{investigation_id}' not found."
        )
    return inv


# ==========================================
# Block 5: Overview Analytics
# ==========================================

@app.get("/api/analytics/overview")
async def get_overview_analytics():
    """Calculate actual summary KPIs and chart aggregations from transactions database."""
    total_txns = get_transaction_count()
    if total_txns == 0:
        return {
            "dataset_loaded": False,
            "transaction_count": 0,
            "message": "No transaction dataset loaded. Upload a CSV file to begin.",
            "metrics": None,
            "charts": None
        }

    try:
        with get_connection(read_only=True) as conn:
            cursor = conn.cursor()

            # 1. High-level KPI metrics
            kpi_query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN response_code = '00' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN response_code != '00' THEN 1 ELSE 0 END) as declined,
                    SUM(CASE WHEN response_code IN ('91', '96') THEN 1 ELSE 0 END) as technical,
                    SUM(CASE WHEN response_code IN ('05', '51') OR (response_code != '00' AND response_code NOT IN ('91', '96')) THEN 1 ELSE 0 END) as business
                FROM transactions;
            """
            cursor.execute(kpi_query)
            row = cursor.fetchone()
            tot = int(row["total"])
            appr = int(row["approved"] or 0)
            decl = int(row["declined"] or 0)
            tech = int(row["technical"] or 0)
            biz = int(row["business"] or 0)

            appr_rate = round(appr * 100.0 / tot, 2) if tot > 0 else 0.0
            decl_rate = round(decl * 100.0 / tot, 2) if tot > 0 else 0.0
            tech_rate = round(tech * 100.0 / tot, 2) if tot > 0 else 0.0
            biz_rate = round(biz * 100.0 / tot, 2) if tot > 0 else 0.0

            # 2. Approval rate over time
            time_query = """
                SELECT
                    SUBSTR(ts, 1, 13) as time_bucket,
                    COUNT(*) as volume,
                    ROUND(100.0 * SUM(CASE WHEN response_code = '00' THEN 1 ELSE 0 END) / COUNT(*), 2) as approval_rate
                FROM transactions
                GROUP BY time_bucket
                ORDER BY time_bucket ASC
                LIMIT 30;
            """
            cursor.execute(time_query)
            time_rows = [dict(r) for r in cursor.fetchall()]

            # 3. Response-code distribution
            code_query = """
                SELECT
                    response_code,
                    COUNT(*) as count
                FROM transactions
                GROUP BY response_code
                ORDER BY count DESC
                LIMIT 8;
            """
            cursor.execute(code_query)
            code_rows = [dict(r) for r in cursor.fetchall()]

            # 4. Approval rate by network
            network_query = """
                SELECT
                    COALESCE(network, 'Unknown') as network,
                    COUNT(*) as volume,
                    ROUND(100.0 * SUM(CASE WHEN response_code = '00' THEN 1 ELSE 0 END) / COUNT(*), 2) as approval_rate
                FROM transactions
                GROUP BY network
                ORDER BY volume DESC;
            """
            cursor.execute(network_query)
            network_rows = [dict(r) for r in cursor.fetchall()]

            # 5. Approval rate by channel
            channel_query = """
                SELECT
                    COALESCE(channel, 'Unknown') as channel,
                    COUNT(*) as volume,
                    ROUND(100.0 * SUM(CASE WHEN response_code = '00' THEN 1 ELSE 0 END) / COUNT(*), 2) as approval_rate
                FROM transactions
                GROUP BY channel
                ORDER BY volume DESC;
            """
            cursor.execute(channel_query)
            network_rows_channel = [dict(r) for r in cursor.fetchall()]

            return {
                "dataset_loaded": True,
                "transaction_count": tot,
                "metrics": {
                    "total_transactions": tot,
                    "approval_rate": appr_rate,
                    "decline_rate": decl_rate,
                    "technical_failure_rate": tech_rate,
                    "business_decline_rate": biz_rate
                },
                "charts": {
                    "approval_over_time": time_rows,
                    "response_code_distribution": code_rows,
                    "approval_by_network": network_rows,
                    "approval_by_channel": network_rows_channel
                }
            }
    except Exception as exc:
        logger.exception("Error calculating overview analytics")
        return {
            "dataset_loaded": True,
            "error": str(exc),
            "metrics": None,
            "charts": None
        }


# ==========================================
# Block 6: Evaluation & Ground Truth APIs
# ==========================================

class ModelConfigRequest(BaseModel):
    model: str = Field(..., description="Gemini model name, e.g. gemini-3.8-flash")


class EvaluateRequest(BaseModel):
    investigation_id: Optional[str] = Field(None, description="Optional ID of specific investigation to evaluate")


@app.get("/api/config/model")
async def get_agent_model():
    """Return active Gemini model and default version."""
    return {
        "model": analyst_agent.model_name,
        "default": "gemini-3.8-flash",
        "supported_example_models": ["gemini-3.8-flash"]
    }


@app.post("/api/config/model")
async def update_agent_model(req: ModelConfigRequest):
    """Dynamically update the active Gemini model version for the agent."""
    new_model = req.model.strip()
    if not new_model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Model name cannot be empty.")

    analyst_agent.model_name = new_model
    settings.GEMINI_MODEL = new_model
    logger.info("Updated active agent model to: %s", new_model)
    return {
        "success": True,
        "message": f"Active agent model successfully updated to '{new_model}'.",
        "model": new_model
    }


@app.post("/api/ground-truth/upload")
async def upload_ground_truth(file: UploadFile = File(...)):
    """Upload, validate, and store ground_truth.json for evaluation benchmark isolation."""
    filename = file.filename or "ground_truth.json"
    if not filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Ground truth file must have a .json extension."
        )

    try:
        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded ground-truth file is empty."
            )
        content = raw_bytes.decode("utf-8")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {str(exc)}"
        )

    res = evaluator.save_ground_truth(content)
    if not res.get("success", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=res.get("message", "Validation error")
        )

    return res


@app.post("/api/evaluate")
async def run_evaluation(req: EvaluateRequest = EvaluateRequest()):
    """Evaluate completed agent findings against uploaded ground-truth benchmarks."""
    # 1. Ground truth availability check
    if not evaluator.is_ground_truth_available():
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "evaluation_status": "unavailable",
                "message": "Evaluation unavailable. Upload the ground-truth file to evaluate the investigation.",
                "metrics": None
            }
        )

    # 2. Retrieve target investigation
    target_inv = None
    if req.investigation_id:
        target_inv = investigation_manager.get_investigation(req.investigation_id)
        if not target_inv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Investigation with ID '{req.investigation_id}' not found."
            )
    else:
        # Grab most recent completed investigation from history
        if investigation_manager.history:
            latest_id = list(investigation_manager.history.keys())[-1]
            target_inv = investigation_manager.history[latest_id]

    if not target_inv:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "evaluation_status": "no_investigation",
                "message": "No completed investigation found. Please run an investigation before evaluating.",
                "metrics": None
            }
        )

    # 3. Perform evaluation
    eval_result = evaluator.evaluate_investigation(target_inv)
    return eval_result


@app.get("/api/evaluation")
async def get_evaluation_status():
    """Return actual evaluation metrics or availability status."""
    if not evaluator.is_ground_truth_available():
        return {
            "evaluation_status": "unavailable",
            "message": "Evaluation unavailable. Upload the ground-truth file to evaluate the investigation.",
            "metrics": None
        }

    return evaluator.get_summary_metrics()


# ==========================================
# Block 7: Zero-PII Privacy & Decoupling APIs
# ==========================================

class PrivacyTestRequest(BaseModel):
    text: str = Field(..., description="Sample text or prompt containing PII (emails, cards, transaction IDs) to test")


@app.get("/api/privacy/status")
async def get_privacy_status():
    """Return active Zero-PII Shield configuration, deny-list, and operational metrics."""
    return get_global_privacy_status()


@app.get("/api/privacy/audit")
async def get_privacy_audit_logs(limit: int = Query(50, ge=1, le=200)):
    """Retrieve recent privacy redaction and surrogate tokenization audit records."""
    logs = list(GLOBAL_PRIVACY_AUDIT)[-limit:]
    return {
        "count": len(logs),
        "audit_logs": logs
    }


@app.post("/api/privacy/test")
async def test_privacy_decoupling(req: PrivacyTestRequest):
    """Interactive diagnostic endpoint to demonstrate the two-way decoupling vault in real-time.

    Shows:
    1. Raw user input.
    2. Sanitized text passed to the LLM (Zero-PII).
    3. Allocated surrogate tokens.
    4. Rehydrated output returned to the human user.
    """
    sample_text = req.text.strip()
    if not sample_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Text field cannot be empty.")

    test_vault = PrivacyVault(session_id="diagnostic_test")
    sanitized_prompt = test_vault.tokenize_text(sample_text, source="diagnostic_test")
    rehydrated_text = test_vault.rehydrate_text(sanitized_prompt)

    return {
        "success": True,
        "original_text": sample_text,
        "sanitized_for_llm": sanitized_prompt,
        "rehydrated_for_user": rehydrated_text,
        "tokens_created": test_vault.counters,
        "token_mapping": test_vault.token_to_real,
        "audit_records": test_vault.audit_log,
        "shield_active": True
    }


# ==========================================
# Block 8: Common Enterprise Messaging APIs (Phase 2)
# ==========================================

@app.post("/api/integration/message", response_model=CommonMessageResponse)
async def process_integration_message(req: CommonMessageRequest):
    """Universal messaging integration ingress for Slack, Microsoft Teams, Web, and API platforms.

    Connects incoming normalized platform requests to Felix, maintains isolated session context,
    runs the agent with existing tools, and returns multi-platform payloads (Block Kit, Adaptive Cards).
    """
    return await common_message_processor.process_message_async(req)


@app.get("/api/integration/sessions")
async def list_integration_sessions(workspace_id: Optional[str] = Query(None, description="Optional workspace filter")):
    """List active multi-turn conversational sessions across messaging platforms."""
    sessions = session_manager.list_sessions(workspace_id=workspace_id)
    return {
        "count": len(sessions),
        "sessions": sessions
    }


@app.get("/api/integration/sessions/{session_id:path}")
async def get_integration_session_detail(session_id: str):
    """Retrieve full conversational history and findings for a specific session."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found."
        )
    return session.to_dict()


@app.delete("/api/integration/sessions/{session_id:path}")
async def clear_integration_session(session_id: str):
    """Reset or clear a conversation session."""
    cleared = session_manager.clear_session(session_id)
    if not cleared:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found."
        )
    return {
        "success": True,
        "message": f"Session '{session_id}' successfully cleared."
    }


