#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent 1 — LLM Planner (RAG-aware, robust JSON + rich debugging)

What this file does:
- Build a compact prompt for Agent-1 (Planner) from:
  • system_agent1_planner.md (strict JSON contract)
  • schema_card.md (full schema; clipped)
  • retrieval JSON (few-shots + glossary kit produced by your FAISS retriever)
- Call your local LLM (Ollama-like /api/chat or compatible endpoint)
- Parse exactly ONE JSON envelope (aggressive recovery)
- Light validation & safe auto-repair (row_limit, grain)
- Persist all debug artifacts under ./out/ for inspection

Design:
- Retrieval is OUTSIDE this file.
- Small modular funcs; main() is hardcoded for quick tests.
"""

from __future__ import annotations
import os
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import dotenv
dotenv.load_dotenv()  # load .env if present
# -------------------------------
# Defaults (used by hardcoded main)
# -------------------------------
# NOTE:
# - In dev: these will read from your .env (LLM_HOST / LLM_MODEL),
#   falling back to the old hardcoded values if not set.
# - In Docker/prod: we will override LLM_HOST/LLM_MODEL via environment.
DEFAULT_HOST = os.getenv("LLM_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("LLM_MODEL", "qwen3-coder:30b")
DEFAULT_NUM_CTX = 22000
DEFAULT_TEMPERATURE = 0.1


DEFAULT_SYSTEM_PATH = Path("prompts/system_agent1_planner.md")
DEFAULT_SCHEMA_PATH = Path("prompts/schema_card.md")
DEFAULT_RETRIEVAL_JSON = Path("out/retrieval.json")   # <- produced by your retrieve_context.py
DEFAULT_GLOSSARY_JSONL = Path("data/docs/glossary.docs.jsonl")
DEFAULT_FEWSHOTS_JSONL = Path("data/docs/fewshots.docs.jsonl")

# Budgets (characters)
BUDGET_SCHEMA = 16000
BUDGET_GLOSSARY_EACH = 12000
BUDGET_FEWSHOT_EACH = 14000

OUT_DIR = Path("out")

ALLOWED_GRAINS = {"none", "day", "month", "year"}

# -------------------------------
# Utilities
# -------------------------------

def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")

def write_json(path: Path, obj: Any, pretty: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        write_text(path, json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        write_text(path, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"[WARN] Could not read {p}: {e}"

def clip(txt: str, max_chars: int) -> str:
    if max_chars <= 0 or len(txt) <= max_chars:
        return txt
    return txt[:max_chars] + "\n...[TRUNCATED]..."

def load_jsonl_map(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Load a JSONL file into a map {id: object} for quick lookup.
    Useful to expand retrieval IDs to full content.
    """
    m: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return m
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        m[obj["id"]] = obj
    return m

def strip_code_fences(t: str) -> str:
    s = t.strip()
    if s.startswith("```"):
        # remove fences like ```json ... ```
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    return s

# -------------------------------
# Robust JSON extraction
# -------------------------------

def try_parse_json(s: str) -> Optional[Dict[str, Any]]:
    try:
        x = json.loads(s)
        if isinstance(x, dict):
            return x
        # If the top-level is a JSON string that itself contains JSON, decode twice
        if isinstance(x, str):
            try:
                y = json.loads(x)
                if isinstance(y, dict):
                    return y
            except Exception:
                return None
        return None
    except Exception:
        return None

def find_largest_json_object(s: str) -> Optional[str]:
    """
    Scan for the largest balanced {...} region.
    Helps when the model wraps JSON with prose.
    """
    stack = []
    best = None
    start_idx = None
    for i, ch in enumerate(s):
        if ch == "{":
            if not stack:
                start_idx = i
            stack.append("{")
        elif ch == "}":
            if stack:
                stack.pop()
                if not stack and start_idx is not None:
                    candidate = s[start_idx:i+1]
                    if best is None or len(candidate) > len(best):
                        best = candidate
                    start_idx = None
    return best

def first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Aggressive JSON recovery:
      1) direct parse
      2) strip code fences & reparse
      3) double-decoding (JSON string containing JSON)
      4) scan for largest {...} block and parse that
    """
    # 1) direct
    j = try_parse_json(text)
    if j is not None:
        return j

    # 2) de-fence
    s = strip_code_fences(text)
    j = try_parse_json(s)
    if j is not None:
        return j

    # 3) largest balanced block
    block = find_largest_json_object(s)
    if block:
        j = try_parse_json(block)
        if j is not None:
            return j

    return None

# -------------------------------
# Renderers (build prompt sections)
# -------------------------------

def render_glossary_kit(
    gl_docs: List[Dict[str, Any]],
    glossary_map: Dict[str, Dict[str, Any]],
    max_chars_each: int = BUDGET_GLOSSARY_EACH,
) -> str:
    """
    Turn retrieved glossary docs (IDs or inline objects) into a compact textual kit section.
    Each item: Title, (Chapter/Type), Content (short), Canonical joins (verbatim), Date semantics (cols + grains).
    """
    parts: List[str] = []
    for d in gl_docs:
        src = glossary_map.get(d.get("id", ""), d)  # prefer JSONL, else provided obj
        title = src.get("title") or d.get("title") or src.get("id") or d.get("id") or "Untitled"
        chapter = src.get("chapter") or d.get("chapter")
        doc_type = src.get("doc_type") or d.get("doc_type")
        content = src.get("content") or d.get("content") or ""
        cj = (
            src.get("canonical_joins")
            or src.get("canonical_joins_snippets")
            or d.get("canonical_joins")
            or []
        )
        ds = src.get("date_semantics") or d.get("date_semantics") or None

        block: List[str] = [
            f"### {title}",
            f"(chapter: {chapter}; type: {doc_type})",
        ]
        if content:
            block.append(clip(content.strip(), max_chars_each))
        if cj:
            block.append("Canonical joins (verbatim):")
            for line in cj:
                block.append(f"- {line}")
        if ds:
            grains = ", ".join(ds.get("grains_allowed") or [])
            prim = ", ".join(ds.get("primary_date_cols") or [])
            note = ds.get("notes") or ""
            block.append(
                f"Date semantics: primary_date_cols=[{prim}] grains=[{grains}] {note}".strip()
            )
        parts.append("\n".join(block))

    return "\n\n".join(parts)

def render_fewshots(
    fs_docs: List[Dict[str, Any]],
    fewshots_map: Dict[str, Dict[str, Any]],
    max_chars_each: int = BUDGET_FEWSHOT_EACH,
) -> str:
    """
    For each few-shot: Title, Question, core SQL only (trimmed).
    """
    out: List[str] = []
    for d in fs_docs:
        src = fewshots_map.get(d.get("id", ""), d)
        title = src.get("title") or d.get("title") or src.get("id") or d.get("id") or "Untitled"
        question = src.get("question") or d.get("question") or ""
        sql = (src.get("sql_text") or d.get("sql_text") or "").strip()
        out.append(f"### {title}\nQuestion: {question}\nSQL:\n{clip(sql, max_chars_each)}")
    return "\n\n".join(out)

def build_messages(
    system_txt: str,
    schema_txt: str,
    user_query: str,
    glossary_kit_txt: str,
    fewshots_txt: str,
) -> List[Dict[str, str]]:
    """
    Construct the OpenAI-style messages list.
    """
    user_content = (
        "User Query:\n"
        + user_query.strip()
        + "\n\n<SCHEMA_CARD>\n"
        + schema_txt
        + "\n</SCHEMA_CARD>\n"
        + "<RETRIEVED_GLOSSARY_KIT>\n"
        + glossary_kit_txt
        + "\n</RETRIEVED_GLOSSARY_KIT>\n"
        + "<RETRIEVED_FEWSHOTS>\n"
        + fewshots_txt
        + "\n</RETRIEVED_FEWSHOTS>\n"
        + "\nReturn ONLY the JSON envelope object described in the system prompt."
    )
    return [
        {"role": "system", "content": system_txt},
        {"role": "user", "content": user_content},
    ]

# -------------------------------
# LLM client
# -------------------------------
def llm_chat(host, model, messages, num_ctx, temperature, debug=False):
    """Simple Ollama /api/chat client — minimal and reliable."""
    import json
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "num_predict": 2048
        },
        "stream": False,
        "keep_alive": "5m"
    }
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=900) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            if debug:
                print("\n[DEBUG raw]\n", raw[:1000], "\n[/DEBUG]\n")
            obj = json.loads(raw)
            if "message" in obj:
                return (obj["message"].get("content") or "").strip()
            if "choices" in obj and obj["choices"]:
                return (obj["choices"][0].get("message", {}).get("content") or "").strip()
            return ""
    except HTTPError as e:
        print(f"[HTTP ERROR] {e.code} {e.reason}")
        return ""
    except URLError as e:
        print(f"[NETWORK ERROR] {e.reason}")
        return ""
    except Exception as e:
        print(f"[ERROR] {e}")
        return ""

# -------------------------------
# Envelope validation
# -------------------------------

def validate_and_autofix_envelope(
    env: Dict[str, Any],
    glossary_docs_used: List[Dict[str, Any]],
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """
    Light validation of Agent-1 envelope + safe auto-fixes.
    Returns (ok, warnings, fixed_env)
    """
    warnings: List[str] = []
    fixed = dict(env)  # shallow copy

    # 1) row_limit
    if "row_limit" not in fixed or not isinstance(fixed.get("row_limit"), int) or fixed["row_limit"] <= 0:
        fixed["row_limit"] = 200
        warnings.append("row_limit missing or invalid; set to 200.")

    # 2) grain
    g = str(fixed.get("grain", "none")).lower()
    if g not in ALLOWED_GRAINS:
        warnings.append(f"grain '{fixed.get('grain')}' not in {ALLOWED_GRAINS}; set to 'none'.")
        fixed["grain"] = "none"
    else:
        fixed["grain"] = g

    # 3) date_key present
    if not fixed.get("date_key"):
        warnings.append("date_key missing.")

    # 4) joins exist
    joins = fixed.get("required_joins_verbatim") or []
    if not isinstance(joins, list) or not joins:
        warnings.append("required_joins_verbatim missing or empty.")

    # 5) tables match join references (weak check)
    tables = set(fixed.get("tables") or [])
    for j in joins or []:
        # naive parse: take tokens that look like TABLE.COLUMN
        for tok in re.findall(r"\b([A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b", j):
            table = tok.split(".")[0]
            if table not in tables:
                warnings.append(f"Join references table '{table}' not present in 'tables' list.")

    ok = ("date_key" in fixed and fixed["date_key"]) and (isinstance(joins, list) and len(joins) > 0)
    return ok, warnings, fixed

# -------------------------------
# Orchestration
# -------------------------------

def build_prompt_from_retrieval(
    system_path: Path,
    schema_path: Path,
    retrieval_json_path: Path,
    glossary_jsonl_path: Path,
    fewshots_jsonl_path: Path,
    budget_schema: int = BUDGET_SCHEMA,
    budget_glossary_each: int = BUDGET_GLOSSARY_EACH,
    budget_fewshot_each: int = BUDGET_FEWSHOT_EACH,
    save_artifacts: bool = True,
) -> Tuple[List[Dict[str, str]], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Expand retrieval JSON -> build messages for Agent-1 (Planner).
    Returns (messages, retrieval_obj, glossary_docs_expanded, fewshots_docs_expanded)
    """
    # Load core texts
    system_txt = read_text(system_path)
    schema_txt = clip(read_text(schema_path), budget_schema)

    # Load retrieval JSON
    retrieval = json.loads(retrieval_json_path.read_text(encoding="utf-8"))
    user_query = retrieval.get("query") or ""

    # Expand IDs -> content
    gl_map = load_jsonl_map(glossary_jsonl_path)
    fs_map = load_jsonl_map(fewshots_jsonl_path)

    gl_list = retrieval.get("glossary", []) or []
    fs_list = retrieval.get("fewshots", []) or []

    glossary_kit_txt = render_glossary_kit(gl_list, gl_map, max_chars_each=budget_glossary_each)
    fewshots_txt = render_fewshots(fs_list, fs_map, max_chars_each=budget_fewshot_each)

    messages = build_messages(
        system_txt=system_txt,
        schema_txt=schema_txt,
        user_query=user_query,
        glossary_kit_txt=glossary_kit_txt,
        fewshots_txt=fewshots_txt,
    )
    if save_artifacts:
        # persist debug artifacts
        write_json(OUT_DIR / "agent1_messages.json", messages, pretty=True)
        write_text(OUT_DIR / "agent1_prompt.txt", messages[-1]["content"])

    # Return expanded (so caller can validate/cite)
    gl_expanded = [gl_map.get(d.get("id",""), d) for d in gl_list]
    fs_expanded = [fs_map.get(d.get("id",""), d) for d in fs_list]

    return messages, retrieval, gl_expanded, fs_expanded

def run_planner_once(
    host: str,
    model: str,
    messages: List[Dict[str, str]],
    retrieval_obj: Dict[str, Any],
    glossary_expanded: List[Dict[str, Any]],
    debug: bool = True,
) -> Dict[str, Any]:
    """
    Call LLM, parse JSON envelope, validate & return.
    Auto-retries once with a stricter JSON-only reminder if parsing fails.
    """
    def call_and_parse(msgs: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        raw = llm_chat(host=host, model=model, messages=msgs, num_ctx=DEFAULT_NUM_CTX,
                       temperature=DEFAULT_TEMPERATURE, debug=debug)
        if not raw:
            return None
        env = first_json_object(raw)
        if env is None:
            write_text(OUT_DIR / "agent1_raw.txt", raw)
        return env

    # 1st attempt
    env = call_and_parse(messages)

    # If parse failed, retry once with a stricter nudge
    if env is None:
        retry_messages = list(messages)
        retry_messages[-1] = {
            "role": "user",
            "content": messages[-1]["content"]
                       + "\n\nSTRICT FORMAT REMINDER: Return ONLY one valid JSON object. "
                         "Do NOT include prose, markdown, or code fences."
        }
        write_json(OUT_DIR / "agent1_messages_retry.json", retry_messages, pretty=True)
        env = call_and_parse(retry_messages)

    if env is None:
        raise ValueError("Could not parse JSON envelope. Raw saved to out/agent1_raw.txt")

    ok, warnings, fixed = validate_and_autofix_envelope(env, glossary_expanded)

    # attach citations if missing
    if "retrieval_citations" not in fixed:
        fixed["retrieval_citations"] = {
            "fewshots": [d.get("id") for d in retrieval_obj.get("fewshots", []) if d.get("id")],
            "glossary": [d.get("id") for d in retrieval_obj.get("glossary", []) if d.get("id")],
        }

    # fixed["__validation_warnings__"] = warnings
    fixed["__ok__"] = ok
    return fixed

# -------------------------------
# Hardcoded testable main()
# -------------------------------

def main():
    """
    Hardcoded test runner (so you can just run this file to test Agent-1).
    Replace paths below if needed.
    """
    host = DEFAULT_HOST
    model = DEFAULT_MODEL

    system_path = DEFAULT_SYSTEM_PATH
    schema_path = DEFAULT_SCHEMA_PATH
    retrieval_json_path = DEFAULT_RETRIEVAL_JSON
    glossary_jsonl_path = DEFAULT_GLOSSARY_JSONL
    fewshots_jsonl_path = DEFAULT_FEWSHOTS_JSONL

    # 1) Build messages from retrieval JSON
    messages, retrieval_obj, gl_expanded, fs_expanded = build_prompt_from_retrieval(
        system_path=system_path,
        schema_path=schema_path,
        retrieval_json_path=retrieval_json_path,
        glossary_jsonl_path=glossary_jsonl_path,
        fewshots_jsonl_path=fewshots_jsonl_path,
        budget_schema=BUDGET_SCHEMA,
        budget_glossary_each=BUDGET_GLOSSARY_EACH,
        budget_fewshot_each=BUDGET_FEWSHOT_EACH,
    )

    # 2) Call LLM Planner once (with robust parse + retry)
    envelope = run_planner_once(
        host=host,
        model=model,
        messages=messages,
        retrieval_obj=retrieval_obj,
        glossary_expanded=gl_expanded,
        debug=True,
    )

    # 3) Print & save
    out_path = OUT_DIR / "agent1_envelope.json"
    write_text(out_path, json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(envelope, indent=2, ensure_ascii=False))
    print(f"\n[SAVED] Envelope written to: {out_path}")

if __name__ == "__main__":
    main()
