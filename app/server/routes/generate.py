"""LLM metadata-generation → downloadable Excel.

`POST /generate/stream` runs the entity-level detail (scoped), then uses the
workspace's own Foundation Model API (the same model picker the Plan tab uses) to
DRAFT the missing metadata — catalog/schema/entity descriptions + tags, column
comments, Genie-agent instructions, and metric-view definitions — and derives PK/FK
DDL deterministically. It streams progress (SSE) and ends with a `download_token`;
`GET /generate/excel/{token}` returns the assembled workbook.

Nothing is applied to Unity Catalog. Every output is a suggestion for review — the
PK/FK rows carry ready-to-run `ALTER TABLE … NOT ENFORCED RELY` statements, but the
customer runs them.
"""

import json
import time
import uuid
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Body, Header
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from server.assessment.detail import run_detail
from server.assessment.probes import _resolve_sources, _src, _internal_catalog_filter
from server.sql_client import execute_sql
from server.config import set_user_token
from server.workspace_filter import set_workspace_filter, set_catalog_scope
from server.routes._shared import stream_llm_chat, _ai_model
from server.excel import build_generation_workbook
from server.security import safe_error

logger = logging.getLogger(__name__)
router = APIRouter()

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Bounds so a wide scope can't fan out an unbounded number of LLM calls / blow the
# gateway timeout. Generation is best run scoped to a catalog/schema.
_CAP = {"catalogs": 40, "schemas": 60, "entities": 80, "agents": 15, "mv_schemas": 20}
_CONCURRENCY = 4

# token -> (created_ts, filename, bytes). Small in-process store so the long LLM run
# happens on the SSE request and the browser downloads the finished file separately.
_WB_STORE: dict[str, tuple[float, str, bytes]] = {}
_WB_TTL = 900  # 15 min
_WB_MAX = 20


def _store(fname: str, data: bytes) -> str:
    now = time.time()
    for tok in [t for t, (ts, _, _) in _WB_STORE.items() if now - ts > _WB_TTL]:
        _WB_STORE.pop(tok, None)
    if len(_WB_STORE) >= _WB_MAX:
        _WB_STORE.pop(min(_WB_STORE, key=lambda t: _WB_STORE[t][0]), None)
    tok = uuid.uuid4().hex
    _WB_STORE[tok] = (now, fname, data)
    return tok


class GenerateRequest(BaseModel):
    workspace_filter: Optional[dict] = None
    catalogs: list[str] = []


async def _llm_json(model: str, system: str, user: str, max_tokens: int = 1200, _what: str = ""):
    """Run one FM API completion and parse a JSON object/array from the reply.
    Returns None on any failure (the caller degrades that row to a blank suggestion).
    Every failure is logged with the model + a truncated snippet of what came back, so a
    blank field in the workbook is diagnosable from the app log rather than silent."""
    parts = []
    try:
        async for chunk in stream_llm_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model, max_tokens=max_tokens, temperature=0.2,
        ):
            line = chunk.strip()
            if not line.startswith("data: "):
                continue
            body = line[6:]
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except Exception:
                continue
            if obj.get("error"):
                logger.warning(f"generate: LLM error for {_what or 'item'} (model={model}): "
                               f"{str(obj.get('error'))[:160]}")
                return None
            if obj.get("content"):
                parts.append(obj["content"])
    except Exception as e:
        logger.warning(f"generate: LLM completion failed for {_what or 'item'} (model={model}): {str(e)[:160]}")
        return None
    text = "".join(parts).strip()
    if not text:
        logger.warning(f"generate: empty LLM reply for {_what or 'item'} (model={model})")
        return None
    # Strip ``` fences and locate the JSON payload.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    for lo, hi in (("{", "}"), ("[", "]")):
        i, j = text.find(lo), text.rfind(hi)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    logger.warning(f"generate: could not parse JSON for {_what or 'item'} (model={model}); "
                   f"reply started with: {text[:160]!r}")
    return None


async def _entity_columns(s: dict, keys: set[tuple]) -> dict[tuple, list[str]]:
    """Ordered column names for a set of (catalog, schema, entity) keys (bounded)."""
    col = _src("columns", s)
    if col is None or not keys:
        return {}
    rows = await execute_sql(
        f"SELECT table_catalog AS c, table_schema AS s, table_name AS t, column_name AS col, "
        f"ordinal_position AS pos FROM {col} WHERE table_schema <> 'information_schema' "
        f"{_internal_catalog_filter(s)} ORDER BY table_catalog, table_schema, table_name, ordinal_position "
        f"LIMIT 60000"
    )
    out: dict[tuple, list[str]] = {}
    for r in rows:
        k = (r.get("c"), r.get("s"), r.get("t"))
        if k in keys and r.get("col"):
            out.setdefault(k, []).append(r.get("col"))
    return out


# --- deterministic PK/FK heuristics ----------------------------------------
def _pk_column(entity: str, cols: list[str]) -> Optional[str]:
    low = [c.lower() for c in cols]
    ent = entity.lower().rstrip("s")
    for i, c in enumerate(low):
        if c == "id":
            return cols[i]
    for i, c in enumerate(low):
        if c in (f"{ent}_id", f"{entity.lower()}_id", f"{ent}_key"):
            return cols[i]
    for i, c in enumerate(low):
        if c.endswith("_id") and ent in c:
            return cols[i]
    for i, c in enumerate(low):
        if c.endswith("_id") or c.endswith("_key") or c.endswith("_pk"):
            return cols[i]
    return None


def _fk_links(cat: str, sch: str, entity: str, cols: list[str],
              entity_names: dict[str, str]) -> list[dict]:
    """Suggest FKs: a column `<parent>_id` that matches another scoped entity's name."""
    links = []
    for c in cols:
        cl = c.lower()
        if not cl.endswith("_id"):
            continue
        base = cl[:-3]
        parent = entity_names.get(base) or entity_names.get(base + "s") or entity_names.get(base.rstrip("s"))
        if not parent or parent.lower() == entity.lower():
            continue
        pk_col = c  # convention: FK column name matches the parent PK column name
        stmt = (f"ALTER TABLE {cat}.{sch}.{entity} ADD CONSTRAINT {entity}_{base}_fk "
                f"FOREIGN KEY ({c}) REFERENCES {cat}.{sch}.{parent}({pk_col}) NOT ENFORCED RELY")
        links.append({
            "catalog": cat, "schema": sch, "parent_entity": parent, "column_name": c,
            "foreign_key": f"{entity}.{c}", "primary_key_column": pk_col,
            "foreign_key_column": c, "statement": stmt,
        })
    return links


_SYS_META = ("You are a Databricks Unity Catalog metadata expert preparing an estate for "
             "Genie Ontology. Write concise, business-meaningful descriptions and a single "
             "lower_snake_case governed tag value. Reply with STRICT JSON only, no prose.")


async def _gen_catalog_item(model, sem, cat, pillar, schema_names):
    """One LLM call per catalog. Grounded in the catalog's own schema names (a
    "careful review of metadata and its name", not a bare-name guess) — and, unlike
    a batched call, the result is keyed by call site, not by asking the model to
    echo the name back, so a model-side rewording can never blank every row."""
    async with sem:
        ctx = f" It contains schemas: {json.dumps(schema_names[:40])}." if schema_names else ""
        user = (f"Unity Catalog catalog `{cat}`.{ctx} Based on the catalog name and its "
                "schemas, write a business-meaningful description and a governed tag. "
                'Return STRICT JSON {"description": "<one business sentence>", '
                '"tag": "<one lower_snake_case governed tag value>"}.')
        data = await _llm_json(model, _SYS_META, user, max_tokens=400, _what=f"catalog {cat}") or {}
        return {"pillar": pillar, "catalog": cat,
                "description": data.get("description", ""), "tag": data.get("tag", "")}


async def _gen_schema_item(model, sem, cat, sch, pillar, entity_names):
    """One LLM call per schema, grounded in its entity names."""
    async with sem:
        ctx = f" It contains tables: {json.dumps(entity_names[:60])}." if entity_names else ""
        user = (f"Unity Catalog schema `{cat}.{sch}`.{ctx} Based on the schema name and its "
                "tables, write a business-meaningful description and a governed tag. "
                'Return STRICT JSON {"description": "<one business sentence>", '
                '"tag": "<one lower_snake_case governed tag value>"}.')
        data = await _llm_json(model, _SYS_META, user, max_tokens=400, _what=f"schema {cat}.{sch}") or {}
        return {"pillar": pillar, "catalog": cat, "schema": sch,
                "description": data.get("description", ""), "tag": data.get("tag", "")}


async def _gen_entity(model, sem, cat, sch, ent, pillar, cols):
    """One LLM call per entity: description + tag + a comment for every listed
    (uncommented) column. Returns (entity_row, column_rows) — kept separate so the
    Entity sheet stays entity-grain and columns land in their own Entity_Columns sheet."""
    async with sem:
        ctx = f" It has columns: {json.dumps(cols)}." if cols else ""
        user = (f"Unity Catalog table `{cat}.{sch}.{ent}`.{ctx} Based on the table name, its "
                "schema, and its column names, write a business-meaningful description and a "
                "governed tag for the table, and a short business comment for every listed "
                'column. Return STRICT JSON {"description": "<one business sentence>", '
                '"tag": "<one lower_snake_case governed tag value>", '
                '"columns": {"<column>": "<short business comment>"}}.')
        data = await _llm_json(model, _SYS_META, user, max_tokens=1400, _what=f"entity {cat}.{sch}.{ent}") or {}
        desc, tag = data.get("description", ""), data.get("tag", "")
        comments = data.get("columns", {}) if isinstance(data.get("columns"), dict) else {}
        entity_row = {"pillar": pillar, "catalog": cat, "schema": sch, "entity": ent,
                      "entity_description": desc, "entity_tag": tag}
        column_rows = [
            {"catalog": cat, "schema": sch, "entity": ent, "column": c, "column_comment": comments.get(c, "")}
            for c in cols
        ]
        return entity_row, column_rows


async def _gen_agent(model, sem, name, sid):
    async with sem:
        user = (f'Draft concise, high-quality Genie space instructions for the agent "{name}". '
                "Cover: the business domain, key metrics and their definitions, join guidance, "
                "and answer style. Return STRICT JSON {\"instructions\": \"<text>\"}.")
        data = await _llm_json(model, _SYS_META, user, max_tokens=900, _what=f"genie agent {name}") or {}
        return {"name": name, "space_id": sid, "instructions": data.get("instructions", "")}


async def _gen_metric_view(model, sem, cat, sch, entities_summary):
    async with sem:
        user = (f"Propose ONE Unity Catalog metric view for schema `{cat}.{sch}` based on these "
                f"tables/columns: {json.dumps(entities_summary)[:4000]}. Return STRICT JSON "
                '{"text": "<CREATE VIEW ... WITH METRICS LANGUAGE YAML ... $$ ... $$>"} with '
                "sensible measures and dimensions. Use fully-qualified names.")
        data = await _llm_json(model, _SYS_META, user, max_tokens=1500, _what=f"metric view {cat}.{sch}") or {}
        return {"catalog": cat, "schema": sch, "text": data.get("text", "")}


async def _generate(detail: dict, s: dict, model: str, emit):
    """Build the generation payload from the failing items in `detail`. Every
    catalog/schema/entity is a SEPARATE, grounded LLM call (never a batch the model
    must echo names back from) — a model-side rewording of one item can never blank
    every row, and each item gets real child-metadata context, not a bare name."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    payload: dict = {"catalog": [], "schema": [], "entity": [], "entity_columns": [],
                     "relationship_pk": [], "relationship_fk": [],
                     "genie_agent": [], "metric_views": []}

    # 1. Catalogs — one call per catalog, grounded in its own schema names.
    fail_cats = [r for r in detail.get("catalogs", []) if r.get("status") == "FAIL"][:_CAP["catalogs"]]
    schemas_by_cat: dict[str, list] = {}
    for r in detail.get("schemas", []):
        schemas_by_cat.setdefault(r["catalog"], []).append(r["schema"])
    await emit("catalogs", 0, len(fail_cats))
    if fail_cats:
        ctasks = [_gen_catalog_item(model, sem, r["catalog"], r.get("pillar", ""),
                                    schemas_by_cat.get(r["catalog"], [])) for r in fail_cats]
        done = 0
        for coro in asyncio.as_completed(ctasks):
            payload["catalog"].append(await coro)
            done += 1
            if done % 5 == 0 or done == len(ctasks):
                await emit("catalogs", done, len(ctasks))
    await emit("catalogs", len(fail_cats), len(fail_cats))

    # 2. Schemas — one call per schema, grounded in its own entity names.
    fail_schemas = [r for r in detail.get("schemas", []) if r.get("status") == "FAIL"][:_CAP["schemas"]]
    entities_by_schema: dict[tuple, list] = {}
    for r in detail.get("entities", []):
        entities_by_schema.setdefault((r["catalog"], r["schema"]), []).append(r["entity"])
    await emit("schemas", 0, len(fail_schemas))
    if fail_schemas:
        stasks = [_gen_schema_item(model, sem, r["catalog"], r["schema"], r.get("pillar", ""),
                                   entities_by_schema.get((r["catalog"], r["schema"]), [])) for r in fail_schemas]
        done = 0
        for coro in asyncio.as_completed(stasks):
            payload["schema"].append(await coro)
            done += 1
            if done % 5 == 0 or done == len(stasks):
                await emit("schemas", done, len(stasks))
    await emit("schemas", len(fail_schemas), len(fail_schemas))

    # 3. Entities (per-entity LLM; needs column names). Each call returns an
    # entity-grain row plus its own column-grain rows — kept in separate payload
    # lists so the workbook's Entity and Entity_Columns sheets stay at their own grain.
    fail_entities = [r for r in detail.get("entities", []) if r.get("status") == "FAIL"][:_CAP["entities"]]
    ent_keys = {(r["catalog"], r["schema"], r["entity"]) for r in fail_entities}
    cols_map = await _entity_columns(s, ent_keys)
    # uncommented columns per entity → only comment those
    failing_cols_by: dict[tuple, list] = {}
    for fc in detail.get("columns_failing", []):
        failing_cols_by.setdefault((fc["catalog"], fc["schema"], fc["entity"]), []).append(fc["column"])
    await emit("entities", 0, len(fail_entities))
    tasks = []
    for r in fail_entities:
        k = (r["catalog"], r["schema"], r["entity"])
        cols = failing_cols_by.get(k) or cols_map.get(k, [])
        tasks.append(_gen_entity(model, sem, r["catalog"], r["schema"], r["entity"], r.get("pillar", ""), cols))
    done = 0
    for coro in asyncio.as_completed(tasks):
        entity_row, column_rows = await coro
        payload["entity"].append(entity_row)
        payload["entity_columns"].extend(column_rows)
        done += 1
        if done % 5 == 0 or done == len(tasks):
            await emit("entities", done, len(tasks))

    # 4. Relationships (deterministic PK/FK from column names)
    rel_fail = [r for r in detail.get("relationships", []) if r.get("status") == "FAIL"]
    entity_names = {r["entity"].lower(): r["entity"] for r in detail.get("relationships", [])}
    await emit("relationships", 0, len(rel_fail))
    for r in rel_fail:
        cat, sch, ent = r["catalog"], r["schema"], r["entity"]
        cols = cols_map.get((cat, sch, ent), [])
        pk = _pk_column(ent, cols)
        if pk:
            payload["relationship_pk"].append({
                "catalog": cat, "schema": sch, "parent_entity": ent, "column_name": pk,
                "statement": f"ALTER TABLE {cat}.{sch}.{ent} ADD CONSTRAINT {ent}_pk PRIMARY KEY ({pk})",
            })
        payload["relationship_fk"].extend(_fk_links(cat, sch, ent, cols, entity_names))
    await emit("relationships", len(rel_fail), len(rel_fail))

    # 5. Genie agents missing instructions
    agents = [a for a in detail.get("genie_agents", {}).get("agents", [])
              if a.get("curation_readable") and "instructions" in (a.get("missing") or [])][:_CAP["agents"]]
    await emit("genie_agents", 0, len(agents))
    if agents:
        atasks = [_gen_agent(model, sem, a["name"], a["space_id"]) for a in agents]
        payload["genie_agent"] = await asyncio.gather(*atasks)
    await emit("genie_agents", len(agents), len(agents))

    # 6. Metric views — one proposed per schema that has entities (cap)
    ent_by_schema: dict[tuple, list] = {}
    for r in detail.get("entities", []):
        ent_by_schema.setdefault((r["catalog"], r["schema"]), []).append(
            {"entity": r["entity"], "columns": r.get("columns")})
    mv_schemas = list(ent_by_schema.items())[:_CAP["mv_schemas"]]
    await emit("metric_views", 0, len(mv_schemas))
    if mv_schemas:
        mtasks = [_gen_metric_view(model, sem, cat, sch, ents) for (cat, sch), ents in mv_schemas]
        payload["metric_views"] = await asyncio.gather(*mtasks)
    await emit("metric_views", len(mv_schemas), len(mv_schemas))

    return payload


@router.post("/generate/stream")
async def generate_stream(
    req: GenerateRequest = Body(default=GenerateRequest()),
    x_forwarded_access_token: Optional[str] = Header(default=None),
):
    """Run generation, streaming SSE progress, ending with a download token."""
    wsf, cat_scope = req.workspace_filter, (req.catalogs or None)
    # Capture the request's model NOW — the X-AI-Model middleware resets the
    # contextvar before the streaming body runs, so re-apply it inside the generator.
    model = _ai_model.get()

    async def gen():
        set_user_token(x_forwarded_access_token)
        set_workspace_filter(wsf)
        set_catalog_scope(cat_scope)

        queue: asyncio.Queue = asyncio.Queue()

        async def _emit(stage, done, total):
            await queue.put({"type": "progress", "stage": stage, "done": done, "total": total})

        async def _run():
            try:
                detail = await run_detail()
                if not detail.get("available"):
                    await queue.put({"type": "error", "error": detail.get("note", "No detail available.")})
                    return
                payload = await _generate(detail, await _resolve_sources(), model, _emit)
                buf = build_generation_workbook(payload)
                tok = _store("genie-ontology-readiness-generated.xlsx", buf.getvalue())
                counts = {k: len(v) for k, v in payload.items()}
                await queue.put({"type": "complete", "download_token": tok, "counts": counts})
            except Exception as e:
                ref, msg = safe_error(e, "generate stream", logger)
                await queue.put({"type": "error", "error": msg, "reference": ref})
            finally:
                await queue.put(None)

        task = asyncio.ensure_future(_run())
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            if not task.done():
                task.cancel()
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/generate/excel/{token}")
async def generate_excel(token: str):
    """Download a previously generated workbook by its token."""
    entry = _WB_STORE.get(token)
    if entry is None:
        return JSONResponse(status_code=404, content={"error": "Generated file expired or not found — re-run generation."})
    _ts, fname, data = entry

    def _body():
        yield data

    return StreamingResponse(
        _body(), media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
