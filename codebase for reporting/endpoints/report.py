# endpoints/report.py

from typing import Any, Dict, List, Optional

import base64
from fastapi import APIRouter
from pydantic import BaseModel, Field

from reports.report_utils import (
    build_report_context,
    build_html_report,
    build_csv_bytes,
    build_docx_bytes,
    apply_column_customizations,
    apply_layout_grouping,
)
from reports.pdf_utils import build_report_pdf


router = APIRouter(
    prefix="/report",
    tags=["report"],
)


# =========================
# Pydantic models
# =========================

class IncludeFlags(BaseModel):
    """
    Which formats the backend should generate.
    """
    html: bool = True
    csv: bool = True
    pdf: bool = False
    docx: bool = False


class ComputedColumnSpec(BaseModel):
    """
    Computed column definition:
    - name: raw column name (no spaces, like TURNOVER_K)
    - expression: arithmetic using existing raw columns (e.g. TURNOVER_VALUE / 1000)
    - label: optional display header for this column
    """
    name: str
    expression: str
    label: Optional[str] = None


class ColumnRules(BaseModel):
    """
    Column customisation contract, mirroring what app.py and frontend.py use:
    - include: which raw columns to keep/drop
    - rename: mapping raw column -> display header
    - computed: list of computed column specs
    """
    include: Dict[str, bool] = Field(default_factory=dict)
    rename: Dict[str, str] = Field(default_factory=dict)
    computed: List[ComputedColumnSpec] = Field(default_factory=list)


class ReportLayoutOptions(BaseModel):
    """
    Layout / grouping options for the report.

    - layout_type: "flat" (default) or "grouped"
    - group_by: list of column names to group by (e.g. ["BROKER_NAME"])
    - detail_columns: which columns to show in each group table
      (defaults to all columns if empty)
    """
    layout_type: str = "flat"               # "flat" or "grouped"
    group_by: List[str] = Field(default_factory=list)
    detail_columns: List[str] = Field(default_factory=list)


class ReportRequest(BaseModel):
    """
    Request body for /v1/report

    Note:
    - 'sql' is the pipeline/DB SQL.
    - 'sql_override' (optional) lets the frontend change what SQL is SHOWN in the report
      without re-running the DB. If provided, this is what goes into the report context.
    """
    title: str = "NL→SQL Report"
    question: str

    sql: str
    sql_override: Optional[str] = None

    columns: List[str]
    rows: List[List[Any]]

    model: Optional[str] = None
    gen_ms: Optional[int] = None
    exec_ms: Optional[int] = None
    rows_est: Optional[str] = None
    generated_by: Optional[str] = "ATS SQLBot"

    # include: IncludeFlags = IncludeFlags()
    # column_rules: Optional[ColumnRules] = None
    include: IncludeFlags = IncludeFlags()
    column_rules: Optional[ColumnRules] = None
    layout: Optional[ReportLayoutOptions] = None



class ReportResponse(BaseModel):
    """
    Response from /v1/report.

    - html/csv_base64/pdf_base64/docx_base64: generated formats (optional, depending on IncludeFlags)
    - final_columns / final_display_columns / final_rows:
        the fully customised table (after include/rename/computed),
        so the frontend can show a preview that matches the exports.
    """
    html: Optional[str] = None
    csv_base64: Optional[str] = None
    pdf_base64: Optional[str] = None
    docx_base64: Optional[str] = None

    final_columns: List[str] = Field(default_factory=list)
    final_display_columns: List[str] = Field(default_factory=list)
    final_rows: List[List[Any]] = Field(default_factory=list)


# =========================
# Helpers
# =========================

def _encode_b64(data: Optional[bytes]) -> Optional[str]:
    if not data:
        return None
    return base64.b64encode(data).decode("utf-8")


# =========================
# Endpoint
# =========================

@router.post("", response_model=ReportResponse)
def create_report(req: ReportRequest) -> ReportResponse:
    """
    Build a report from DB results + column rules.

    Steps:
    1) Build a base 'context' using build_report_context.
    2) Apply column customisations (include/rename/computed).
    3) Generate HTML / CSV / PDF / DOCX depending on 'include' flags.
    4) Return both the rendered outputs and the final table (columns + rows).
    """

    # 1) Decide which SQL to show in the report
    effective_sql = (req.sql_override or "").strip() or req.sql

    # 2) Build base report context (handles column normalisation)
    context = build_report_context(
        title=req.title,
        question=req.question,
        sql=effective_sql,
        columns=req.columns,
        rows=req.rows,
        model=req.model,
        gen_ms=req.gen_ms,
        exec_ms=req.exec_ms,
        rows_est=req.rows_est,
        generated_by=req.generated_by,
    )

    
        # 3) Apply column customisations, if provided
    if req.column_rules:
        rules: Dict[str, Any] = {
            "include": req.column_rules.include or {},
            "rename": req.column_rules.rename or {},
            # convert ComputedColumnSpec objects to plain dicts for report_utils
            "computed": [c.model_dump() for c in req.column_rules.computed],
        }
        context = apply_column_customizations(context, rules)

    # 3b) Apply layout / grouping options (if any)
    layout_dict: Dict[str, Any] = {}
    if req.layout:
        # Convert Pydantic model to plain dict
        layout_dict = req.layout.model_dump()

    context = apply_layout_grouping(context, layout_dict)


    # Pull out the final table state after customisation
    final_columns: List[str] = context.get("columns") or []
    final_display: List[str] = context.get("display_columns") or final_columns
    final_rows: List[List[Any]] = context.get("rows") or []

    # 4) Generate formats based on IncludeFlags
    html_str: Optional[str] = None
    csv_b64: Optional[str] = None
    pdf_b64: Optional[str] = None
    docx_b64: Optional[str] = None

    # HTML
    if req.include.html:
        html_str = build_html_report(context)

    # CSV
    if req.include.csv:
        csv_bytes = build_csv_bytes(final_columns, final_rows)
        csv_b64 = _encode_b64(csv_bytes)

    # DOCX
    if req.include.docx:
        docx_bytes = build_docx_bytes(context)
        docx_b64 = _encode_b64(docx_bytes)

    # PDF
    if req.include.pdf:
        pdf_bytes = build_report_pdf(context)
        pdf_b64 = _encode_b64(pdf_bytes)

    # 5) Return unified response
    return ReportResponse(
        html=html_str,
        csv_base64=csv_b64,
        pdf_base64=pdf_b64,
        docx_base64=docx_b64,
        final_columns=final_columns,
        final_display_columns=final_display,
        final_rows=final_rows,
    )
