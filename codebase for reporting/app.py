#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, time, traceback, re
from pathlib import Path

import streamlit as st

# ---- Local project imports ----
import retrieve_context
from db.oracle_exec import run_sql
from reports.report_utils import (
    build_report_context,
    build_html_report,
    build_csv_bytes,
    build_docx_bytes,
    apply_column_customizations,
    apply_layout_grouping,
)
from reports.pdf_utils import build_report_pdf


# ---- Paths ----
BASE = Path(".").resolve()
OUT_DIR = BASE / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_A1 = BASE / "prompts/system_agent1_planner.md"
SCHEMA_CARD = BASE / "prompts/schema_card.md"
GLOSSARY_MD = BASE / "prompts/glossary.md"  # adjust if needed
GLOSSARY_JSONL = BASE / "data/docs/glossary.docs.jsonl"
FEWSHOTS_JSONL = BASE / "data/docs/fewshots.docs.jsonl"

# Agent 0 (Intent Gate)
from agents.agent0_intent_llm import (
    run_agent0_once,
    DEFAULT_MODEL as A0_DEFAULT_MODEL,
)

SYSTEM_A0 = BASE / "prompts/system_agent0_intent.md"
AGENT0_INTENT_JSON = OUT_DIR / "agent0_intent.json"

# Agent 1 (Planner)
from agents.agent1_planner_llm import (
    build_prompt_from_retrieval,
    run_planner_once,
    DEFAULT_HOST as A1_DEFAULT_HOST,
    DEFAULT_MODEL as A1_DEFAULT_MODEL,
    BUDGET_SCHEMA as A1_BUDGET_SCHEMA,
    BUDGET_GLOSSARY_EACH as A1_BUDGET_GLOSSARY_EACH,
    BUDGET_FEWSHOT_EACH as A1_BUDGET_FEWSHOT_EACH,
)

# Agent 2 (Composer)
from agents.agent2_composer_llm import (
    call_ollama_chat as a2_call_ollama_chat,
    normalize_envelope as a2_normalize_envelope,
    build_messages as a2_build_messages,
    extract_sql as a2_extract_sql,
    DEFAULT_MODEL as A2_DEFAULT_MODEL,
)

# Agent 3 (Verifier/Refiner)
from agents.agent3_verifier_refiner_llm import run_agent3_once

# (redeclared, but harmless)
BASE = Path(".").resolve()
OUT_DIR = BASE / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_A2 = BASE / "prompts/system_agent2_composer.md"
SYSTEM_A3 = BASE / "prompts/system_agent3_verifier_refiner.md"

RETRIEVAL_JSON = OUT_DIR / "retrieval.json"
A1_ENVELOPE_JSON = OUT_DIR / "agent1_envelope.json"
FINAL_SQL_PATH = OUT_DIR / "final.sql"

# =========================
# Helpers
# =========================

HEADER_RE = re.compile(
    r"(?im)^\s*#{2,}\s+([A-Z0-9_]+\s*\.\s*[A-Z0-9_]+)\b.*$"
)


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _safe_read(path: Path, default=""):
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return default


def _qualify_table_name(t: str, default_owner: str = "ATS") -> str:
    t = (t or "").strip()
    if not t:
        return t
    t = re.sub(r"\s*\.\s*", ".", t)
    if "." in t:
        return t
    return f"{default_owner}.{t}"


def _collect_owner_tables_from_envelope(env: dict) -> list[str]:
    tables = []
    if isinstance(env, dict):
        tables = env.get("tables") or env.get("table_list") or []
    tables = [_qualify_table_name(t) for t in tables if t]
    return sorted({t.upper(): t for t in tables}.values(), key=str.upper)


def _slice_schema_blocks_for_tables(card_text: str, tables: list[str]) -> dict[str, str]:
    card_text = re.sub(r"\s*\.\s*", ".", card_text)
    U = card_text.upper()

    headers = [(m.start(), m.end(), m.group(1).strip()) for m in HEADER_RE.finditer(U)]
    if not headers:
        return {}

    blocks = {}
    sentinel_end = len(U)
    headers_sorted = sorted(headers, key=lambda x: x[0])
    for i, (s, e, tbl) in enumerate(headers_sorted):
        block_start = e
        block_end = headers_sorted[i + 1][0] if i + 1 < len(headers_sorted) else sentinel_end
        block = card_text[block_start:block_end].strip()
        blocks[tbl] = block

    want = {re.sub(r"\s*\.\s*", ".", t.upper()): t for t in tables}
    out = {}
    for fq_upper, orig in want.items():
        uq = fq_upper.split(".")[-1]
        if fq_upper in blocks:
            out[orig] = blocks[fq_upper]
        else:
            match_key = next((k for k in blocks.keys() if k.split(".")[-1] == uq), None)
            if match_key:
                out[orig] = blocks[match_key]
            else:
                out[orig] = ""
    return out


def _light_guard_tables_only(sql_text: str, allowed_tables: list[str]) -> list[str]:
    if not sql_text or not allowed_tables:
        return []
    allowed_upper = {t.upper() for t in allowed_tables}
    known_owners = {t.split(".")[0].upper() for t in allowed_tables}
    pat = re.compile(r"\b([A-Z][A-Z0-9_]{1,})\.([A-Z][A-Z0-9_]{2,})\b")
    disallowed = set()
    for owner, table in pat.findall(sql_text.upper()):
        if owner in known_owners:
            tok = f"{owner}.{table}"
            if tok not in allowed_upper:
                disallowed.add(tok)
    return sorted(disallowed)


def _norm_dot(s: str) -> str:
    return re.sub(r"\s*\.\s*", ".", (s or "")).upper().strip()


def _columns_from_block(block_text: str) -> list[str]:
    stop = {
        "NOT",
        "NULL",
        "PRIMARY",
        "FOREIGN",
        "KEY",
        "CONSTRAINT",
        "CHECK",
        "UNIQUE",
        "REFERENCES",
        "ON",
        "UPDATE",
        "DELETE",
    }
    cols, seen = [], set()
    if not block_text:
        return cols
    m = re.search(r"(?im)^\s*Columns\s*:\s*$", block_text)
    scan = block_text[m.start() :] if m else block_text
    for raw in scan.splitlines():
        up = raw.strip().upper()
        if not up:
            continue
        m1 = re.match(r'^\s*[-*\u2022]\s*"?(?P<c>[A-Z][A-Z0-9_]+)"?\s+(?:[A-Z]|")', up)
        m2 = re.match(r'^\s*"?(?P<c>[A-Z][A-Z0-9_]+)"?\s+(?:[A-Z]|:)', up)
        m3 = re.match(r'^\s*"?(?P<c>[A-Z][A-Z0-9_]+)"?\s*[:\-]', up)
        m = m1 or m2 or m3
        if not m:
            continue
        c = m.group("c")
        if c in stop:
            continue
        if c not in seen:
            seen.add(c)
            cols.append(c)
    return cols


def _glossary_block_for_table(gloss_text: str, table_name: str, window: int = 80) -> str:
    text = re.sub(r"\s*\.\s*", ".", gloss_text or "")
    lines = text.splitlines()
    UL = [ln.upper() for ln in lines]
    want_fq = _norm_dot(table_name)
    want_uq = want_fq.split(".")[-1]
    idx = -1
    for i, up in enumerate(UL):
        if want_fq in up or re.search(rf"\b{re.escape(want_uq)}\b", up):
            idx = i
            break
    if idx < 0:
        return ""
    lo = max(0, idx - window // 2)
    hi = min(len(lines), idx + window // 2)
    return "\n".join(lines[lo:hi]).strip()


def _columns_from_glossary(gloss_text: str, table_name: str) -> list[str]:
    text = re.sub(r"\s*\.\s*", ".", gloss_text or "")
    U = text.upper()
    want_fq = _norm_dot(table_name)
    want_uq = want_fq.split(".")[-1]
    cols = set()
    for m in re.finditer(rf"\b(?:{re.escape(want_uq)}|{re.escape(want_fq)})\.[A-Z0-9_]+\b", U):
        token = U[m.start() : m.end()]
        col = token.split(".")[-1]
        if re.match(r"^[A-Z][A-Z0-9_]+$", col):
            cols.add(col)
    return sorted(cols)


def _merge_blocks(schema_blocks: dict, glossary_blocks: dict) -> dict:
    merged = {}
    for k in schema_blocks.keys() | glossary_blocks.keys():
        a = (schema_blocks.get(k) or "").strip()
        b = (glossary_blocks.get(k) or "").strip()
        merged[k] = a if a else b
    return merged


def _build_allowed_schema(schema_blocks: dict[str, str], gloss_text: str) -> dict[str, list[str]]:
    allowed = {}
    for tbl, blk in schema_blocks.items():
        cols = set()
        cols.update(_columns_from_block(blk))
        cols.update(_columns_from_glossary(gloss_text, tbl))
        allowed[tbl] = sorted(cols)
    return allowed


def _light_guard_columns_exist(sql_text: str, allowed_schema: dict) -> list[str]:
    bad = []
    if not sql_text or not allowed_schema:
        return bad
    triple_re = re.compile(r"\b([A-Z0-9_]+)\.([A-Z0-9_]+)\.([A-Z0-9_]+)\b")
    allowed_norm = {k.upper(): {c.upper() for c in v} for k, v in allowed_schema.items()}
    for owner, table, col in triple_re.findall(sql_text.upper()):
        key = f"{owner}.{table}"
        if key in allowed_norm and col not in allowed_norm[key]:
            bad.append(f"{key}.{col}")
    return bad


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="NL→SQL (RAG + Agents)", layout="wide")
st.title("🧠 NL → SQL: RAG → Agent-0 → Agent-1 → Agent-2 → (optional) Agent-3")

# ---- Session state (for persistence across reruns) ----
defaults = {
    "a0_intent": None,
    "final_sql": "",
    "rag_obj": None,
    "a1_envelope": None,
    "a1_messages": None,
    "a2_messages": None,
    "last_query": "",
    "gen_ms_ms": None,        # generation time in ms
    "db_columns": None,
    "db_rows": None,
    "db_exec_ms": None,
    "db_message": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


with st.sidebar:
    st.header("⚙️ Runtime")
    col_host = st.text_input("Host URL", value=A1_DEFAULT_HOST)

    col_model_a0 = st.text_input("Agent-0 Model", value=A0_DEFAULT_MODEL)
    col_model_a1 = st.text_input("Agent-1 Model", value=A1_DEFAULT_MODEL)
    col_model_a2 = st.text_input("Agent-2 Model", value=A2_DEFAULT_MODEL)
    model_agent3 = st.text_input("Agent-3 Model", value="qwen3-coder:30b")

    num_ctx = st.number_input("Context tokens", value=22000, step=1000)
    temperature = st.number_input("Temperature", value=0.1, step=0.1, format="%.1f")

    st.header("🧾 Inputs")
    save_artifacts = st.checkbox("Save artifacts to ./out", value=True)
    clip_schema_chars = st.number_input(
        "Schema budget (chars)", value=A1_BUDGET_SCHEMA, step=1000
    )
    clip_glossary_chars = st.number_input(
        "Glossary item budget (chars)", value=A1_BUDGET_GLOSSARY_EACH, step=100
    )
    clip_fewshot_chars = st.number_input(
        "Few-shot item budget (chars)", value=A1_BUDGET_FEWSHOT_EACH, step=100
    )

    st.markdown("---")
    st.header("🧪 Agent-3 (Validation/Refinement)")
    enable_agent3 = st.checkbox("Enable Agent-3 validation", value=True)
    st.caption("Agent-3 receives raw schema blocks between '## OWNER.TABLE' and next '##' (no typing).")

    st.markdown("---")
    st.caption("Run with:  \n`streamlit run app.py --server.port 6767`")

query = st.text_area(
    "💬 Natural language query",
    value="Brokers with highest notional in last 30 days; show broker info",
    height=100,
)
run_button = st.button("▶️ Run Pipeline")

# Tabs
rag_col, a1_col, a2_col, db_col = st.tabs(
    ["🔎 Intent + Retrieval (RAG)", "🧭 Agent-1 (Planner)", "🛠️ Agent-2 + Agent-3", "🗄️ Run on DB"]
)

# =========================
# Pipeline
# =========================
if run_button:
    overall_t0 = time.perf_counter()
    errors = []

    intent_ok = True
    intent_obj = None

    # ---------- 0) Agent-0: Intent & Safety Gate ----------
    try:
        with rag_col:
            st.subheader("Agent-0: Intent & Safety Gate")
            t0 = time.perf_counter()

            intent_obj = run_agent0_once(
                host=col_host,
                model=col_model_a0,
                user_query=query,
                debug=False,
            )

            t1 = time.perf_counter()
            st.success(f"Agent-0 OK · {t1 - t0:0.2f}s")

            st.session_state["a0_intent"] = intent_obj

            with st.expander("Agent-0 intent JSON", expanded=False):
                st.json(intent_obj)

            if save_artifacts:
                _write_json(AGENT0_INTENT_JSON, intent_obj)

            intent_ok = bool(intent_obj.get("should_run_pipeline", True))

            if not intent_ok:
                reply = intent_obj.get(
                    "assistant_reply",
                    "This does not look like a database / analytics question, so the NL→SQL pipeline was not run.",
                )
                st.warning(reply)

                with a1_col:
                    st.info("⏹️ Pipeline stopped by Agent-0 (intent gate).")
                with a2_col:
                    st.info("⏹️ Pipeline stopped by Agent-0 (intent gate).")
                with db_col:
                    st.info("⏹️ Pipeline stopped by Agent-0 (intent gate).")

    except Exception as e:
        errors.append(("Agent-0", str(e), traceback.format_exc()))
        intent_ok = False
        rag_col.error(f"Agent-0 FAILED: {e}")

    # ---------- 1) RAG ----------
    try:
        if intent_ok:
            with rag_col:
                st.subheader("Retrieval")
                t0 = time.perf_counter()
                rag_obj = retrieve_context.retrieve_context(query)
                t1 = time.perf_counter()

                st.success(f"RAG OK · {t1 - t0:0.2f}s")
                st.write("**Selected chapter(s):**", rag_obj.get("chapters_selected"))
                st.write("**Few-shots (IDs):**", [x.get("id") for x in rag_obj.get("fewshots", [])])
                st.write("**Glossary (IDs):**", [x.get("id") for x in rag_obj.get("glossary", [])])

                with st.expander("View RAG JSON", expanded=False):
                    st.json(rag_obj)

                if save_artifacts:
                    _write_json(RETRIEVAL_JSON, rag_obj)

                # Persist RAG result for later (so it survives DB runs)
                st.session_state["rag_obj"] = rag_obj

    except Exception as e:
        errors.append(("RAG", str(e), traceback.format_exc()))
        rag_col.error(f"RAG FAILED: {e}")

    # ---------- 2) Agent-1 ----------
    a1_envelope = None
    a1_messages = None
    try:
        if not errors and intent_ok:
            with a1_col:
                st.subheader("Agent-1 (Planner)")
                t0 = time.perf_counter()

                a1_messages, retrieval_obj, gl_expanded, fs_expanded = build_prompt_from_retrieval(
                    system_path=SYSTEM_A1,
                    schema_path=SCHEMA_CARD,
                    retrieval_json_path=RETRIEVAL_JSON if save_artifacts else None or RETRIEVAL_JSON,
                    glossary_jsonl_path=GLOSSARY_JSONL,
                    fewshots_jsonl_path=FEWSHOTS_JSONL,
                    budget_schema=int(clip_schema_chars),
                    budget_glossary_each=int(clip_glossary_chars),
                    budget_fewshot_each=int(clip_fewshot_chars),
                )

                with st.expander("Prompt → messages[] (Agent-1)", expanded=False):
                    st.json(a1_messages)

                a1_envelope = run_planner_once(
                    host=col_host,
                    model=col_model_a1,
                    messages=a1_messages,
                    retrieval_obj=retrieval_obj,
                    glossary_expanded=gl_expanded,
                    debug=False,
                )

                t1 = time.perf_counter()
                st.success(f"Agent-1 OK · {t1 - t0:0.2f}s")

                with st.expander("Envelope (JSON)", expanded=True):
                    st.json(a1_envelope)

                if save_artifacts:
                    _write_json(A1_ENVELOPE_JSON, a1_envelope)

                # Persist Agent-1 outputs
                st.session_state["a1_envelope"] = a1_envelope
                st.session_state["a1_messages"] = a1_messages

    except Exception as e:
        errors.append(("Agent-1", str(e), traceback.format_exc()))
        a1_col.error(f"Agent-1 FAILED: {e}")

    # ---------- 3) Agent-2 + Agent-3 ----------
    final_sql = ""
    try:
        if not errors and intent_ok and a1_envelope:
            with a2_col:
                st.subheader("Agent-2 (Composer)")
                t0 = time.perf_counter()

                env_norm = a2_normalize_envelope(a1_envelope)

                system_a2 = _safe_read(
                    SYSTEM_A2,
                    default=(
                        "You are Agent-2. Produce one Oracle SQL statement. "
                        "No prose. No markdown. End with a semicolon."
                    ),
                )
                a2_messages = a2_build_messages(system_a2, query, env_norm)

                with st.expander("Prompt → messages[] (Agent-2)", expanded=False):
                    st.json(a2_messages)

                raw = a2_call_ollama_chat(
                    host=col_host,
                    model=col_model_a2,
                    messages=a2_messages,
                    num_ctx=int(num_ctx),
                    temperature=float(temperature),
                    debug=False,
                )
                agent2_sql = a2_extract_sql(raw).strip()

                t1 = time.perf_counter()
                st.success(f"Agent-2 OK · {t1 - t0:0.2f}s")

                st.markdown("**Agent-2 SQL (pre-validation):**")
                st.code(agent2_sql or raw, language="sql")

                final_sql = agent2_sql

                # Persist Agent-2 messages & SQL
                st.session_state["a2_messages"] = a2_messages
                st.session_state["agent2_sql"] = agent2_sql

                # Agent-3
                if enable_agent3 and agent2_sql:
                    st.subheader("Agent-3 (Verifier/Refiner)")

                    used_tables = _collect_owner_tables_from_envelope(a1_envelope)
                    if not used_tables:
                        st.warning("Agent-1 envelope did not list any tables; skipping Agent-3.")
                    else:
                        st.write("**Used tables (from Agent-1):**", used_tables)

                        card_text = _safe_read(SCHEMA_CARD, default="")
                        gloss_text = _safe_read(GLOSSARY_MD, default="")

                        schema_blocks_card = _slice_schema_blocks_for_tables(card_text, used_tables)
                        glossary_blocks = {
                            t: _glossary_block_for_table(gloss_text, t) for t in used_tables
                        }
                        schema_blocks = _merge_blocks(schema_blocks_card, glossary_blocks)

                        st.write("**Schema blocks (card or glossary fallback):**")
                        st.json(schema_blocks)

                        allowed_schema = _build_allowed_schema(schema_blocks, gloss_text)

                        st.write("**Allowed schema for Agent-3 (merged from card + glossary):**")
                        st.json(allowed_schema)

                        if all(len(v) == 0 for v in allowed_schema.values()):
                            st.error(
                                "Could not extract any columns for the used tables from schema card + glossary."
                            )
                            st.stop()

                        canonical_joins = []

                        try:
                            corrected_sql, rationale = run_agent3_once(
                                host=col_host,
                                model=model_agent3,
                                sql_text=agent2_sql,
                                schema_blocks=schema_blocks,
                                canonical_joins=canonical_joins,
                                system_path=SYSTEM_A3,
                                num_ctx=int(num_ctx),
                                temperature=float(temperature),
                                debug=False,
                            )

                            st.markdown("**Agent-3 SQL (validated):**")
                            st.code(corrected_sql, language="sql")

                            post_invalid = _light_guard_columns_exist(corrected_sql, allowed_schema)
                            if post_invalid:
                                st.error(
                                    "Agent-3 referenced invalid columns: " + ", ".join(post_invalid)
                                )
                                st.stop()

                            final_sql = corrected_sql

                        except Exception as e:
                            st.error(f"Agent-3 failed: {e}")

                # Save final SQL on disk and in session state
                if final_sql:
                    FINAL_SQL_PATH.write_text(final_sql + "\n", encoding="utf-8")
                    st.session_state["final_sql"] = final_sql

                st.markdown("---")
                st.subheader("✅ Final SQL")
                st.code(final_sql or "<empty>", language="sql")

    except Exception as e:
        errors.append(("Agent-2/3", str(e), traceback.format_exc()))
        a2_col.error(f"Agent-2/3 FAILED: {e}")

    # ---------- Summary ----------
    total = time.perf_counter() - overall_t0
    st.markdown("---")
    if errors:
        st.error(f"❌ Pipeline finished with errors · {total:0.2f}s")
        with st.expander("Show errors / tracebacks", expanded=False):
            for stage, msg, tb in errors:
                st.write(f"### {stage} error")
                st.write(msg)
                st.code(tb)
    else:
        if intent_ok:
            st.success(f"✅ Pipeline finished · {total:0.2f}s")
        else:
            st.warning(f"🚦 Pipeline skipped by Agent-0 (intent gate) · {total:0.2f}s")

    # Store last query and generation time (for the report exports)
    st.session_state["last_query"] = query
    st.session_state["gen_ms_ms"] = int(total * 1000)


# =========================
# When not running the pipeline, show last results from state
# =========================
if not run_button:
    # Agent-0
    a0_prev = st.session_state.get("a0_intent")
    if a0_prev:
        with rag_col:
            st.subheader("Agent-0 (Intent Gate) – last run")
            with st.expander("Agent-0 intent JSON", expanded=False):
                st.json(a0_prev)

    # RAG
    rag_prev = st.session_state.get("rag_obj")
    if rag_prev:
        with rag_col:
            st.subheader("Retrieval (last run)")
            st.write("**Selected chapter(s):**", rag_prev.get("chapters_selected"))
            st.write("**Few-shots (IDs):**", [x.get("id") for x in rag_prev.get("fewshots", [])])
            st.write("**Glossary (IDs):**", [x.get("id") for x in rag_prev.get("glossary", [])])
            with st.expander("View RAG JSON", expanded=False):
                st.json(rag_prev)

    # Agent-1
    a1_prev = st.session_state.get("a1_envelope")
    a1_msgs_prev = st.session_state.get("a1_messages")
    if a1_prev or a1_msgs_prev:
        with a1_col:
            st.subheader("Agent-1 (Planner) – last run")
            if a1_msgs_prev:
                with st.expander("Prompt → messages[] (Agent-1)", expanded=False):
                    st.json(a1_msgs_prev)
            if a1_prev:
                with st.expander("Envelope (JSON)", expanded=True):
                    st.json(a1_prev)

    # Agent-2/3
    final_prev = st.session_state.get("final_sql", "")
    a2_msgs_prev = st.session_state.get("a2_messages")
    if final_prev or a2_msgs_prev:
        with a2_col:
            st.subheader("Agent-2 + Agent-3 – last run")
            if a2_msgs_prev:
                with st.expander("Prompt → messages[] (Agent-2)", expanded=False):
                    st.json(a2_msgs_prev)
            st.markdown("**Final SQL**")
            st.code(final_prev or "<empty>", language="sql")

# =========================
# DB Tab (always rendered)
# =========================
with db_col:
    st.subheader("🗄️ Execute SQL on Oracle")

    final_sql_state = st.session_state.get("final_sql", "")

    if not final_sql_state:
        st.info("No SQL available yet. Run the pipeline first to generate SQL.")
    else:
        # Editable SQL box – user can tweak the final SQL before running
        sql_default = st.session_state.get("sql_to_run") or final_sql_state
        sql_to_run = st.text_area(
            "SQL to execute (you can edit this before running)",
            value=sql_default,
            height=220,
            key="sql_to_run",
        )

        preview_limit = st.number_input(
            "Row limit for export",
            min_value=1,
            max_value=2000,
            value=200,
            step=50,
            key="preview_row_limit",
        )

        # Run SQL and cache results in session_state (no table preview)
        if st.button("▶️ Run SQL on DB", key="run_sql_db_button"):
            try:
                t0 = time.perf_counter()
                effective_sql = st.session_state.get("sql_to_run") or final_sql_state
                result = run_sql(effective_sql, row_limit=preview_limit)
                exec_ms = int((time.perf_counter() - t0) * 1000)

                st.session_state["db_columns"] = result["columns"]
                st.session_state["db_rows"] = result["rows"]
                st.session_state["db_exec_ms"] = exec_ms
                st.session_state["db_message"] = (
                    f"Success! Returned {len(result['rows'])} rows in {exec_ms} ms."
                )
                # remember which SQL was actually executed
                st.session_state["executed_sql"] = effective_sql

            except Exception as e:
                st.session_state["db_columns"] = None
                st.session_state["db_rows"] = None
                st.session_state["db_exec_ms"] = None
                st.session_state["db_message"] = None
                st.error(f"Oracle DB Error: {e}")

        # Show status & export buttons (no DataFrame preview)
        msg = st.session_state.get("db_message")
        cols = st.session_state.get("db_columns")
        rows = st.session_state.get("db_rows")

        if msg:
            st.success(msg)

        if cols and rows:
            st.markdown("### Export")

            # Use the SQL that was actually executed (may have been edited)
            executed_sql = (
                st.session_state.get("executed_sql")
                or st.session_state.get("sql_to_run")
                or final_sql_state
            )

            # Build base report context
            model_label = f"A1: {col_model_a1} | A2: {col_model_a2}"
            if enable_agent3:
                model_label += f" | A3: {model_agent3}"

            context = build_report_context(
                title="NL→SQL Report",
                question=st.session_state.get("last_query", ""),
                sql=executed_sql,
                columns=cols,
                rows=rows,
                model=model_label,
                gen_ms=st.session_state.get("gen_ms_ms"),
                exec_ms=st.session_state.get("db_exec_ms"),
                rows_est="-",
                generated_by="ATS SQLBot",
            )

            # -------------------------
            # Column customisation UI
            # -------------------------
            with st.expander("Customize columns (optional)", expanded=False):
                st.caption(
                    "Toggle columns on/off, rename headers, and define simple computed columns.\n"
                    "Use raw column names in expressions, e.g. `ORDER_PRICE * ORDER_VOLUME`."
                )

                include_flags = {}
                rename_labels = {}

                # existing columns, with default display titles
                for raw, disp in zip(context["columns"], context["display_columns"]):
                    c1, c2, c3 = st.columns([0.12, 0.33, 0.55])
                    with c1:
                        include_flags[raw] = st.checkbox(
                            "",
                            value=True,
                            key=f"include_{raw}",
                        )
                    with c2:
                        st.text(raw)
                    with c3:
                        rename_labels[raw] = st.text_input(
                            "Header label",
                            value=disp,
                            key=f"label_{raw}",
                        )

                st.markdown("---")
                st.caption("Computed columns (optional):")

                computed_specs = []
                for i in range(3):
                    cc1, cc2, cc3 = st.columns([0.2, 0.3, 0.5])
                    with cc1:
                        cname = st.text_input(
                            f"New column {i + 1} name",
                            key=f"comp_name_{i}",
                        )
                    with cc2:
                        clabel = st.text_input(
                            f"Header label {i + 1}",
                            key=f"comp_label_{i}",
                        )
                    with cc3:
                        cexpr = st.text_input(
                            f"Expression {i + 1}",
                            key=f"comp_expr_{i}",
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

                # Build rules object from UI state
                rules = {
                    "include": include_flags,
                    "rename": rename_labels,
                    "computed": computed_specs,
                }
                        # -------------------------
            # Layout / grouping UI
            # -------------------------
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
                    key="layout_type_app",
                )

                group_by_cols = []
                detail_cols_layout = []

                if layout_choice == "Grouped by column(s)":
                    available_cols = context.get("columns") or []
                    group_by_cols = st.multiselect(
                        "Group by column(s)",
                        options=available_cols,
                        default=[available_cols[0]] if available_cols else [],
                        help="These columns define each group / section (e.g. BROKER_NAME).",
                        key="group_by_app",
                    )

                    detail_cols_layout = st.multiselect(
                        "Columns to show inside each group table",
                        options=available_cols,
                        default=available_cols,
                        help="Typically you keep all detail columns; you can trim this down if needed.",
                        key="detail_cols_app",
                    )

                # This mirrors ReportLayoutOptions in the API version
                layout_dict = {
                    "layout_type": "grouped" if layout_choice == "Grouped by column(s)" else "flat",
                    "group_by": group_by_cols,
                    "detail_columns": detail_cols_layout or [],
                }


            # # Apply customisations to context (hide/rename/compute)
            # context = apply_column_customizations(context, rules)

            # # Exports now use customised context
            # html_report = build_html_report(context)
            # csv_bytes = build_csv_bytes(context["columns"], context["rows"])
            # docx_bytes = build_docx_bytes(context)
            # pdf_bytes = build_report_pdf(context)
            
                        # Apply customisations to context (hide/rename/compute)
            context = apply_column_customizations(context, rules)

            # Apply layout / grouping (flat vs grouped sections)
            context = apply_layout_grouping(context, layout_dict)

            # Exports now use customised + (optionally) grouped context
            html_report = build_html_report(context)
            csv_bytes = build_csv_bytes(context["columns"], context["rows"])
            docx_bytes = build_docx_bytes(context)
            pdf_bytes = build_report_pdf(context)


            st.download_button(
                "⬇️ Download HTML report",
                data=html_report,
                file_name="report.html",
                mime="text/html",
            )

            st.download_button(
                "⬇️ Download CSV",
                data=csv_bytes,
                file_name="data.csv",
                mime="text/csv",
            )

            st.download_button(
                "⬇️ Download DOCX",
                data=docx_bytes,
                file_name="report.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

            st.download_button(
                "⬇️ Download PDF",
                data=pdf_bytes,
                file_name="report.pdf",
                mime="application/pdf",
            )
