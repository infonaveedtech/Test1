"""
Agent 3 — LLM Verifier/Refiner (schema blocks edition)

Inputs:
- ORIGINAL_SQL (from Agent-2)
- SCHEMA_BLOCKS: { "OWNER.TABLE": "<raw text between ## OWNER.TABLE and next ##>" }
- CANONICAL_JOINS: [ "OWNER.T1.COL = OWNER.T2.COL", ... ]  (optional)

Behavior:
- Ask the model to validate identifiers and repair ONLY if needed, using columns that appear inside the given schema blocks.
- Return ONE corrected SQL statement and an optional brief rationale.

Notes:
- Compatible with the same Ollama-like /api/chat used by Agents 1/2.
"""

from __future__ import annotations
import argparse
import os
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Defaults for Agent-3; host comes from env so Docker/prod can override.
DEFAULT_MODEL = os.getenv("LLM_MODEL", "qwen3-coder:30b")
DEFAULT_HOST = os.getenv("LLM_HOST", "http://localhost:11434")
DEFAULT_NUM_CTX = 22000
DEFAULT_TEMP = 0.1

_SQL_RE = re.compile(r"(?is)(?:^|\n)((?:with\s+.+?|select\s+.+?)\s*;)")

OUT_DIR = Path("out")

# ---------- IO helpers ----------
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")

def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8", errors="replace")

def read_json(p: Path):
    return json.loads(read_text(p))

def write_json(p: Path, obj):
    write_text(p, json.dumps(obj, indent=2, ensure_ascii=False))

# ---------- minimal Ollama client ----------
def call_ollama_chat(host: str, model: str, messages, num_ctx: int, temperature: float, debug: bool) -> str:
    url = f"{host.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "num_predict": 4096
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
                print("\n[DEBUG raw]\n", raw[:2000], "\n[/DEBUG]\n")
            obj = json.loads(raw)
            if "message" in obj:
                return obj["message"].get("content", "").strip()
            if "choices" in obj and obj["choices"]:
                return obj["choices"][0].get("message", {}).get("content", "").strip()
            return ""
    except HTTPError as e:
        return f"[HTTP ERROR] {e.code} {e.reason}"
    except URLError as e:
        return f"[NETWORK ERROR] {e.reason}"
    except Exception as e:
        return f"[ERROR] {e}"

# ---------- message builders ----------
def build_messages(system_txt: str,
                   original_sql: str,
                   schema_blocks: Dict[str, str],
                   canonical_joins: List[str]) -> List[Dict[str, str]]:
    # Pack blocks compactly: one header per table, then the raw block
    blocks_text_parts = []
    for tbl, block in schema_blocks.items():
        blocks_text_parts.append(f"## {tbl}\n{block.strip()}\n")
    blocks_text = "\n".join(blocks_text_parts).strip()

    joins_block = "\n".join(f"- {j}" for j in canonical_joins) if canonical_joins else "(none)"

    user_content = (
        "ORIGINAL_SQL:\n"
        f"{original_sql.strip()}\n\n"
        "SCHEMA_BLOCKS (do not use columns outside these blocks):\n"
        f"{blocks_text}\n\n"
        "CANONICAL_JOINS (verbatim; do not deviate):\n"
        f"{joins_block}\n\n"
        "Return exactly one corrected SQL statement ending with a semicolon, "
        "then optionally a brief rationale after the SQL block."
    )
    return [
        {"role": "system", "content": system_txt},
        {"role": "user", "content": user_content},
    ]

# ---------- SQL extraction ----------
def extract_sql_and_rationale(text: str) -> Tuple[str, Optional[str]]:
    t = text.strip()
    # Strip leading code fence if present
    if t.startswith("```"):
        # remove leading fence block headers
        t = re.sub(r"^```(?:sql)?\s*", "", t, flags=re.I)
        # strip trailing fences
        t = re.sub(r"\n```$", "", t)

    m = _SQL_RE.search(t)
    sql = ""
    tail = ""
    if m:
        sql = m.group(1).strip()
        if not sql.endswith(";"):
            sql += ";"
        tail = t[m.end():].strip()
    else:
        # Fallback if model returned raw SQL without our regex cue
        lines = [ln for ln in t.splitlines() if ln.strip()]
        joined = " ".join(lines)
        if joined.lower().startswith(("with ", "select ")):
            sql = joined if joined.endswith(";") else (joined + ";")
        else:
            sql = t

    rationale = None
    if tail:
        r_lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        rationale = " ".join(r_lines[:3]) if r_lines else None

    return sql, rationale

# ---------- public API ----------
def run_agent3_once(
    host: str,
    model: str,
    sql_text: str,
    schema_blocks: Dict[str, str],
    canonical_joins: List[str],
    system_path: Path = Path("prompts/system_agent3_verifier_refiner.md"),
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = DEFAULT_TEMP,
    debug: bool = False,
) -> Tuple[str, Optional[str]]:
    """
    Returns (corrected_sql, rationale).
    Raises ValueError if no usable SQL comes back.
    """
    system_txt = read_text(system_path)
    messages = build_messages(system_txt, sql_text, schema_blocks, canonical_joins)

    # Artifacts
    write_json(OUT_DIR / "agent3_messages.json", messages)
    write_json(OUT_DIR / "agent3_schema_blocks.json", schema_blocks)
    write_json(OUT_DIR / "agent3_canonical_joins.json", canonical_joins)
    write_text(OUT_DIR / "agent3_input_sql.sql", sql_text)

    raw = call_ollama_chat(host, model, messages, num_ctx, temperature, debug)
    if not raw:
        raise ValueError("Agent-3 returned empty response")

    write_text(OUT_DIR / "agent3_raw.txt", raw)

    corrected_sql, rationale = extract_sql_and_rationale(raw)
    if not corrected_sql or not corrected_sql.strip().lower().startswith(("with", "select")):
        raise ValueError("Agent-3 did not return a recognizable SQL statement")

    write_text(OUT_DIR / "agent3_corrected.sql", corrected_sql + ("" if corrected_sql.endswith("\n") else "\n"))
    if rationale:
        write_text(OUT_DIR / "agent3_rationale.txt", rationale + "\n")

    return corrected_sql, rationale

# ---------- CLI (optional) ----------
def main():
    ap = argparse.ArgumentParser(description="Agent 3 — SQL Verifier/Refiner (schema blocks)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP)
    ap.add_argument("--system", default="system_agent3_verifier_refiner.md", help="Prompt filename under prompts/")
    ap.add_argument("--base", default="prompts", help="Base directory containing the system prompt")
    ap.add_argument("--sql", required=True, help="Path to the SQL file produced by Agent-2")
    ap.add_argument("--schema-blocks", required=True, help="Path to JSON mapping {OWNER.TABLE: raw_block_text}")
    ap.add_argument("--canonical-joins", default="", help="Optional path to a JSON array of join lines")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    system_path = Path(args.base) / args.system
    sql_text = read_text(Path(args.sql))
    schema_blocks = read_json(Path(args.schema_blocks))
    canonical_joins = []
    if args.canonical_joins:
        p = Path(args.canonical_joins)
        if p.exists():
            canonical_joins = read_json(p)

    corrected_sql, _ = run_agent3_once(
        host=args.host,
        model=args.model,
        sql_text=sql_text,
        schema_blocks=schema_blocks,
        canonical_joins=canonical_joins,
        system_path=system_path,
        num_ctx=args.num_ctx,
        temperature=args.temp,
        debug=args.debug,
    )
    print(corrected_sql)

if __name__ == "__main__":
    main()