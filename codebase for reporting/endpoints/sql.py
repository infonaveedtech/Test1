# endpoints/sql.py
from typing import List, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db.oracle_exec import run_sql

router = APIRouter(
    prefix="",
    tags=["sql"],
)


class SqlExecuteRequest(BaseModel):
    sql: str = Field(..., description="Oracle SQL query to execute (SELECT only).")
    row_limit: int = Field(
        200,
        ge=1,
        le=5000,
        description="Maximum number of rows to fetch.",
    )


class SqlExecuteResponse(BaseModel):
    columns: List[str]
    rows: List[List[Any]]
    row_limit: int
    exec_ms: int


@router.post("/sql/execute", response_model=SqlExecuteResponse)
def sql_execute_endpoint(body: SqlExecuteRequest) -> SqlExecuteResponse:
    """
    Execute a SQL query on the Oracle DB using the existing run_sql helper.

    NOTE:
        - This is intended for SELECT-style queries, not DDL/DML.
        - Row limit is enforced on the DB side (via run_sql).
    """
    # Basic guard against obviously dangerous operations
    dangerous_keywords = ["DROP ", "TRUNCATE ", "DELETE ", "UPDATE ", "INSERT "]
    upper_sql = body.sql.upper()
    if any(kw in upper_sql for kw in dangerous_keywords):
        raise HTTPException(
            status_code=400,
            detail="Potentially destructive SQL detected. Only SELECT-style queries are allowed.",
        )

    try:
        import time

        t0 = time.perf_counter()
        result = run_sql(body.sql, row_limit=body.row_limit)
        exec_ms = int((time.perf_counter() - t0) * 1000)

    except Exception as e:
        # You can special-case oracledb.Error here if you want finer control
        raise HTTPException(
            status_code=500,
            detail=f"Oracle execution error: {e}",
        ) from e

    # run_sql already returns columns/list-of-rows. We just attach exec_ms.
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    row_limit = result.get("row_limit", body.row_limit)

    # FastAPI will JSON-encode tuples as lists, and handle datetimes/decimals.
    return SqlExecuteResponse(
        columns=columns,
        rows=rows,
        row_limit=row_limit,
        exec_ms=exec_ms,
    )
