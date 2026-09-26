"""Multi-tab .xlsx builders for the entity-level assessment report and the
LLM-generation workbook.

Two public builders:
  * ``build_assessment_workbook(detail, scorecard)`` — the read-only report: which
    catalogs / schemas / entities / columns / relationships / metric views / Genie
    Agents / non-certified assets are failing the readiness checks.
  * ``build_generation_workbook(payload)`` — the LLM-drafted fixes, laid out in the
    exact tabs/columns the spec defines (Catalog, Schema, Entity, two Relationship
    sheets, GenieAgent, MetricViews).

Both return a ``BytesIO`` positioned at 0, ready to stream. openpyxl only; no
template files.
"""

import io
import logging

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

_HEADER_FILL = PatternFill("solid", fgColor="1B3139")   # Databricks navy
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_FAIL_FONT = Font(color="B23A48", bold=True)
_PASS_FONT = Font(color="1C7A52")
_WRAP = Alignment(vertical="top", wrap_text=True)
_TOP = Alignment(vertical="top")
_THIN = Side(style="thin", color="D9DEE6")
_BORDER = Border(bottom=_THIN)


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list],
           widths: list[int] | None = None, wrap_last: bool = False):
    """Add one styled sheet. ``title`` is truncated to Excel's 31-char limit and
    de-duplicated. FAIL/PASS values in a 'Status' column are colored."""
    name = title[:31]
    n = 2
    while name in wb.sheetnames:
        name = f"{title[:28]}_{n}"; n += 1
    ws = wb.create_sheet(name)

    status_idx = headers.index("Status") if "Status" in headers else -1
    ws.append(headers)
    for i, cell in enumerate(ws[1], start=1):
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _TOP
    for r in rows:
        ws.append(["" if v is None else v for v in r])
        row_cells = ws[ws.max_row]
        for c in row_cells:
            c.alignment = _WRAP if wrap_last else _TOP
            c.border = _BORDER
        if status_idx >= 0:
            sc = row_cells[status_idx]
            if sc.value == "FAIL":
                sc.font = _FAIL_FONT
            elif sc.value == "PASS":
                sc.font = _PASS_FONT

    widths = widths or [max(12, min(60, len(h) + 4)) for h in headers]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def _yn(v) -> str:
    return "Yes" if v else "No"


def _finalize(wb: Workbook) -> io.BytesIO:
    # openpyxl always creates a default 'Sheet'; drop it if we added our own.
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
        del wb["Sheet"]
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Assessment report
# ---------------------------------------------------------------------------
def build_assessment_workbook(detail: dict, scorecard: dict | None = None) -> io.BytesIO:
    wb = Workbook()
    detail = detail or {}
    summary = detail.get("summary", {}) or {}

    # Summary — overall (if a scorecard was supplied) + per-section fail counts.
    srows = []
    if scorecard:
        overall = scorecard.get("overall", {}) or {}
        if overall.get("score") is not None:
            srows.append(["Overall readiness", f"{overall.get('score')}/100",
                          overall.get("readiness_stage") or ""])
    label = {"catalogs": "Catalogs", "schemas": "Schemas", "entities": "Entities",
             "columns_uncommented": "Uncommented columns", "relationships": "Entities missing a PK",
             "metric_views": "Metric views", "genie_agents": "Genie Agents", "noncertified": "Non-certified assets"}
    for key, lab in label.items():
        sec = summary.get(key, {})
        if sec:
            srows.append([lab, f"{sec.get('failing', 0)} failing", f"of {sec.get('total', 0)} total"])
    _sheet(wb, "Summary", ["Check", "Result", "Detail"], srows, widths=[30, 22, 22])

    _sheet(wb, "Catalogs",
           ["Pillar", "Catalog", "Description", "Tags", "Status"],
           [[r.get("pillar"), r.get("catalog"), _yn(r.get("description_present")),
             _yn(r.get("tags_present")), r.get("status")] for r in detail.get("catalogs", [])],
           widths=[20, 30, 14, 10, 10])

    _sheet(wb, "Schemas",
           ["Pillar", "Catalog", "Schema", "Description", "Tags", "Status"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), _yn(r.get("description_present")),
             _yn(r.get("tags_present")), r.get("status")] for r in detail.get("schemas", [])],
           widths=[20, 26, 26, 14, 10, 10])

    _sheet(wb, "Entities",
           ["Pillar", "Catalog", "Schema", "Entity", "Type", "Description", "Tags",
            "Columns", "Commented", "Column %", "Status"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), r.get("entity"), r.get("table_type"),
             _yn(r.get("description_present")), _yn(r.get("tags_present")), r.get("columns"),
             r.get("commented_columns"), r.get("column_comment_pct"), r.get("status")]
            for r in detail.get("entities", [])],
           widths=[18, 22, 22, 26, 12, 12, 8, 9, 10, 9, 9])

    _sheet(wb, "Columns (uncommented)",
           ["Pillar", "Catalog", "Schema", "Entity", "Column"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), r.get("entity"), r.get("column")]
            for r in detail.get("columns_failing", [])],
           widths=[18, 22, 22, 26, 26])

    _sheet(wb, "Relationships",
           ["Pillar", "Catalog", "Schema", "Entity", "Has PK", "Has FK", "PK Columns", "Status"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), r.get("entity"),
             _yn(r.get("has_pk")), _yn(r.get("has_fk")), r.get("pk_columns"), r.get("status")]
            for r in detail.get("relationships", [])],
           widths=[18, 22, 22, 26, 8, 8, 24, 10])

    _sheet(wb, "MetricViews",
           ["Pillar", "Catalog", "Schema", "Metric View", "Described", "Status"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), r.get("entity"),
             _yn(r.get("description_present")), r.get("status")]
            for r in detail.get("metric_views", [])],
           widths=[18, 24, 24, 28, 12, 10])

    ga = detail.get("genie_agents", {}) or {}
    _sheet(wb, "GenieAgents",
           ["Genie Agent", "Space ID", "Curation readable", "Instructions", "Example SQL",
            "Benchmarks", "Sample Qs", "Functions", "Tables", "Missing", "Status"],
           [[a.get("name"), a.get("space_id"), _yn(a.get("curation_readable")),
             a.get("instructions", ""), a.get("example_sqls", ""), a.get("benchmarks", ""),
             a.get("sample_questions", ""), a.get("functions", ""), a.get("tables", ""),
             ", ".join(a.get("missing", [])), a.get("status", "")]
            for a in ga.get("agents", [])],
           widths=[26, 22, 15, 12, 12, 11, 10, 10, 8, 30, 10], wrap_last=True)

    _sheet(wb, "NonCertified",
           ["Pillar", "Catalog", "Schema", "Entity", "Certified", "Status"],
           [[r.get("pillar"), r.get("catalog"), r.get("schema"), r.get("entity"),
             _yn(r.get("certified")), r.get("status")] for r in detail.get("noncertified", [])],
           widths=[18, 24, 24, 28, 11, 10])

    pages = detail.get("pages", {}) or {}
    _sheet(wb, "Domains & Pages", ["Item", "Assessable", "Note"],
           [["Genie Ontology Pages", _yn(pages.get("assessable")), pages.get("note", "")]],
           widths=[24, 12, 80], wrap_last=True)

    return _finalize(wb)


# ---------------------------------------------------------------------------
# Generation workbook — exact tabs/columns per spec
# ---------------------------------------------------------------------------
def build_generation_workbook(payload: dict) -> io.BytesIO:
    wb = Workbook()
    p = payload or {}

    _sheet(wb, "Catalog",
           ["Catalog", "Catalog_Description_Generated", "Catalog_Tag_Generated"],
           [[r.get("catalog"), r.get("description"), r.get("tag")]
            for r in p.get("catalog", [])],
           widths=[28, 60, 40], wrap_last=True)

    _sheet(wb, "Schema",
           ["Catalog", "Schema", "Schema_Description_Generated", "Schema_Tag_Generated"],
           [[r.get("catalog"), r.get("schema"), r.get("description"), r.get("tag")]
            for r in p.get("schema", [])],
           widths=[24, 24, 60, 55], wrap_last=True)

    _sheet(wb, "Entity",
           ["Catalog", "Schema", "Entity",
            "Entity_Description_Generated", "Entity_Tag_Generated"],
           [[r.get("catalog"), r.get("schema"), r.get("entity"),
             r.get("entity_description"), r.get("entity_tag")]
            for r in p.get("entity", [])],
           widths=[20, 20, 24, 60, 65], wrap_last=True)

    _sheet(wb, "Entity_Columns",
           ["Catalog", "Schema", "Entity", "Column", "Column_Comments_Generated"],
           [[r.get("catalog"), r.get("schema"), r.get("entity"), r.get("column"), r.get("column_comment")]
            for r in p.get("entity_columns", [])],
           widths=[20, 20, 24, 22, 60], wrap_last=True)

    _sheet(wb, "Relationship_PrimaryKey",
           ["Catalog", "Schema", "Parent_Entity", "Column_Name", "Constraint_Type", "Statement"],
           [[r.get("catalog"), r.get("schema"), r.get("parent_entity"), r.get("column_name"),
             "PrimaryKey", r.get("statement")] for r in p.get("relationship_pk", [])],
           widths=[20, 20, 26, 22, 14, 80], wrap_last=True)

    _sheet(wb, "Relationship_ForeignKey",
           ["Catalog", "Schema", "Parent_Entity", "Column_Name", "Foreign_Key",
            "Primary_Key_Column", "Foreign_key_Column", "Constraint_Type", "Statement"],
           [[r.get("catalog"), r.get("schema"), r.get("parent_entity"), r.get("column_name"),
             r.get("foreign_key"), r.get("primary_key_column"), r.get("foreign_key_column"),
             "ForeignKey", r.get("statement")] for r in p.get("relationship_fk", [])],
           widths=[18, 18, 24, 20, 24, 22, 22, 14, 80], wrap_last=True)

    _sheet(wb, "GenieAgent",
           ["GenieAgent_Name", "GenieAgentID", "Generated_Instructions"],
           [[r.get("name"), r.get("space_id"), r.get("instructions")]
            for r in p.get("genie_agent", [])],
           widths=[30, 24, 90], wrap_last=True)

    _sheet(wb, "MetricViews",
           ["Catalog", "Schema", "Text"],
           [[r.get("catalog"), r.get("schema"), r.get("text")] for r in p.get("metric_views", [])],
           widths=[24, 24, 100], wrap_last=True)

    return _finalize(wb)
