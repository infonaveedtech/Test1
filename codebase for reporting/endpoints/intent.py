# endpoints/intent.py
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from agents.agent0_intent_llm import (
    run_agent0_once,
    DEFAULT_MODEL as A0_DEFAULT_MODEL,
)
from agents.agent1_planner_llm import DEFAULT_HOST as DEFAULT_LLM_HOST
from auth import get_current_user, CurrentUser
router = APIRouter(
    prefix="",
    tags=["intent"],
)


class IntentRequest(BaseModel):
    query: str = Field(..., description="User's raw natural language query.")
    host: Optional[str] = Field(
        None,
        description="Override LLM host, e.g. 'http://localhost:11434'. If omitted, uses project default.",
    )
    model: Optional[str] = Field(
        None,
        description="Override LLM model name for Agent-0. If omitted, uses Agent-0 default.",
    )
    debug: bool = Field(
        False,
        description="If true, Agent-0 may log extra debug info to stdout.",
    )


@router.post("/intent")
def intent_endpoint(body: IntentRequest, current_user: CurrentUser = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Run Agent-0 (intent & safety gate) on a given query.

    This does NOT run RAG, Agent-1/2/3, or the DB.
    It just classifies the query and decides whether the pipeline should run.
    """
    # 1) Resolve host & model with sensible defaults
    host = body.host or DEFAULT_LLM_HOST
    model = body.model or A0_DEFAULT_MODEL

    # If user accidentally passes "localhost:11434" or "string",
    # normalise to a valid URL like "http://localhost:11434"
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host

    try:
        intent_obj = run_agent0_once(
            host=host,
            model=model,
            user_query=body.query,
            debug=body.debug,
        )
    except Exception as e:
        # Wrap internal errors in an HTTPException so FastAPI returns 500 JSON nicely
        raise HTTPException(status_code=500, detail=f"Agent-0 error: {e}") from e

    # Optionally, you could hide the raw LLM text if you don't want to expose it:
    # intent_obj.pop("__raw__", None)

    return intent_obj
