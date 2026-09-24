"""Entity-level detail + assessment Excel report.

`GET /report/detail` returns the row-level readiness detail (catalog → schema →
entity → column → relationship → metric view → Genie Agent → non-certified),
scope-aware like `/assess`. `POST /report/excel` streams the same detail as a
multi-tab .xlsx. Both are read-only.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.assessment.detail import run_detail
from server.config import set_user_token
from server.workspace_filter import set_workspace_filter, set_catalog_scope
from server.excel import build_assessment_workbook

logger = logging.getLogger(__name__)
router = APIRouter()

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ReportRequest(BaseModel):
    workspace_filter: Optional[dict] = None
    catalogs: list[str] = []
    scorecard: Optional[dict] = None  # inline scorecard → Summary sheet overall row


def _apply_scope(token: Optional[str], wsf: Optional[dict], catalogs: Optional[list]) -> None:
    set_user_token(token)
    set_workspace_filter(wsf)
    set_catalog_scope(catalogs or None)


@router.get("/report/detail")
async def report_detail(
    x_forwarded_access_token: Optional[str] = Header(default=None),
    workspace_ids: Optional[str] = None,
    workspace_mode: str = "include",
    catalogs: Optional[str] = None,
):
    """Row-level readiness detail (JSON). Optional ?workspace_ids=&catalogs= scope it."""
    ids = [w.strip() for w in (workspace_ids or "").split(",") if w.strip()]
    cat = [c.strip() for c in (catalogs or "").split(",") if c.strip()]
    _apply_scope(x_forwarded_access_token,
                 {"mode": workspace_mode, "workspace_ids": ids} if ids else None,
                 cat)
    return await run_detail()


@router.post("/report/excel")
async def report_excel(
    req: ReportRequest = Body(default=ReportRequest()),
    x_forwarded_access_token: Optional[str] = Header(default=None),
):
    """Download the entity-level assessment as a multi-tab Excel workbook."""
    _apply_scope(x_forwarded_access_token, req.workspace_filter, req.catalogs or None)
    detail = await run_detail()
    buf = build_assessment_workbook(detail, req.scorecard)
    day = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    fname = f"genie-ontology-readiness-detail-{day}.xlsx"
    return StreamingResponse(
        buf, media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
