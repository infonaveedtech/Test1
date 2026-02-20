from fastapi import APIRouter
from endpoints.pipeline import run_pipeline_internal
from reports.pdf_utils import build_report_pdf
from reports.report_utils import build_report_context
import base64
 
# ✅ NEW: Agent-0 intent gate
from agents.agent0_intent_llm import (
    run_agent0_once,
    DEFAULT_HOST as AGENT0_HOST,
    DEFAULT_MODEL as AGENT0_MODEL,
)
 
router = APIRouter()
 
 
@router.post("/v1/nl-to-pdf")
def nl_to_pdf(payload: dict):
    # ✅ Keep backward compatibility: support both keys
    query = (payload.get("query") or payload.get("additionalProp1") or "").strip()
    row_limit = payload.get("row_limit", 200)
    title = payload.get("title", "AI Generated Report")
 
    # ✅ If query missing/empty → return chat immediately
    if not query:
        return {
            "type": "chat",
            "message": "Please send a report request like: 'Top 10 brokers by notional in last 30 days'."
        }
 
    # 0️⃣ Agent-0 Intent Gate (fast)
    intent_obj = run_agent0_once(
        host=AGENT0_HOST,
        model=AGENT0_MODEL,
        user_query=query,
        debug=False
    )
 
    should_run = bool(intent_obj.get("should_run_pipeline", True))
    assistant_reply = (intent_obj.get("assistant_reply") or "").strip()
    rephrased_query = (intent_obj.get("rephrased_query") or query).strip()
 
    # ✅ If not a SQL/report intent → return chat response
    if not should_run:
        if not assistant_reply:
            assistant_reply = (
                "Hi! If you want a PDF report, ask something like: "
                "'Sales by region last month' or 'Top brokers by notional in last 30 days'."
            )
 
        return {
            "type": "chat",
            "message": assistant_reply,
            "intent": intent_obj.get("intent"),
            "reason": intent_obj.get("reason"),
            "safety": intent_obj.get("safety"),
        }
 
    # 1️⃣ Run pipeline internally
    try:
        pipeline_result = run_pipeline_internal(
            query=rephrased_query,
            run_db=True,
            row_limit=row_limit,
            save_artifacts=False
        )
    except Exception as e:
        # ✅ Pipeline failed → return chat, don’t hard-crash frontend
        return {
            "type": "chat",
            "message": f"I couldn't run the report pipeline. Error: {str(e)}"
        }
 
    # ✅ Defensive extraction (your old code assumes these keys always exist)
    final_sql = pipeline_result.get("final_sql")
    db = pipeline_result.get("db") or {}
    columns = db.get("columns") or []
    rows = db.get("rows") or []
 
    # If something went wrong, respond as chat instead of breaking
    if not final_sql:
        return {
            "type": "chat",
            "message": "I couldn't generate SQL for that request. Try adding a timeframe and metric.",
            "debug": {"missing": "final_sql"}
        }
 
    if not columns:
        return {
            "type": "chat",
            "message": "SQL was generated but no columns were returned. Check DB connectivity or refine the request.",
            "debug": {"missing": "db.columns"}
        }
 
    # # 2️⃣ Generate report payload (NO SQL)
    # report_payload = {
    #     "title": title,
    #     "columns": columns,
    #     "rows": rows,
    #     "include_sql": False   # important
    # }
 
    # # 3️⃣ Generate PDF
    # try:
    #     pdf_bytes = build_report_pdf(report_payload)
    #     pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    # except Exception as e:
    #     return {
    #         "type": "chat",
    #         "message": f"Report data is ready, but PDF generation failed: {str(e)}"
    #     }
    
    # 2️⃣ Enrich context with normalization and metadata (replaces raw payload)
    context = build_report_context(
        title=title,
        question=rephrased_query,  # Use this as the "query asked" (from Agent-0); fallback to original query if needed
        sql=final_sql,
        columns=columns,
        rows=rows,
        model="",  # Pick the composer model (Agent-2) as the "main" one; could use agent1 or agent3 instead
        gen_ms=pipeline_result["meta"]["total_ms"],  # Total generation time from pipeline meta
        exec_ms=db.get("exec_ms"),  # DB execution time from pipeline db result
        rows_est=len(rows),  # Actual rows returned (or use db.get("row_limit") for the limit)
        generated_by="AI Reporting System"  # Customize if your system has a different name, e.g., "Grok Reporting"
    )

    # Optional: Show the technical/debug section in the PDF (includes times, query, SQL)
    context["show_technical"] = True

    # 3️⃣ Generate PDF with enriched context
    try:
        pdf_bytes = build_report_pdf(context)
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    except Exception as e:
        return {
            "type": "chat",
            "message": f"Report data is ready, but PDF generation failed: {str(e)}"
        }
 
    # ✅ Keep your existing keys + add type/message
    return {
        "type": "pdf",
        "message": assistant_reply or "Report generated.",
        "pdf_base64": pdf_base64,
        "columns": columns,
        "rows": rows
    }