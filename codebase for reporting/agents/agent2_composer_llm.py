# !/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent 2 — LLM Composer (Ollama)
- Reads: Agent 1 JSON envelope + user query + system prompt for Agent 2
- Normalizes the envelope (schema/table/column qualifiers)
- Calls Ollama /api/chat with your model
- Prints ONLY the final SQL statement to stdout
"""

import argparse
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DEFAULT_MODEL = "qwen3-coder:30b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_NUM_CTX = 32536
DEFAULT_TEMP = 0.1

_SQL_RE = re.compile(r"(?is)(?:^|\n)((?:with\s+.+?|select\s+.+?)\s*;)")

# ---------------- util IO ----------------
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")

def read_json(p: Path):
    return json.loads(read_text(p))

# ---------------- minimal Ollama client ----------------
def call_ollama_chat(host: str, model: str, messages, num_ctx: int, temperature: float, debug: bool):
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
                print("\n[DEBUG raw]")
                print(raw[:2000])
                print("[/DEBUG]\n")
            obj = json.loads(raw)
            if "message" in obj:
                return obj["message"].get("content", "").strip()
            if "choices" in obj and obj["choices"]:
                return obj["choices"][0].get("message", {}).get("content","").strip()
            return ""
    except HTTPError as e:
        return f"[HTTP ERROR] {e.code} {e.reason}"
    except URLError as e:
        return f"[NETWORK ERROR] {e.reason}"
    except Exception as e:
        return f"[ERROR] {e}"

# ---------------- envelope normalizer ----------------
UPPER_ID = re.compile(r"^[A-Za-z0-9_]+$")

def _qualify_table(t: str) -> str:
    t = t.strip()
    if "." in t:
        # already owner-qualified (assume correct)
        return t
    # default owner is ATS
    return f"ATS.{t}"

def _ensure_table_col(s: str, fallback_table: str) -> str:
    s = s.strip()
    if "." in s:
        return s  # already table.col
    # attach fallback table (no owner here; envelope can be two-part)
    # if fallback is owner-qualified, keep only the table part
    tbl = fallback_table.split(".")[-1]
    return f"{tbl}.{s}"

def _qualify_join_line(j: str) -> str:
    """
    Convert BEST_MKT.EXCHANGE_ID = EXCHANGES.EXCHANGE_ID
    --> ATS.BEST_MKT.EXCHANGE_ID = ATS.EXCHANGES.EXCHANGE_ID
    (idempotent if already ATS.)
    """
    def qtok(tok: str) -> str:
        tok = tok.strip()
        if tok.count(".") == 2:
            return tok  # ATS.TABLE.COL
        if tok.count(".") == 1:
            tbl, col = tok.split(".")
            if "." in tbl:  # weird case
                return tok
            return f"ATS.{tbl}.{col}"
        return tok

    # split around '=' and re-join
    parts = j.split("=")
    if len(parts) != 2:
        return j
    left = qtok(parts[0].strip())
    right = qtok(parts[1].strip())
    return f"{left} = {right}"

def normalize_envelope(env: dict) -> dict:
    """
    - Qualify tables to ATS.<TABLE>
    - Ensure date_key is <TABLE>.<COL> (two-part)
    - Qualify group_by items to <TABLE>.<COL> (two-part)
    - Qualify join lines to ATS.<TABLE>.<COL> = ATS.<TABLE>.<COL>
    """
    fixed = dict(env)

    # tables
    tables = fixed.get("tables") or []
    tables = [_qualify_table(t) for t in tables]
    fixed["tables"] = tables

    # pick a fallback fact table for date_key / group_by if needed
    fallback_table = tables[0] if tables else "ATS.BEST_MKT"

    # date_key
    if fixed.get("date_key"):
        fixed["date_key"] = _ensure_table_col(fixed["date_key"], fallback_table)

    # group_by
    gb = fixed.get("group_by") or []
    fixed["group_by"] = [_ensure_table_col(g, fallback_table) for g in gb]

    # required_joins_verbatim
    rj = fixed.get("required_joins_verbatim") or []
    fixed["required_joins_verbatim"] = [_qualify_join_line(j) for j in rj]

    return fixed

# ---------------- prompt builder ----------------
def build_messages(system_txt: str, user_query: str, envelope_obj: dict):
    """
    Keep it minimal: system prompt defines all rules.
    User message supplies the query + the Agent-1 envelope.
    """
    user_content = (
        "User Query:\n"
        + user_query.strip() + "\n\n"
        "Agent1 Envelope (JSON):\n"
        + json.dumps(envelope_obj, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
        "Return EXACTLY ONE Oracle SQL statement that answers the User Query using ONLY the information above. "
        "Start with WITH or SELECT and end with a semicolon. No explanations, no markdown, no extra text."
    )
    return [
        {"role": "system", "content": system_txt},
        {"role": "user", "content": user_content}
    ]

# ---------------- SQL extractor ----------------
def extract_sql(text: str) -> str:
    t = text.strip()
    # strip simple code fences if present
    if t.startswith("```"):
        t = t.strip("`")
        t = re.sub(r"^\s*sql\s*", "", t, flags=re.I)
    m = _SQL_RE.search(t)
    if m:
        sql = m.group(1).strip()
        return sql if sql.endswith(";") else sql + ";"
    # fallback: if it already looks like SQL but missing semicolon
    lines = [ln for ln in t.splitlines() if ln.strip()]
    joined = " ".join(lines)
    if joined.lower().startswith(("with ", "select ")):
        return joined if joined.endswith(";") else joined + ";"
    return t  # last resort: print whatever came back

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description="Agent 2 — LLM Composer (prints ONLY SQL)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP)
    ap.add_argument("--base", default=".")
    ap.add_argument("--system", default="system_agent2_composer.md")
    ap.add_argument("--envelope", required=True, help="Path to Agent 1 JSON output")
    ap.add_argument("--query", required=True, help="User natural-language query")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    base = Path(args.base).expanduser().resolve()
    system_txt = read_text(base / args.system)

    # 1) load & normalize envelope
    envelope_raw = read_json(Path(args.envelope))
    envelope_obj = normalize_envelope(envelope_raw)

    # 2) build messages
    messages = build_messages(system_txt, args.query, envelope_obj)

    # 3) call model & extract SQL
    raw = call_ollama_chat(args.host, args.model, messages, args.num_ctx, args.temp, args.debug)
    if not raw:
        return

    sql = extract_sql(raw).strip()
    # Print ONLY the SQL (no extra text)
    print(sql)

if __name__ == "__main__":
    main()
