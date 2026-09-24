"""Entity-level readiness detail — the row-by-row backing for the Excel report
and the LLM-generation tab.

Where ``probes.py`` scores each pillar and rolls up to a *per-schema* drill-down,
this module goes one level deeper: it returns a row per **catalog**, **schema**,
**entity (table)**, **failing column**, **relationship (PK/FK)**, **metric view**,
**Genie Agent**, and **non-certified asset** — each carrying a pass/fail flag and,
where applicable, the **Pillar** the asset belongs to (a catalog-level governed
tag, per the product decision).

Everything is read-only and reuses the probe machinery: ``_resolve_sources`` /
``_src`` / ``_internal_catalog_filter`` for the metastore-vs-per-catalog source,
``execute_sql`` for OBO+SP reads, and ``_inspect_space`` for Genie curation. It is
scope-aware through the same contextvars the probes read (workspace/catalog scope),
so "specific workspace / catalog / all catalogs" works unchanged.
"""

import asyncio
import logging

from server.security import quote_ident, quote_literal, safe_error
from server.sql_client import execute_sql
from server.assessment.probes import (
    _resolve_sources,
    _src,
    _internal_catalog_filter,
    _inspect_space,
    _genie_audit_rows,
    _MAX_INSPECT,
    _EDIT_HINT,
)
from server.config import get_workspace_host, get_auth_headers

logger = logging.getLogger(__name__)

# Catalog-level governed tag(s) that define the business "Pillar". First match wins,
# preferring an explicit `pillar` tag, then the domain-style tags.
_PILLAR_TAG_KEYS = ("pillar", "data_domain", "business_domain", "data_product")

# Row caps so an "all catalogs" run on a wide metastore can't return an unbounded
# payload. The UI recommends scoping generation to a catalog/schema.
_ENTITY_CAP = 8000
_COLUMN_CAP = 30000


def _commented(expr: str = "comment") -> str:
    return f"({expr} IS NOT NULL AND trim({expr}) <> '')"


async def _pillar_map(s: dict) -> dict:
    """catalog_name -> Pillar value, from the catalog governed tags. Empty when the
    catalog_tags view isn't readable (Pillar then renders blank)."""
    ct = _src("catalog_tags", s)
    if ct is None:
        return {}
    keys = ", ".join(quote_literal(k) for k in _PILLAR_TAG_KEYS)
    try:
        rows = await execute_sql(
            f"SELECT catalog_name AS cat, lower(tag_name) AS k, tag_value AS v "
            f"FROM {ct} WHERE lower(tag_name) IN ({keys})"
        )
    except Exception as e:
        logger.info(f"pillar tag read failed: {str(e)[:80]}")
        return {}
    # Prefer the earliest key in _PILLAR_TAG_KEYS when a catalog carries several.
    rank = {k: i for i, k in enumerate(_PILLAR_TAG_KEYS)}
    best: dict[str, tuple[int, str]] = {}
    for r in rows:
        cat, k, v = r.get("cat"), r.get("k"), r.get("v")
        if not cat or not v:
            continue
        pr = rank.get(k, 99)
        if cat not in best or pr < best[cat][0]:
            best[cat] = (pr, v)
    return {c: v for c, (_, v) in best.items()}


async def catalog_detail(s: dict, pillars: dict) -> list[dict]:
    """One row per catalog: description present, ≥1 tag present, Pillar."""
    cat_src = _src("catalogs", s)
    ct = _src("catalog_tags", s)
    if cat_src is None:
        return []
    rows = await execute_sql(
        f"SELECT catalog_name AS catalog, {_commented()} AS has_desc FROM {cat_src}"
    )
    tagged: set[str] = set()
    if ct is not None:
        try:
            trows = await execute_sql(f"SELECT DISTINCT catalog_name AS c FROM {ct}")
            tagged = {r.get("c") for r in trows if r.get("c")}
        except Exception:
            tagged = set()
    out = []
    for r in rows:
        cat = r.get("catalog")
        if not cat:
            continue
        desc = bool(r.get("has_desc"))
        has_tag = cat in tagged
        out.append({
            "pillar": pillars.get(cat, ""),
            "catalog": cat,
            "description_present": desc,
            "tags_present": has_tag,
            "status": "PASS" if (desc and has_tag) else "FAIL",
        })
    return sorted(out, key=lambda x: (x["status"] != "FAIL", x["catalog"]))


async def schema_detail(s: dict, pillars: dict) -> list[dict]:
    """One row per schema: description + ≥1 schema tag present."""
    sch_src = _src("schemata", s)
    st = _src("schema_tags", s)
    if sch_src is None:
        return []
    rows = await execute_sql(
        f"SELECT catalog_name AS catalog, schema_name AS schema, {_commented()} AS has_desc "
        f"FROM {sch_src} WHERE schema_name <> 'information_schema'"
    )
    tagged: set[tuple] = set()
    if st is not None:
        try:
            trows = await execute_sql(f"SELECT DISTINCT catalog_name AS c, schema_name AS s FROM {st}")
            tagged = {(r.get("c"), r.get("s")) for r in trows}
        except Exception:
            tagged = set()
    out = []
    for r in rows:
        cat, sch = r.get("catalog"), r.get("schema")
        if not cat or not sch:
            continue
        desc = bool(r.get("has_desc"))
        has_tag = (cat, sch) in tagged
        out.append({
            "pillar": pillars.get(cat, ""),
            "catalog": cat, "schema": sch,
            "description_present": desc, "tags_present": has_tag,
            "status": "PASS" if (desc and has_tag) else "FAIL",
        })
    return sorted(out, key=lambda x: (x["status"] != "FAIL", x["catalog"], x["schema"]))


async def entity_detail(s: dict, pillars: dict) -> tuple[list[dict], list[dict]]:
    """(entities, failing_columns). Per entity: description + tag present, and
    column-comment coverage. Failing columns: the individual columns with no comment."""
    tbl = _src("tables", s)
    col = _src("columns", s)
    tt = _src("table_tags", s)
    if tbl is None or col is None:
        return [], []
    # Per-entity table comment + column coverage in two grouped scans, joined in Python.
    trows = await execute_sql(
        f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, "
        f"       table_type AS tt, {_commented()} AS has_desc "
        f"FROM {tbl} WHERE table_schema <> 'information_schema' {_internal_catalog_filter(s)} "
        f"LIMIT {_ENTITY_CAP}"
    )
    crows = await execute_sql(
        f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, "
        f"       COUNT(*) AS cols, SUM(CASE WHEN {_commented()} THEN 1 ELSE 0 END) AS commented "
        f"FROM {col} WHERE table_schema <> 'information_schema' {_internal_catalog_filter(s)} "
        f"GROUP BY table_catalog, table_schema, table_name"
    )
    cov = {(r.get("c"), r.get("s"), r.get("t")): (int(r.get("cols") or 0), int(r.get("commented") or 0)) for r in crows}
    tagged: set[tuple] = set()
    if tt is not None:
        try:
            grows = await execute_sql(
                f"SELECT DISTINCT catalog_name AS c, schema_name AS s, table_name AS t FROM {tt}"
            )
            tagged = {(r.get("c"), r.get("s"), r.get("t")) for r in grows}
        except Exception:
            tagged = set()

    entities = []
    for r in trows:
        cat, sch, tname = r.get("c"), r.get("s"), r.get("t")
        if not cat or not sch or not tname:
            continue
        key = (cat, sch, tname)
        cols, commented = cov.get(key, (0, 0))
        desc = bool(r.get("has_desc"))
        has_tag = key in tagged
        col_pct = round(100.0 * commented / cols, 1) if cols else 0.0
        ok = desc and has_tag and cols and commented == cols
        entities.append({
            "pillar": pillars.get(cat, ""),
            "catalog": cat, "schema": sch, "entity": tname,
            "table_type": r.get("tt") or "",
            "description_present": desc, "tags_present": has_tag,
            "columns": cols, "commented_columns": commented, "column_comment_pct": col_pct,
            "status": "PASS" if ok else "FAIL",
        })
    entities.sort(key=lambda x: (x["status"] != "FAIL", x["catalog"], x["schema"], x["entity"]))

    # Failing columns: individual uncommented columns (the actionable list).
    frows = await execute_sql(
        f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, column_name AS col "
        f"FROM {col} WHERE table_schema <> 'information_schema' AND NOT {_commented()} "
        f"{_internal_catalog_filter(s)} LIMIT {_COLUMN_CAP}"
    )
    failing_cols = [
        {"pillar": pillars.get(r.get("c"), ""), "catalog": r.get("c"), "schema": r.get("s"),
         "entity": r.get("t"), "column": r.get("col"), "status": "FAIL"}
        for r in frows if r.get("c") and r.get("t")
    ]
    return entities, failing_cols


async def relationship_detail(s: dict, pillars: dict) -> list[dict]:
    """Per entity: whether a PK / FK constraint is declared, plus PK columns (used
    by the assessment and to seed generation for entities missing a PK)."""
    tbl = _src("tables", s)
    tc = _src("table_constraints", s)
    kcu = _src("key_column_usage", s)
    if tbl is None:
        return []
    # Base: every base table in scope (metric/other types excluded from PK/FK checks).
    base = await execute_sql(
        f"SELECT table_catalog AS c, table_schema AS s, table_name AS t "
        f"FROM {tbl} WHERE table_schema <> 'information_schema' "
        f"AND table_type IN ('MANAGED','EXTERNAL','MANAGED_SHALLOW_CLONE','VIEW') "
        f"{_internal_catalog_filter(s)} LIMIT {_ENTITY_CAP}"
    )
    by_type: dict[tuple, dict] = {}
    pk_cols: dict[tuple, list] = {}
    if tc is not None:
        try:
            crows = await execute_sql(
                f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, "
                f"constraint_type AS ct, COUNT(*) AS n "
                f"FROM {tc} GROUP BY table_catalog, table_schema, table_name, constraint_type"
            )
            for r in crows:
                k = (r.get("c"), r.get("s"), r.get("t"))
                d = by_type.setdefault(k, {"pk": 0, "fk": 0})
                if r.get("ct") == "PRIMARY KEY":
                    d["pk"] = int(r.get("n") or 0)
                elif r.get("ct") == "FOREIGN KEY":
                    d["fk"] = int(r.get("n") or 0)
        except Exception as e:
            logger.info(f"table_constraints read failed: {str(e)[:80]}")
        if kcu is not None and tc is not None:
            try:
                krows = await execute_sql(
                    f"SELECT k.table_catalog AS c, k.table_schema AS s, k.table_name AS t, "
                    f"k.column_name AS col FROM {kcu} k JOIN {tc} tc2 "
                    f"ON k.constraint_name = tc2.constraint_name AND k.table_name = tc2.table_name "
                    f"WHERE tc2.constraint_type = 'PRIMARY KEY'"
                )
                for r in krows:
                    pk_cols.setdefault((r.get("c"), r.get("s"), r.get("t")), []).append(r.get("col"))
            except Exception as e:
                logger.info(f"key_column_usage read failed: {str(e)[:80]}")

    out = []
    for r in base:
        cat, sch, tname = r.get("c"), r.get("s"), r.get("t")
        if not cat or not tname:
            continue
        k = (cat, sch, tname)
        d = by_type.get(k, {"pk": 0, "fk": 0})
        has_pk = d["pk"] > 0
        out.append({
            "pillar": pillars.get(cat, ""),
            "catalog": cat, "schema": sch, "entity": tname,
            "has_pk": has_pk, "has_fk": d["fk"] > 0,
            "pk_columns": ", ".join(c for c in pk_cols.get(k, []) if c),
            "status": "PASS" if has_pk else "FAIL",
        })
    return sorted(out, key=lambda x: (x["status"] != "FAIL", x["catalog"], x["schema"], x["entity"]))


async def metric_view_detail(s: dict, pillars: dict) -> list[dict]:
    """Per metric view: comment present. (Metric views are the GA semantic layer
    feeding Genie Ontology.)"""
    tbl = _src("tables", s)
    if tbl is None:
        return []
    for tv in ("METRIC_VIEW", "METRIC VIEW"):
        try:
            rows = await execute_sql(
                f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, {_commented()} AS has_desc "
                f"FROM {tbl} WHERE table_type = {quote_literal(tv)} {_internal_catalog_filter(s)}"
            )
            return [
                {"pillar": pillars.get(r.get("c"), ""), "catalog": r.get("c"), "schema": r.get("s"),
                 "entity": r.get("t"), "description_present": bool(r.get("has_desc")),
                 "status": "PASS" if bool(r.get("has_desc")) else "FAIL"}
                for r in rows if r.get("c")
            ]
        except Exception:
            continue
    return []


async def genie_agent_detail() -> dict:
    """Per Genie Agent: curation counts (instructions, example SQL, benchmarks,
    sample questions, functions, tables) and a `missing` list. Requires CAN_EDIT on
    the space to read curation; unreadable spaces are reported as such, never as
    'uncurated' (mirrors probe_genie_agents' honesty)."""
    rows = await _genie_audit_rows()
    host, headers = get_workspace_host(), get_auth_headers()
    if not rows or not host or not headers:
        return {"available": bool(rows), "agents": [], "note":
                "No Genie Agents observed in the audit log, or the workspace host/token is unavailable."}
    seen, agents, unreadable = set(), [], 0
    sem = asyncio.Semaphore(8)

    async def _one(space_id: str, title: str):
        async with sem:
            return space_id, title, await _inspect_space(host, headers, space_id, title)

    tasks = []
    for r in rows:
        sid = r.get("space_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        tasks.append(_one(sid, r.get("agent") or sid))
        if len(tasks) >= _MAX_INSPECT:
            break
    for sid, title, (status, data) in await asyncio.gather(*tasks):
        if status != "ok" or not data:
            unreadable += 1
            agents.append({"name": title, "space_id": sid, "curation_readable": False,
                           "missing": ["curation unreadable — grant CAN_EDIT to assess"]})
            continue
        missing = []
        if not data["instructions"]:
            missing.append("instructions")
        if not data["example_sqls"]:
            missing.append("example SQL")
        if not data["benchmarks"]:
            missing.append("benchmarks")
        agents.append({
            "name": data["title"], "space_id": sid, "curation_readable": True,
            "instructions": data["instructions"], "example_sqls": data["example_sqls"],
            "benchmarks": data["benchmarks"], "sample_questions": data["sample_questions"],
            "functions": data["functions"], "tables": data["tables"],
            "missing": missing, "status": "PASS" if not missing else "FAIL",
        })
    return {"available": True, "agents": agents,
            "note": _EDIT_HINT if unreadable else None, "unreadable": unreadable}


async def noncertified_detail(s: dict, pillars: dict) -> list[dict]:
    """Tables carrying a domain/steward tag but NOT certified (the assets most worth
    certifying). Best-effort over table_tags; empty when tags aren't readable."""
    tt = _src("table_tags", s)
    if tt is None:
        return []
    try:
        rows = await execute_sql(
            "SELECT catalog_name AS c, schema_name AS s, table_name AS t, "
            "  MAX(CASE WHEN lower(tag_name) IN ('system.certification_status','certification_status') "
            "      AND lower(tag_value) = 'certified' THEN 1 ELSE 0 END) AS certified "
            f"FROM {tt} GROUP BY catalog_name, schema_name, table_name "
            "HAVING certified = 0 LIMIT 2000"
        )
    except Exception as e:
        logger.info(f"noncertified read failed: {str(e)[:80]}")
        return []
    return [
        {"pillar": pillars.get(r.get("c"), ""), "catalog": r.get("c"), "schema": r.get("s"),
         "entity": r.get("t"), "certified": False, "status": "FAIL"}
        for r in rows if r.get("c")
    ]


async def pages_status() -> dict:
    """Best-effort Genie Ontology *Pages* detection. Pages are a gated Beta with no
    public list API, so this reports 'not assessable' rather than a misleading 0."""
    return {
        "available": False,
        "assessable": False,
        "note": "Genie Ontology Pages are a gated preview with no public list API — "
                "Page coverage cannot be read programmatically yet. Assess Pages manually in "
                "Catalog Explorer ▸ Discover ▸ Pages.",
    }


def _summary(detail: dict) -> dict:
    """Fail/total counts per section for the report's Summary sheet."""
    def ft(rows):
        rows = rows or []
        fails = sum(1 for r in rows if r.get("status") == "FAIL")
        return {"total": len(rows), "failing": fails}
    ga = detail.get("genie_agents", {}).get("agents", [])
    return {
        "catalogs": ft(detail.get("catalogs")),
        "schemas": ft(detail.get("schemas")),
        "entities": ft(detail.get("entities")),
        "columns_uncommented": {"total": len(detail.get("columns_failing") or []),
                                "failing": len(detail.get("columns_failing") or [])},
        "relationships": ft(detail.get("relationships")),
        "metric_views": ft(detail.get("metric_views")),
        "genie_agents": {"total": len(ga), "failing": sum(1 for a in ga if a.get("status") == "FAIL")},
        "noncertified": {"total": len(detail.get("noncertified") or []),
                         "failing": len(detail.get("noncertified") or [])},
    }


async def run_detail() -> dict:
    """Assemble the full entity-level detail payload (scope-aware). Every section is
    best-effort; a section that can't be read comes back empty with the top-level
    still usable."""
    try:
        s = await _resolve_sources()
    except Exception as e:
        ref, msg = safe_error(e, "detail: resolve sources", logger)
        return {"available": False, "note": msg}

    pillars = await _pillar_map(s)
    try:
        catalogs = await catalog_detail(s, pillars)
    except Exception as e:
        logger.info(f"catalog_detail failed: {str(e)[:80]}"); catalogs = []
    try:
        schemas = await schema_detail(s, pillars)
    except Exception as e:
        logger.info(f"schema_detail failed: {str(e)[:80]}"); schemas = []
    try:
        entities, columns_failing = await entity_detail(s, pillars)
    except Exception as e:
        logger.info(f"entity_detail failed: {str(e)[:80]}"); entities, columns_failing = [], []
    try:
        relationships = await relationship_detail(s, pillars)
    except Exception as e:
        logger.info(f"relationship_detail failed: {str(e)[:80]}"); relationships = []
    try:
        metric_views = await metric_view_detail(s, pillars)
    except Exception as e:
        logger.info(f"metric_view_detail failed: {str(e)[:80]}"); metric_views = []
    try:
        genie_agents = await genie_agent_detail()
    except Exception as e:
        logger.info(f"genie_agent_detail failed: {str(e)[:80]}")
        genie_agents = {"available": False, "agents": [], "note": "Genie curation could not be read."}
    try:
        noncertified = await noncertified_detail(s, pillars)
    except Exception as e:
        logger.info(f"noncertified_detail failed: {str(e)[:80]}"); noncertified = []
    pages = await pages_status()

    detail = {
        "available": True,
        "pillar_source": "catalog governed tag (pillar / data_domain / business_domain / data_product)",
        "catalogs": catalogs, "schemas": schemas, "entities": entities,
        "columns_failing": columns_failing, "relationships": relationships,
        "metric_views": metric_views, "genie_agents": genie_agents,
        "noncertified": noncertified, "pages": pages,
    }
    detail["summary"] = _summary(detail)
    return detail
