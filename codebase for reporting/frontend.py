#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
frontend.py

Streamlit frontend that talks ONLY to the FastAPI backend (api.py).

- Does NOT import agents / retrieve_context / run_sql directly.
- Calls:
    - POST /v1/pipeline/run  (full NL→SQL pipeline with Agents 0–3)
    - POST /v1/report        (to build HTML/CSV/PDF/DOCX)
    - POST /v1/sql/execute   (optional tab for manual SQL execution)

Run with:
    streamlit run frontend.py --server.port 6768

Make sure the API server is running:
    uvicorn api:app --reload --port 8000
"""

import base64
import json
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

# =========================
# Config
# =========================

# Default API base URL (FastAPI backend)
DEFAULT_API_BASE = "http://localhost:8000"

st.set_page_config(page_title="NL→SQL (API Frontend)", layout="wide")
st.title("🧠 NL→SQL (API-based Frontend)")


# =========================
# Helpers
# =========================

def api_post(
    base_url: str,
    path: str,
    payload: Dict[str, Any],
    timeout: int = 900,
) -> Dict[str, Any]:
    """
    Small helper to POST JSON to the backend and return JSON response.
    Raises a streamlit error if something goes wrong.
    """
    url = base_url.rstrip("/") + path
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except Exception as e:
        st.error(f"Error calling backend at {url}: {e}")
        return {}

    if not resp.ok:
        # Try to show backend error details if present
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        st.error(f"Backend returned {resp.status_code}: {data}")
        return {}

    try:
        return resp.json()
    except Exception as e:
        st.error(f"Could not parse JSON response from {url}: {e}")
        return {}


def decode_base64_to_bytes(b64_str: Optional[str]) -> Optional[bytes]:
    if not b64_str:
        return None
    try:
        return base64.b64decode(b64_str)
    except Exception:
        return None


# =========================
# Sidebar / config
# =========================

with st.sidebar:
    st.header("⚙️ Backend")
    api_base = st.text_input("API base URL", value=DEFAULT_API_BASE)
    st.caption("The FastAPI server should be running at this URL.")

    st.markdown("---")
    st.header("💾 Options")
    run_db = st.checkbox("Execute SQL on DB", value=True)
    row_limit = st.number_input(
        "Row limit (if DB is executed)",
        min_value=1,
        max_value=5000,
        value=200,
        step=50,
    )
    save_artifacts = st.checkbox(
        "Backend: save artifacts to ./out",
        value=False,
        help="If enabled, the backend will write retrieval/envelope/final SQL to ./out.",
    )

    st.markdown("---")
    st.caption("Run backend: `uvicorn api:app --reload --port 8000`")


# =========================
# Main inputs
# =========================

query = st.text_area(
    "💬 Natural language query",
    value="Top 5 brokers by notional in the last 30 days, with broker details.",
    height=100,
)

run_button = st.button("▶️ Run pipeline via API")

# Tabs
intent_tab, rag_tab, a1_tab, a2a3_tab, db_tab, report_tab = st.tabs(
    [
        "🧠 Intent (Agent-0)",
        "🔎 RAG",
        "🧭 Agent-1 (Planner)",
        "🛠️ Agents 2 & 3 (SQL)",
        "🗄️ DB Results",
        "📄 Report",
    ]
)

# Store last pipeline response in session state so we can use it in the Report tab
if "last_pipeline_response" not in st.session_state:
    st.session_state["last_pipeline_response"] = None


# =========================
# Pipeline call
# =========================

if run_button:
    if not query.strip():
        st.warning("Please enter a query first.")
    else:
        with st.spinner("Running pipeline on backend..."):
            payload = {
                "query": query,
                # We rely on backend defaults for host/model_* unless you want to expose them in UI
                "options": {
                    "run_db": run_db,
                    "row_limit": int(row_limit),
                    "save_artifacts": bool(save_artifacts),
                    "run_agent3": False,  # ignored; Agent-3 always runs now
                },
            }
            resp = api_post(api_base, "/v1/pipeline/run", payload)

        if resp:
            st.session_state["last_pipeline_response"] = resp


# =========================
# Show pipeline results (if present)
# =========================

pipeline = st.session_state.get("last_pipeline_response") or {}

if pipeline:

    # ---------- Intent ----------
    intent = pipeline.get("intent")
    with intent_tab:
        st.subheader("Agent-0: Intent & Safety")
        if not intent:
            st.info("No intent information available.")
        else:
            st.write(f"**Intent:** `{intent.get('intent')}`")
            st.write(f"**Should run pipeline:** `{intent.get('should_run_pipeline')}`")
            st.write(f"**Reason:** {intent.get('reason')}")
            st.write(f"**Assistant reply:** {intent.get('assistant_reply')}")
            st.write(f"**Rephrased query:** {intent.get('rephrased_query')}")
            with st.expander("Raw intent JSON"):
                st.json(intent)

    # ---------- RAG ----------
    rag = pipeline.get("rag")
    with rag_tab:
        st.subheader("Retrieval (RAG)")
        if not rag:
            st.info("No RAG result available (pipeline may not have run).")
        else:
            st.write("**Selected chapter(s):**", rag.get("chapters_selected"))
            st.write(
                "**Few-shots (IDs):**",
                [x.get("id") for x in rag.get("fewshots", [])],
            )
            st.write(
                "**Glossary (IDs):**",
                [x.get("id") for x in rag.get("glossary", [])],
            )
            with st.expander("Full RAG JSON"):
                st.json(rag)

    # ---------- Agent-1 ----------
    a1 = pipeline.get("agent1") or {}
    with a1_tab:
        st.subheader("Agent-1 (Planner)")
        env = a1.get("envelope")
        msgs = a1.get("messages")

        if not env:
            st.info("No Agent-1 envelope (pipeline may have failed before Agent-1).")
        else:
            with st.expander("Envelope (JSON)", expanded=True):
                st.json(env)

        if msgs:
            with st.expander("messages[] sent to Agent-1", expanded=False):
                st.json(msgs)

    # ---------- Agents 2 & 3 ----------
    a2 = pipeline.get("agent2") or {}
    a3 = pipeline.get("agent3") or {}
    meta = pipeline.get("meta") or {}

    with a2a3_tab:
        st.subheader("Agent-2 (Composer)")
        a2_sql = a2.get("sql") or ""
        a2_raw = a2.get("raw_llm_output") or ""
        a2_msgs = a2.get("messages") or []

        if not a2_sql:
            st.info("No Agent-2 SQL available.")
        else:
            st.markdown("**Agent-2 SQL (pre-validation):**")
            st.code(a2_sql, language="sql")

        with st.expander("Agent-2 messages", expanded=False):
            st.json(a2_msgs)

        with st.expander("Agent-2 raw LLM output", expanded=False):
            st.text(a2_raw)

        st.markdown("---")
        st.subheader("Agent-3 (Verifier / Refiner)")

        a3_sql = a3.get("sql") or ""
        if a3.get("ran"):
            st.success("Agent-3 ran and produced validated SQL.")
        else:
            st.warning(a3.get("reason") or "Agent-3 did not run; falling back to Agent-2 SQL.")

        if a3_sql:
            st.markdown("**Agent-3 SQL (validated final SQL):**")
            st.code(a3_sql, language="sql")

        rationale = a3.get("rationale")
        if rationale:
            with st.expander("Agent-3 rationale", expanded=False):
                st.text(rationale)

        schema_blocks = a3.get("schema_blocks")
        if schema_blocks:
            with st.expander("Schema blocks used by Agent-3", expanded=False):
                st.json(schema_blocks)

        errors = meta.get("errors") or []
        if errors:
            with st.expander("Pipeline errors", expanded=False):
                st.json(errors)

    # ---------- DB results ----------
    db = pipeline.get("db")
    with db_tab:
        st.subheader("DB Results")
        if not db:
            st.info("DB was not executed (or returned no result). Enable 'Execute SQL on DB' in sidebar.")
        else:
            cols = db.get("columns") or []
            rows = db.get("rows") or []
            exec_ms = db.get("exec_ms")

            st.write(f"**Rows:** {len(rows)}")
            if exec_ms is not None:
                st.write(f"**Execution time:** {exec_ms} ms")

            if cols and rows:
                st.dataframe(
                    [{col: row[i] for i, col in enumerate(cols)} for row in rows],
                    width ='stretch',
                )
            else:
                st.info("No rows returned.")
    
        # ---------- Report ----------
    with report_tab:
        st.subheader("Report generation via /v1/report")

        # We can generate a report if we at least have final SQL and some data.
        final_sql = (a3.get("sql") or a2.get("sql") or "").strip()
        db_data = pipeline.get("db") or {}
        cols = db_data.get("columns") or []
        rows = db_data.get("rows") or []

        if not final_sql:
            st.warning("No final SQL available yet. Run the pipeline first.")
        elif not cols or not rows:
            st.warning(
                "No DB results available. Execute SQL on DB (run_db=true) to get data for the report."
            )
        else:
            st.write("Final SQL and DB results are available. You can generate a report.")

            # SQL editor for the report (does not re-run DB, just changes what is shown)
            edited_sql = st.text_area(
                "Final SQL (you can edit this for the report)",
                value=final_sql,
                height=150,
            )

            # Basic metadata for the report
            title = st.text_input("Report title", value="NL→SQL Report")
            generated_by = st.text_input("Generated by", value="ATS SQLBot")

            # Prepare model label from meta
            models = meta.get("models") or {}
            model_label = " | ".join(
                f"{k}: {v}" for k, v in models.items() if v
            ) or ""

            gen_ms = meta.get("total_ms")
            exec_ms = db_data.get("exec_ms")
            rows_est = str(len(rows))

            # -----------------------------
            # Column customisation UI
            # -----------------------------
            with st.expander("Customize columns (optional)", expanded=False):
                st.caption(
                    "Toggle columns on/off, rename headers, and define simple computed columns.\n"
                    "Use raw column names in expressions, e.g. `ORDER_PRICE * ORDER_VOLUME`."
                )

                include_flags: Dict[str, bool] = {}
                rename_labels: Dict[str, str] = {}

                for raw in cols:
                    c1, c2, c3 = st.columns([0.12, 0.33, 0.55])
                    with c1:
                        include_flags[raw] = st.checkbox(
                            "",
                            value=True,
                            key=f"rep_include_{raw}",
                        )
                    with c2:
                        st.text(raw)
                    with c3:
                        rename_labels[raw] = st.text_input(
                            "Header label",
                            value=raw,
                            key=f"rep_label_{raw}",
                        )

                st.markdown("---")
                st.caption("Computed columns (optional):")

                computed_specs = []
                for i in range(3):
                    cc1, cc2, cc3 = st.columns([0.2, 0.3, 0.5])
                    with cc1:
                        cname = st.text_input(
                            f"New column {i + 1} name",
                            key=f"rep_comp_name_{i}",
                        )
                    with cc2:
                        clabel = st.text_input(
                            f"Header label {i + 1}",
                            key=f"rep_comp_label_{i}",
                        )
                    with cc3:
                        cexpr = st.text_input(
                            f"Expression {i + 1}",
                            key=f"rep_comp_expr_{i}",
                            placeholder="ORDER_PRICE * ORDER_VOLUME",
                        )

                    if cname and cexpr:
                        spec = {
                            "name": cname.strip(),
                            "expression": cexpr.strip(),
                        }
                        if clabel:
                            spec["label"] = clabel.strip()
                        computed_specs.append(spec)

                column_rules = {
                    "include": include_flags,
                    "rename": rename_labels,
                    "computed": computed_specs,
                }

            # -----------------------------
            # Layout / grouping UI
            # -----------------------------
            with st.expander("Layout / grouping (optional)", expanded=True):
                st.caption(
                    "Choose between a simple flat table or a grouped layout.\n"
                    "For example: Broker-wise, Client-wise, Symbol-wise sections."
                )

                layout_choice = st.radio(
                    "Layout type",
                    options=["Simple table", "Grouped by column(s)"],
                    index=0,
                    horizontal=True,
                    key="rep_layout_type",
                )

                group_by_cols: List[str] = []
                detail_cols_layout: List[str] = []

                if layout_choice == "Grouped by column(s)":
                    # Let user pick one or more group-by columns (e.g. BROKER_NAME, CLIENT_NAME)
                    group_by_cols = st.multiselect(
                        "Group by column(s)",
                        options=cols,
                        default=[cols[0]] if cols else [],
                        help="These columns define each group / section (e.g. BROKER_NAME).",
                        key="rep_group_by",
                    )

                    # Which columns to show inside each group's table
                    detail_cols_layout = st.multiselect(
                        "Columns to show inside each group table",
                        options=cols,
                        default=cols,
                        help="Typically you keep all detail columns; you can trim this down if needed.",
                        key="rep_detail_cols",
                    )

                # This object mirrors ReportLayoutOptions on the backend
                layout_payload: Dict[str, Any] = {
                    "layout_type": "grouped" if layout_choice == "Grouped by column(s)" else "flat",
                    "group_by": group_by_cols,
                    "detail_columns": detail_cols_layout or [],
                }

            include_html = st.checkbox("Include HTML", value=True)
            include_csv = st.checkbox("Include CSV", value=True)
            include_pdf = st.checkbox("Include PDF", value=False)
            include_docx = st.checkbox("Include DOCX", value=False)

            if st.button("📄 Generate report via API"):
                payload_report = {
                    "title": title,
                    "question": pipeline.get("intent", {}).get("rephrased_query") or query,
                    "sql": final_sql,
                    "columns": cols,
                    "rows": rows,
                    "model": model_label,
                    "gen_ms": gen_ms,
                    "exec_ms": exec_ms,
                    "rows_est": rows_est,
                    "generated_by": generated_by,
                    "include": {
                        "html": include_html,
                        "csv": include_csv,
                        "pdf": include_pdf,
                        "docx": include_docx,
                    },
                    # NEW: send customisation + SQL override + layout
                    "sql_override": edited_sql if edited_sql and edited_sql != final_sql else None,
                    "column_rules": column_rules,
                    "layout": layout_payload,
                }
                with st.spinner("Calling /v1/report on backend..."):
                    rep = api_post(api_base, "/v1/report", payload_report)

                if rep:
                    html_report = rep.get("html")
                    csv_b64 = rep.get("csv_base64")
                    pdf_b64 = rep.get("pdf_base64")
                    docx_b64 = rep.get("docx_base64")

                    # Optional: show the final customised table (still flat preview)
                    final_cols = rep.get("final_columns") or []
                    final_disp = rep.get("final_display_columns") or final_cols
                    final_rows = rep.get("final_rows") or []

                    if final_cols and final_rows:
                        st.markdown("### Customised table preview")
                        st.dataframe(
                            [
                                {final_disp[i]: row[i] for i in range(len(final_cols))}
                                for row in final_rows
                            ],
                            use_container_width=True,
                        )

                    if html_report:
                        st.markdown("### HTML preview")
                        st.markdown(html_report, unsafe_allow_html=True)

                    if csv_b64:
                        csv_bytes = decode_base64_to_bytes(csv_b64)
                        if csv_bytes:
                            st.download_button(
                                "⬇️ Download CSV",
                                data=csv_bytes,
                                file_name="report.csv",
                                mime="text/csv",
                            )

                    if pdf_b64:
                        pdf_bytes = decode_base64_to_bytes(pdf_b64)
                        if pdf_bytes:
                            st.download_button(
                                "⬇️ Download PDF",
                                data=pdf_bytes,
                                file_name="report.pdf",
                                mime="application/pdf",
                            )

                    if docx_b64:
                        docx_bytes = decode_base64_to_bytes(docx_b64)
                        if docx_bytes:
                            st.download_button(
                                "⬇️ Download DOCX",
                                data=docx_bytes,
                                file_name="report.docx",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            )

else:
    intent_tab.write("Run the pipeline to see results here.")
    rag_tab.write("Run the pipeline to see results here.")
    a1_tab.write("Run the pipeline to see results here.")
    a2a3_tab.write("Run the pipeline to see results here.")
    db_tab.write("Run the pipeline to see results here.")
    report_tab.write("Run the pipeline to see results here.")
