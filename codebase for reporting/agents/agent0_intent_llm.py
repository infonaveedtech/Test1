#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent 0 — Intent & Safety Gate

- Very small classifier in front of the NL→SQL pipeline.
- Decides if we should run the heavy RAG + Agent-1/2/3 + Oracle,
  or just answer directly (small talk, assistant questions, etc.).
"""
import os
import json
from pathlib import Path
from typing import Any, Dict, List

from agents.agent2_composer_llm import call_ollama_chat  # reuse same HTTP client

# Defaults (can be overridden from Streamlit sidebar)
# NOTE:
# - These now read from env (LLM_HOST, LLM_MODEL) if set.
# - If env vars are missing, they fall back to the original values.
DEFAULT_HOST = os.getenv("LLM_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("LLM_MODEL", "qwen3-coder:30b")
DEFAULT_NUM_CTX = 2048
DEFAULT_TEMPERATURE = 0.0  # we want deterministic, stable classification

DEFAULT_SYSTEM_PATH = Path("prompts/system_agent0_intent.md")


def _safe_read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return default


def build_messages(system_txt: str, user_query: str) -> List[Dict[str, str]]:
    """
    Keep it minimal: system prompt defines the JSON contract.
    """
    return [
        {"role": "system", "content": system_txt},
        {
            "role": "user",
            "content": (
                "User message:\n"
                + user_query.strip()
                + "\n\n"
                "Decide whether this should go through the NL→SQL pipeline. "
                "Return ONLY the JSON object as specified."
            ),
        },
    ]


def _fallback_intent(user_query: str) -> Dict[str, Any]:
    """
    Very safe default if LLM fails: allow pipeline but mark intent as generic sql_query.
    """
    return {
        "should_run_pipeline": True,
        "intent": "sql_query",
        "reason": "Fallback intent because parsing failed.",
        "assistant_reply": "Got it — I will try to build a SQL query for this.",
        "rephrased_query": user_query.strip(),
        "safety": {
            "is_potentially_unsafe": False,
            "notes": "No explicit safety signals detected in fallback.",
        },
    }


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    """
    Try to locate and parse the first {...} JSON object in the LLM output.
    Similar to what Agent-1 does, but leaner.
    """
    if not text:
        return _fallback_intent("")

    # Quick path: try parse as-is
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Scan for first '{' and attempt to parse progressively
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # If everything fails, fallback
    return _fallback_intent("")


def run_agent0_once(
    host: str,
    model: str,
    user_query: str,
    debug: bool = False,
) -> Dict[str, Any]:
    """
    Main entrypoint for Agent-0.

    - Reads system prompt from prompts/system_agent0_intent.md
    - Calls the local LLM
    - Parses JSON object
    - Ensures required keys exist
    """
    system_txt = _safe_read(
        DEFAULT_SYSTEM_PATH,
        default=(
            "You are Agent-0, an intent gate for an NL→SQL pipeline. "
            "Return ONLY a JSON object with keys: should_run_pipeline, intent, "
            "reason, assistant_reply, rephrased_query, safety."
        ),
    )

    messages = build_messages(system_txt, user_query)

    raw = call_ollama_chat(
        host=host,
        model=model,
        messages=messages,
        num_ctx=DEFAULT_NUM_CTX,
        temperature=DEFAULT_TEMPERATURE,
        debug=debug,
    )

    intent_obj = _extract_first_json_object(raw)

    # Normalize / ensure keys
    intent_obj.setdefault("should_run_pipeline", True)
    intent_obj.setdefault("intent", "sql_query")
    intent_obj.setdefault("reason", "No reason provided by model.")
    intent_obj.setdefault(
        "assistant_reply",
        "I will try to route this through the database pipeline.",
    )
    intent_obj.setdefault("rephrased_query", user_query.strip())
    safety = intent_obj.get("safety") or {}
    safety.setdefault("is_potentially_unsafe", False)
    safety.setdefault("notes", "")
    intent_obj["safety"] = safety

    # Optionally attach raw for debugging (but not used in downstream logic)
    intent_obj["__raw__"] = raw

    return intent_obj


# Small CLI for manual testing, optional
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="User query to classify")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    out = run_agent0_once(
        host=args.host,
        model=args.model,
        user_query=args.query,
        debug=True,
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
