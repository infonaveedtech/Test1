from __future__ import annotations

import json
import time
import tempfile
import uuid
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import retrieve_context
from db.oracle_exec import run_sql

from agents.agent0_intent_llm import (
    run_agent0_once,
    DEFAULT_MODEL as A0_DEFAULT_MODEL,
)
from agents.agent1_planner_llm import (
    build_prompt_from_retrieval,
    run_planner_once,
    DEFAULT_HOST as DEFAULT_LLM_HOST,
    DEFAULT_MODEL as A1_DEFAULT_MODEL,
    BUDGET_SCHEMA as A1_BUDGET_SCHEMA,
    BUDGET_GLOSSARY_EACH as A1_BUDGET_GLOSSARY_EACH,
    BUDGET_FEWSHOT_EACH as A1_BUDGET_FEWSHOT_EACH,
)
from agents.agent2_composer_llm import (
    call_ollama_chat as a2_call_ollama_chat,
    normalize_envelope as a2_normalize_envelope,
    build_messages as a2_build_messages,
    extract_sql as a2_extract_sql,
    DEFAULT_MODEL as A2_DEFAULT_MODEL,
)
from agents.agent3_verifier_refiner_llm import run_agent3_once
from reports.report_utils import build_report_context

# Agent-3 default model (fallback if module doesn't export one)
try:
    from agents.agent3_verifier_refiner_llm import DEFAULT_MODEL as A3_DEFAULT_MODEL
except Exception:
    A3_DEFAULT_MODEL = "qwen3-coder:30b"

# ---------- Paths & constants ----------
BASE = Path(".").resolve()
OUT_DIR = BASE / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_A1 = BASE / "prompts/system_agent1_planner.md"
SYSTEM_A2 = BASE / "prompts/system_agent2_composer.md"
SYSTEM_A3 = BASE / "prompts/system_agent3_verifier_refiner.md"

SCHEMA_CARD = BASE / "prompts/schema_card.md"
GLOSSARY_MD = BASE / "prompts/glossary.md"

GLOSSARY_JSONL = BASE / "data/docs/glossary.docs.jsonl"
FEWSHOTS_JSONL = BASE / "data/docs/fewshots.docs.jsonl"

RETRIEVAL_JSON = OUT_DIR / "retrieval.json"
A1_ENVELOPE_JSON = OUT_DIR / "agent1_envelope.json"
FINAL_SQL_PATH = OUT_DIR / "final.sql"

HEADER_RE = re.compile(r"(?im)^\s*#{2,}\s+([A-Z0-9_]+\s*\.\s*[A-Z0-9_]+)\b.*$")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _safe_read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return default


def _normalize_host(host: Optional[str]) -> str:
    """
    Treat None / empty / 'string' as 'use default host'.
    Ensure we always return a proper http(s) URL.
    """
    if not host or host.strip().lower() == "string":
        host = DEFAULT_LLM_HOST
    host = host.strip()
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host
    return host


def _normalize_model(model: Optional[str], default_model: str) -> str:
    """
    Treat None / empty / 'string' as 'use default model'.
    """
    if not model or model.strip().lower() == "string":
        return default_model
    return model.strip()


# ---------- Agent-3 helper functions (adapted from app.py) ----------

def _qualify_table_name(t: str, default_owner: str = "ATS") -> str:
    t = (t or "").strip()
    if not t:
        return t
    t = re.sub(r"\s*\.\s*", ".", t)
    if "." in t:
        return t
    return f"{default_owner}.{t}"


def _collect_owner_tables_from_envelope(env: dict) -> List[str]:
    tables: List[str] = []
    if isinstance(env, dict):
        tables = env.get("tables") or env.get("table_list") or []
    tables = [_qualify_table_name(t) for t in tables if t]
    # unique, stable
    return sorted({t.upper(): t for t in tables}.values(), key=str.upper)


def _slice_schema_blocks_for_tables(card_text: str, tables: List[str]) -> Dict[str, str]:
    """
    From the big schema_card.md, extract the blocks under each '## OWNER.TABLE' header.
    """
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
    out: Dict[str, str] = {}
    for fq_upper, orig in want.items():
        uq = fq_upper.split(".")[-1]
        if fq_upper in blocks:
            out[orig] = blocks[fq_upper]
        else:
            match_key = next((k for k in blocks.keys() if k.split(".")[-1] == uq), None)
            out[orig] = blocks.get(match_key, "") if match_key else ""
    return out


def _norm_dot(s: str) -> str:
    return re.sub(r"\s*\.\s*", ".", (s or "")).upper().strip()


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


def _columns_from_block(block_text: str) -> List[str]:
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
    cols: List[str] = []
    seen = set()
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


def _columns_from_glossary(gloss_text: str, table_name: str) -> List[str]:
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


def _merge_blocks(schema_blocks: Dict[str, str], glossary_blocks: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for k in schema_blocks.keys() | glossary_blocks.keys():
        a = (schema_blocks.get(k) or "").strip()
        b = (glossary_blocks.get(k) or "").strip()
        merged[k] = a if a else b
    return merged


def _build_allowed_schema(schema_blocks: Dict[str, str], gloss_text: str) -> Dict[str, List[str]]:
    allowed: Dict[str, List[str]] = {}
    for tbl, blk in schema_blocks.items():
        cols = set()
        cols.update(_columns_from_block(blk))
        cols.update(_columns_from_glossary(gloss_text, tbl))
        allowed[tbl] = sorted(cols)
    return allowed


def _light_guard_columns_exist(sql_text: str, allowed_schema: Dict[str, List[str]]) -> List[str]:
    """
    Very light check that OWNER.TABLE.COLUMN triple references exist
    in the allowed_schema.
    """
    bad: List[str] = []
    if not sql_text or not allowed_schema:
        return bad
    triple_re = re.compile(r"\b([A-Z0-9_]+)\.([A-Z0-9_]+)\.([A-Z0-9_]+)\b")
    allowed_norm = {k.upper(): {c.upper() for c in v} for k, v in allowed_schema.items()}
    for owner, table, col in triple_re.findall(sql_text.upper()):
        key = f"{owner}.{table}"
        if key in allowed_norm and col not in allowed_norm[key]:
            bad.append(f"{key}.{col}")
    return bad


# ---------- FastAPI router ----------

router = APIRouter(
    prefix="",
    tags=["pipeline"],
)


class PipelineOptions(BaseModel):
    run_db: bool = Field(
        False,
        description="If true, execute the final SQL on Oracle DB.",
    )
    row_limit: int = Field(
        200,
        ge=1,
        le=5000,
        description="Row limit for DB execution (if run_db=true).",
    )
    save_artifacts: bool = Field(
        False,
        description="If true, write retrieval/envelope/final SQL to ./out (like Streamlit).",
    )
    # Agent-3 is always attempted now; this flag is kept only for backward-compat
    run_agent3: bool = Field(
        False,
        description="Ignored. Agent-3 is always run when possible.",
    )


class PipelineRequest(BaseModel):
    query: str = Field(..., description="User's natural language question.")
    host: Optional[str] = Field(
        None,
        description="LLM host (Ollama/compatible). If omitted, uses project default.",
    )
    model_agent0: Optional[str] = None
    model_agent1: Optional[str] = None
    model_agent2: Optional[str] = None
    model_agent3: Optional[str] = None
    num_ctx: int = Field(
        22000,
        description="Context window for Agent-2 and Agent-3.",
    )
    temperature: float = Field(
        0.1,
        description="Temperature for Agent-2 and Agent-3.",
    )
    options: PipelineOptions = Field(
        default_factory=PipelineOptions,
        description="Execution options (DB, artifacts, etc.).",
    )


@router.post("/pipeline/run")

def run_pipeline_internal(
    *,
    query: str,
    host: Optional[str] = None,
    model_agent0: Optional[str] = None,
    model_agent1: Optional[str] = None,
    model_agent2: Optional[str] = None,
    model_agent3: Optional[str] = None,
    num_ctx: int = 22000,
    temperature: float = 0.1,
    run_db: bool = False,
    row_limit: int = 200,
    save_artifacts: bool = False,
) -> Dict[str, Any]:
    """
    Internal (non-FastAPI) pipeline runner.
    Returns the same 'result' shape as /pipeline/run currently returns.
    """

    overall_t0 = time.perf_counter()

    # normalize host/models same way endpoint does
    host_n = _normalize_host(host)
    model_a0 = _normalize_model(model_agent0, A0_DEFAULT_MODEL)
    model_a1 = _normalize_model(model_agent1, A1_DEFAULT_MODEL)
    model_a2 = _normalize_model(model_agent2, A2_DEFAULT_MODEL)
    model_a3 = _normalize_model(model_agent3, A3_DEFAULT_MODEL)

    result: Dict[str, Any] = {
        "intent": None,
        "rag": None,
        "agent1": None,
        "agent2": None,
        "agent3": None,
        "db": None,
        "meta": {
            "ran_pipeline": False,
            "total_ms": None,
            "models": {
                "agent0": model_a0,
                "agent1": model_a1,
                "agent2": model_a2,
                "agent3": model_a3,
            },
            "errors": [],
        },
    }

    # ---------- 0) Agent-0 ----------
    intent_obj = run_agent0_once(
        host=host_n,
        model=model_a0,
        user_query=query,
        debug=False,
    )
    result["intent"] = intent_obj

    if not intent_obj.get("should_run_pipeline", True):
        overall_t1 = time.perf_counter()
        result["meta"]["ran_pipeline"] = False
        result["meta"]["total_ms"] = int((overall_t1 - overall_t0) * 1000)
        return result

    tmp_retrieval_path: Optional[Path] = None

    # ---------- 1) RAG ----------
    rag_obj = retrieve_context.retrieve_context(query)
    result["rag"] = rag_obj

    if save_artifacts:
        _write_json(RETRIEVAL_JSON, rag_obj)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    try:
        tmp.write(json.dumps(rag_obj, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        tmp.close()
    tmp_retrieval_path = Path(tmp.name)

    # ---------- 2) Agent-1 ----------
    try:
        a1_messages, retrieval_obj, gl_expanded, fs_expanded = build_prompt_from_retrieval(
            system_path=SYSTEM_A1,
            schema_path=SCHEMA_CARD,
            retrieval_json_path=tmp_retrieval_path,
            glossary_jsonl_path=GLOSSARY_JSONL,
            fewshots_jsonl_path=FEWSHOTS_JSONL,
            budget_schema=A1_BUDGET_SCHEMA,
            budget_glossary_each=A1_BUDGET_GLOSSARY_EACH,
            budget_fewshot_each=A1_BUDGET_FEWSHOT_EACH,
            save_artifacts=save_artifacts,
        )

        envelope = run_planner_once(
            host=host_n,
            model=model_a1,
            messages=a1_messages,
            retrieval_obj=retrieval_obj,
            glossary_expanded=gl_expanded,
            debug=False,
        )

        if save_artifacts:
            _write_json(A1_ENVELOPE_JSON, envelope)

        result["agent1"] = {"envelope": envelope, "messages": a1_messages}

    finally:
        if tmp_retrieval_path is not None:
            try:
                tmp_retrieval_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ---------- 3) Agent-2 ----------
    env_norm = a2_normalize_envelope(envelope)

    system_a2 = _safe_read(
        SYSTEM_A2,
        default=(
            "You are Agent-2. Produce one Oracle SQL statement. "
            "No prose. No markdown. End with a semicolon."
        ),
    )

    a2_messages = a2_build_messages(system_a2, query, env_norm)

    raw_a2 = a2_call_ollama_chat(
        host=host_n,
        model=model_a2,
        messages=a2_messages,
        num_ctx=int(num_ctx),
        temperature=float(temperature),
        debug=False,
    )

    agent2_sql = a2_extract_sql(raw_a2).strip()
    if not agent2_sql:
        raise RuntimeError("Agent-2 did not return a SQL statement.")

    result["agent2"] = {"sql": agent2_sql, "raw_llm_output": raw_a2, "messages": a2_messages}

    # ---------- 4) Agent-3 ----------
    card_text = _safe_read(SCHEMA_CARD, default="")
    gloss_text = _safe_read(GLOSSARY_MD, default="")
    used_tables = _collect_owner_tables_from_envelope(envelope)

    if not used_tables:
        result["agent3"] = {
            "ran": False,
            "reason": "Agent-1 envelope did not list any tables; Agent-3 skipped.",
            "sql": agent2_sql,
        }
        final_sql = agent2_sql
    else:
        try:
            schema_blocks_card = _slice_schema_blocks_for_tables(card_text, used_tables)
            glossary_blocks = {t: _glossary_block_for_table(gloss_text, t) for t in used_tables}
            schema_blocks = _merge_blocks(schema_blocks_card, glossary_blocks)
            allowed_schema = _build_allowed_schema(schema_blocks, gloss_text)

            corrected_sql, rationale = run_agent3_once(
                host=host_n,
                model=model_a3,
                sql_text=agent2_sql,
                schema_blocks=schema_blocks,
                canonical_joins=[],
                system_path=SYSTEM_A3,
                num_ctx=int(num_ctx),
                temperature=float(temperature),
                debug=False,
            )

            post_invalid = _light_guard_columns_exist(corrected_sql, allowed_schema)
            if post_invalid:
                raise RuntimeError(f"Agent-3 referenced invalid columns: {', '.join(post_invalid)}")

            final_sql = corrected_sql
            result["agent3"] = {
                "ran": True,
                "sql": corrected_sql,
                "rationale": rationale,
                "schema_blocks": schema_blocks,
            }

        except Exception as e:
            result["meta"]["errors"].append(f"Agent-3 error: {e}")
            result["agent3"] = {"ran": False, "reason": f"Agent-3 failed: {e}", "sql": agent2_sql}
            final_sql = agent2_sql

    result["final_sql"] = final_sql  # ✅ add this convenience key

    if save_artifacts:
        FINAL_SQL_PATH.write_text(final_sql + "\n", encoding="utf-8")

    # ---------- 5) DB (optional) ----------
    if run_db:
        t0 = time.perf_counter()
        db_res = run_sql(final_sql, row_limit=row_limit)
        exec_ms = int((time.perf_counter() - t0) * 1000)
        result["db"] = {
            "columns": db_res.get("columns", []),
            "rows": db_res.get("rows", []),
            "row_limit": db_res.get("row_limit", row_limit),
            "exec_ms": exec_ms,
        }

    overall_t1 = time.perf_counter()
    result["meta"]["ran_pipeline"] = True
    result["meta"]["total_ms"] = int((overall_t1 - overall_t0) * 1000)
    return result
 


def pipeline_run_endpoint(body: PipelineRequest) -> Dict[str, Any]:
    """
    Run the full NL→SQL pipeline:

    1. Agent-0 (intent gate)
    2. RAG retrieval
    3. Agent-1 (planner) -> JSON envelope
    4. Agent-2 (composer) -> SQL
    5. Agent-3 (verifier/refiner) -> validated SQL
    6. Optional: Execute SQL on Oracle (run_db option)
    """
    
    return run_pipeline_internal(
        query=body.query,
        host=body.host,
        model_agent0=body.model_agent0,
        model_agent1=body.model_agent1,
        model_agent2=body.model_agent2,
        model_agent3=body.model_agent3,
        num_ctx=body.num_ctx,
        temperature=body.temperature,
        run_db=body.options.run_db,
        row_limit=body.options.row_limit,
        save_artifacts=body.options.save_artifacts,
    )
    # overall_t0 = time.perf_counter()

    # host = _normalize_host(body.host)
    # model_a0 = _normalize_model(body.model_agent0, A0_DEFAULT_MODEL)
    # model_a1 = _normalize_model(body.model_agent1, A1_DEFAULT_MODEL)
    # model_a2 = _normalize_model(body.model_agent2, A2_DEFAULT_MODEL)
    # model_a3 = _normalize_model(body.model_agent3, A3_DEFAULT_MODEL)

    # result: Dict[str, Any] = {
    #     "intent": None,
    #     "rag": None,
    #     "agent1": None,
    #     "agent2": None,
    #     "agent3": None,
    #     "db": None,
    #     "meta": {
    #         "ran_pipeline": False,
    #         "total_ms": None,
    #         "models": {
    #             "agent0": model_a0,
    #             "agent1": model_a1,
    #             "agent2": model_a2,
    #             "agent3": model_a3,
    #         },
    #         "errors": [],
    #     },
    # }

    # # ---------- 0) Agent-0 ----------
    # try:
    #     intent_obj = run_agent0_once(
    #         host=host,
    #         model=model_a0,
    #         user_query=body.query,
    #         debug=False,
    #     )
    #     result["intent"] = intent_obj
    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"Agent-0 error: {e}") from e

    # if not intent_obj.get("should_run_pipeline", True):
    #     overall_t1 = time.perf_counter()
    #     result["meta"]["ran_pipeline"] = False
    #     result["meta"]["total_ms"] = int((overall_t1 - overall_t0) * 1000)
    #     return result

    # # temporary per-request retrieval file for Agent-1
    # tmp_retrieval_path: Optional[Path] = None

    # # ---------- 1) RAG ----------
    #     # ---------- 1) RAG ----------
    # try:
    #     rag_obj = retrieve_context.retrieve_context(body.query)
    #     result["rag"] = rag_obj

    #     # Optional persistent artifact for debugging (only if user asked for it)
    #     if body.options.save_artifacts:
    #         _write_json(RETRIEVAL_JSON, rag_obj)

    #     # Per-request temp file for Agent-1 (avoids shared-file races / permissions issues)
    #     tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    #     try:
    #         tmp.write(json.dumps(rag_obj, ensure_ascii=False, indent=2).encode("utf-8"))
    #     finally:
    #         tmp.close()
    #     tmp_retrieval_path = Path(tmp.name)

    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"RAG error: {e}") from e

    #     # ---------- 2) Agent-1 (Planner) ----------
    # try:
    #     if tmp_retrieval_path is None:
    #         raise RuntimeError("Internal error: retrieval temp file was not created.")

    #     a1_messages, retrieval_obj, gl_expanded, fs_expanded = build_prompt_from_retrieval(
    #         system_path=SYSTEM_A1,
    #         schema_path=SCHEMA_CARD,
    #         retrieval_json_path=tmp_retrieval_path,
    #         glossary_jsonl_path=GLOSSARY_JSONL,
    #         fewshots_jsonl_path=FEWSHOTS_JSONL,
    #         budget_schema=A1_BUDGET_SCHEMA,
    #         budget_glossary_each=A1_BUDGET_GLOSSARY_EACH,
    #         budget_fewshot_each=A1_BUDGET_FEWSHOT_EACH,
    #         save_artifacts=body.options.save_artifacts,
    #     )

    #     envelope = run_planner_once(
    #         host=host,
    #         model=model_a1,
    #         messages=a1_messages,
    #         retrieval_obj=retrieval_obj,
    #         glossary_expanded=gl_expanded,
    #         debug=False,
    #     )

    #     if body.options.save_artifacts:
    #         _write_json(A1_ENVELOPE_JSON, envelope)

    #     result["agent1"] = {
    #         "envelope": envelope,
    #         "messages": a1_messages,
    #     }

    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"Agent-1 error: {e}") from e
    # finally:
    #     # Clean up per-request retrieval file
    #     if tmp_retrieval_path is not None:
    #         try:
    #             tmp_retrieval_path.unlink(missing_ok=True)
    #         except Exception:
    #             # Not fatal – we just don't want to crash because of temp cleanup
    #             pass

    # # ---------- 3) Agent-2 (Composer) ----------
    # try:
    #     env_norm = a2_normalize_envelope(envelope)

    #     system_a2 = _safe_read(
    #         SYSTEM_A2,
    #         default=(
    #             "You are Agent-2. Produce one Oracle SQL statement. "
    #             "No prose. No markdown. End with a semicolon."
    #         ),
    #     )

    #     a2_messages = a2_build_messages(system_a2, body.query, env_norm)

    #     raw_a2 = a2_call_ollama_chat(
    #         host=host,
    #         model=model_a2,
    #         messages=a2_messages,
    #         num_ctx=int(body.num_ctx),
    #         temperature=float(body.temperature),
    #         debug=False,
    #     )

    #     agent2_sql = a2_extract_sql(raw_a2).strip()
    #     if not agent2_sql:
    #         raise RuntimeError("Agent-2 did not return a SQL statement.")

    #     result["agent2"] = {
    #         "sql": agent2_sql,
    #         "raw_llm_output": raw_a2,
    #         "messages": a2_messages,
    #     }

    # except Exception as e:
    #     raise HTTPException(status_code=500, detail=f"Agent-2 error: {e}") from e

    # # ---------- 4) Agent-3 (Verifier / Refiner) ----------
    # card_text = _safe_read(SCHEMA_CARD, default="")
    # gloss_text = _safe_read(GLOSSARY_MD, default="")
    # used_tables = _collect_owner_tables_from_envelope(envelope)

    # if not used_tables:
    #     # No tables → we can't meaningfully validate; just return Agent-2 SQL
    #     result["agent3"] = {
    #         "ran": False,
    #         "reason": "Agent-1 envelope did not list any tables; Agent-3 skipped.",
    #         "sql": agent2_sql,
    #     }
    #     final_sql = agent2_sql
    # else:
    #     try:
    #         schema_blocks_card = _slice_schema_blocks_for_tables(card_text, used_tables)
    #         glossary_blocks = {t: _glossary_block_for_table(gloss_text, t) for t in used_tables}
    #         schema_blocks = _merge_blocks(schema_blocks_card, glossary_blocks)
    #         allowed_schema = _build_allowed_schema(schema_blocks, gloss_text)

    #         canonical_joins: List[str] = []  # you can fill this later if you want

    #         corrected_sql, rationale = run_agent3_once(
    #             host=host,
    #             model=model_a3,
    #             sql_text=agent2_sql,
    #             schema_blocks=schema_blocks,
    #             canonical_joins=canonical_joins,
    #             system_path=SYSTEM_A3,
    #             num_ctx=int(body.num_ctx),
    #             temperature=float(body.temperature),
    #             debug=False,
    #         )

    #         post_invalid = _light_guard_columns_exist(corrected_sql, allowed_schema)
    #         if post_invalid:
    #             raise RuntimeError(
    #                 f"Agent-3 referenced invalid columns: {', '.join(post_invalid)}"
    #             )

    #         final_sql = corrected_sql

    #         result["agent3"] = {
    #             "ran": True,
    #             "sql": corrected_sql,
    #             "rationale": rationale,
    #             "schema_blocks": schema_blocks,
    #         }

    #     except Exception as e:
    #         # If Agent-3 fails, we still want to return something;
    #         # fall back to Agent-2 SQL but expose the error.
    #         result["meta"]["errors"].append(f"Agent-3 error: {e}")
    #         result["agent3"] = {
    #             "ran": False,
    #             "reason": f"Agent-3 failed: {e}",
    #             "sql": agent2_sql,
    #         }
    #         final_sql = agent2_sql

    # # Save final SQL artifact if requested
    # if body.options.save_artifacts:
    #     FINAL_SQL_PATH.write_text(final_sql + "\n", encoding="utf-8")

    # # ---------- 5) DB execution (optional) ----------
    # if body.options.run_db:
    #     try:
    #         t0 = time.perf_counter()
    #         db_res = run_sql(final_sql, row_limit=body.options.row_limit)
    #         exec_ms = int((time.perf_counter() - t0) * 1000)

    #         result["db"] = {
    #             "columns": db_res.get("columns", []),
    #             "rows": db_res.get("rows", []),
    #             "row_limit": db_res.get("row_limit", body.options.row_limit),
    #             "exec_ms": exec_ms,
    #         }

    #     except Exception as e:
    #         raise HTTPException(status_code=500, detail=f"DB error: {e}") from e

    # overall_t1 = time.perf_counter()
    # result["meta"]["ran_pipeline"] = True
    # result["meta"]["total_ms"] = int((overall_t1 - overall_t0) * 1000)

    # return result
