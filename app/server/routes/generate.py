"""LLM metadata-generation → downloadable Excel.

`POST /generate/stream` runs the entity-level detail (scoped), then uses the
workspace's own Foundation Model API to DRAFT the missing metadata —
catalog/schema/entity descriptions + tags, column comments, Genie-agent
instructions, and metric-view definitions — and derives PK/FK DDL
deterministically. It streams progress (SSE) and ends with a `download_token`;
`GET /generate/excel/{token}` returns the assembled workbook.

Nothing is applied to Unity Catalog. Every output is a suggestion for review — the
PK/FK rows carry ready-to-run `ALTER TABLE … NOT ENFORCED RELY` statements, but the
customer runs them.

MODEL INVOCATION: every LLM call goes through Databricks SQL's `ai_query()`
built-in, executed via the SAME SQL-warehouse connection (`execute_sql`) the
entity-level assessment itself already uses — not a direct REST call to
`/serving-endpoints/.../invocations`. This intentionally mirrors the proven
pattern in this app's own `app/accelerators/metadata-ai-comments/` notebook
accelerator (which calls `ai_query()` from `spark.sql()` and is documented on the
Learn tab as "AI-generated, glossary-grounded column comments"). Two prior
iterations of this endpoint used the REST/streaming path (the same one the Plan
tab's chat uses) and, even after fixing a real batching bug, case-sensitive key
matching, and reply truncation, every generated field still came back blank —
pointing at the REST/streaming call itself, not the JSON-parsing layer sitting on
top of it, as this app's assessment queries (same `execute_sql` path `ai_query`
now shares) are demonstrably working. `model` (from the header-driven model
picker) is passed straight through as `ai_query`'s endpoint-name argument, so the
model selector still names the actual serving endpoint invoked.
"""

import json
import re
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
from server.routes._shared import _ai_model
from server.excel import build_generation_workbook
from server.security import quote_literal, safe_error

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


async def _complete(model: str, prompt: str, _what: str) -> Optional[str]:
    """Run one model completion via Databricks SQL's `ai_query()` built-in,
    executed through the existing SQL-warehouse connection (`execute_sql`) — the
    same call path the entity-level assessment itself uses, and the one this
    app's own metadata-ai-comments accelerator demonstrates working from
    `spark.sql()`. `ai_query` takes a single combined prompt (no separate
    system/user roles in the simple 2-argument form used here — the same form the
    accelerator uses) and returns the model's full text reply as a plain string;
    there is no streaming/SSE to reassemble.

    The endpoint name is embedded as an escaped SQL string literal (matching how
    the accelerator itself interpolates it), not a bind parameter — some
    Databricks Runtime versions resolve an AI function's endpoint argument at
    query-analysis time, before parameter substitution. `record=False`: a wide
    generation run can fire many of these, and they'd otherwise flood the
    assessment's "SQL behind this score" disclosure with near-identical entries.

    Returns the trimmed reply text (possibly empty), or None on a query/transport
    failure. Every failure is logged so a blank field is diagnosable from the app
    log rather than silent.
    """
    try:
        rows = await execute_sql(
            f"SELECT ai_query({quote_literal(model)}, :prompt) AS resp",
            parameters={"prompt": prompt},
            record=False,
        )
    except Exception as e:
        logger.warning(f"generate: ai_query failed for {_what or 'item'} (model={model}): {str(e)[:200]}")
        return None
    if not rows:
        logger.warning(f"generate: ai_query returned no rows for {_what or 'item'} (model={model})")
        return None
    resp = rows[0].get("resp")
    return (resp or "").strip()


def _extract_json(text: str):
    """Best-effort JSON object/array extraction from free-form model text: strip a
    ``` fence if present, then take the outer {..} or [..] span. Models routinely
    add a preamble ("Sure, here's the JSON:") or wrap the reply in a code fence
    despite being told not to — this tolerates both."""
    if not text:
        return None
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
    return None


_JSON_ONLY_REMINDER = (
    "\n\nYour previous reply did not contain a single valid JSON object and could not be used. "
    "Reply again with ONLY the JSON object — no markdown code fences, no explanation, nothing "
    "before or after it."
)


async def _llm_json(model: str, system: str, user: str, _what: str = ""):
    """Run a model completion (via `_complete` / `ai_query`) and parse a JSON
    object/array from the reply, retrying ONCE with a stricter reminder if the
    first reply wasn't valid JSON — models routinely add a preamble or wrap the
    JSON in prose despite explicit instructions not to. `system` and `user` are
    combined into one prompt (`ai_query`'s simple form has no separate role
    turns). A transport/query failure is NOT retried (already logged, and an
    immediate retry won't fix it). Returns None if every attempt fails; every
    failure is logged with the model + a truncated snippet."""
    prompt = f"{system}\n\n{user}"
    for attempt in range(2):
        text = await _complete(model, prompt, _what)
        if text is None:
            return None
        if text:
            parsed = _extract_json(text)
            if parsed is not None:
                return parsed
            logger.warning(f"generate: could not parse JSON for {_what or 'item'} "
                           f"(model={model}, attempt {attempt + 1}); reply started with: {text[:160]!r}")
        else:
            logger.warning(f"generate: empty LLM reply for {_what or 'item'} "
                           f"(model={model}, attempt {attempt + 1})")
        prompt = f"{system}\n\n{user}" + _JSON_ONLY_REMINDER
    return None


def _norm_keys(data) -> dict:
    return {str(k).strip().lower(): v for k, v in data.items()} if isinstance(data, dict) else {}


def _pick_str(data: dict, *keys: str) -> str:
    """Case/whitespace-tolerant field lookup. Despite explicit "use exactly this
    lowercase key" instructions, models sometimes still capitalize a JSON key
    ("Description") or rename it ("desc") — a strict `data.get("description")`
    would then read as permanently blank even though the model DID answer. Tries
    each candidate key (in priority order), matched case-insensitively."""
    norm = _norm_keys(data)
    for k in keys:
        v = norm.get(k.strip().lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _pick_dict(data: dict, *keys: str) -> dict:
    norm = _norm_keys(data)
    for k in keys:
        v = norm.get(k.strip().lower())
        if isinstance(v, dict):
            return v
    return {}


def _pick_bool(data: dict, *keys: str) -> Optional[bool]:
    """Case-tolerant boolean lookup — the model may reply with a real JSON bool
    or a string ("true"/"yes"). Returns None (not False) when absent, so callers
    can tell "the model didn't say" from "the model said no"."""
    norm = _norm_keys(data)
    for k in keys:
        v = norm.get(k.strip().lower())
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "yes", "1"):
                return True
            if s in ("false", "no", "0"):
                return False
    return None


def _pick_column_comment(comments: dict, column: str) -> str:
    """Column-name lookup inside a model-returned {column: comment} map, tolerant
    of the model changing the column name's case in its reply."""
    if not isinstance(comments, dict):
        return ""
    v = comments.get(column)
    if isinstance(v, str) and v.strip():
        return v.strip()
    low = {str(k).strip().lower(): val for k, val in comments.items()}
    v = low.get(column.strip().lower())
    return v.strip() if isinstance(v, str) and v.strip() else ""


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


# --- deterministic tagging heuristics ---------------------------------------
# These back-stop the LLM for the tag fields that must always have SOME value
# (quality_tier, table_type) or benefit from a second, non-LLM signal (pii) —
# unlike description/tag/usecase, which are purely the model's judgment and stay
# blank (a real failure signal) if the call didn't produce anything usable.

_GOLD_RE = re.compile(r"(gold|mart|marts|analytics|semantic|presentation|reporting|dwh)", re.I)
_SILVER_RE = re.compile(r"(silver|clean|curated|conformed|refined)", re.I)


def _quality_tier(catalog: str, schema: str) -> str:
    """Bronze / Silver / Gold, inferred from medallion-style naming — the same
    gold-layer name pattern the assessment's relationships probe already uses
    (probes.py's gold_tables query). Always returns one of the three; Bronze is
    the conservative default when the name carries no layer signal."""
    name = f"{catalog}.{schema}".lower()
    if _GOLD_RE.search(name):
        return "Gold"
    if _SILVER_RE.search(name):
        return "Silver"
    return "Bronze"


_PII_COLUMN_RE = re.compile(
    r"(email|phone|mobile|ssn|social_security|passport|address|street|zip|postal|"
    r"\bdob\b|date_of_birth|birth_date|full_name|first_name|last_name|maiden_name|"
    r"credit_card|card_number|\biban\b|tax_id|national_id|driver_license|"
    r"ip_address|geolocation|\blat\b|\blon\b|\blng\b|salary|income)",
    re.I,
)


def _looks_like_pii(cols: list[str]) -> bool:
    """A cheap, deterministic OR-signal alongside the model's own judgment —
    common PII-shaped column names. Errs toward flagging for review rather than
    missing a PII column the model's judgment alone might not catch."""
    return any(_PII_COLUMN_RE.search(c) for c in cols)


def _heuristic_table_type(entity: str, cols: list[str]) -> str:
    """Fallback when the model's table_type isn't one of the three valid values —
    name-prefix convention first, then a light column-shape signal (several FK-
    like columns plus at least one measure-like column reads as a fact table)."""
    name = entity.lower()
    if name.startswith(("dim_", "d_")):
        return "dimension"
    if name.startswith(("fact_", "f_")):
        return "fact"
    if name.startswith(("mv_", "metric_")) or "metric_view" in name:
        return "metric"
    low_cols = [c.lower() for c in cols]
    fk_like = sum(1 for c in low_cols if c.endswith(("_id", "_key")))
    measure_like = sum(1 for c in low_cols
                       if any(t in c for t in ("amount", "total", "qty", "quantity", "price", "count",
                                                "revenue", "cost")))
    return "fact" if fk_like >= 2 and measure_like >= 1 else "dimension"


def _compose_tag(*pairs: str) -> str:
    """Join already-formatted "key = value" pairs (and blank ones are dropped),
    comma-separated, for the *_Tag_Generated cells."""
    return ", ".join(p for p in pairs if p)


def _normalize_tag_value(value: str, max_words: int = 4) -> str:
    """Coerce a model-returned tag VALUE (data_product, usecase) into a short,
    lower_snake_case category label — a safety net alongside the prompt
    instructions, for when the model answers a categorical request ("customer")
    with a descriptive sentence ("Tracks customer orders and their history.")
    despite being told not to. Strips trailing punctuation, lowercases, and
    truncates to the first few words, so a stray sentence degrades to a short
    label instead of polluting the tag with prose. NOT applied to `description`
    (meant to be a full sentence) or the fixed-enum fields (table_type,
    quality_tier, pii)."""
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    return "_".join(w.lower() for w in words[:max_words])


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


_SYS_META = (
    "You are a Databricks Unity Catalog metadata expert preparing an estate for Genie "
    "Ontology. You are given the name of a catalog, schema, table, or column plus its real "
    "child metadata (schema/table/column names) — ground your answer in that metadata, not "
    "just the name.\n\n"
    "Output rules — follow exactly, every time:\n"
    "1. Reply with ONLY a single JSON object: no markdown code fences, no preamble, no "
    "explanation, nothing before or after the JSON.\n"
    "2. Use EXACTLY the lowercase field names given in the user's requested schema — do not "
    "capitalize, translate, rename, or add fields.\n"
    "3. A value that spans multiple lines (e.g. SQL) must use escaped \\n sequences — never a "
    "raw line break inside a JSON string, or the JSON becomes invalid.\n"
    "4. Any field asked for as a 'category label' or 'tag value' is a CLASSIFICATION, not a "
    "description: 1-3 lower_snake_case words (e.g. customer, retail_metrics, region, "
    "date_dimension), never a sentence and never punctuation. Only a field explicitly asked "
    "for as a 'sentence' (like description) may be a full sentence.\n"
    "Example of a correctly formatted reply (field names will vary by request): "
    '{"description": "Stores customer order history.", "tag": "sales"}'
)

# Shared phrasing for every "usecase" ask, so catalog/schema/entity request the
# SAME short category label — not a description. Reused verbatim so a prompt
# tweak only has to happen in one place.
_USECASE_ASK = (
    'a SHORT business-domain category label for {noun} (1-3 lower_snake_case words, '
    "e.g. customer, retail_metrics, region, date_dimension, inventory, finance, marketing, "
    "product_catalog) — a CLASSIFICATION, not a description of what it does"
)


async def _gen_catalog_item(model, sem, cat, pillar, schema_names):
    """One LLM call per catalog. Grounded in the catalog's own schema names (a
    "careful review of metadata and its name", not a bare-name guess) — and, unlike
    a batched call, the result is keyed by call site, not by asking the model to
    echo the name back, so a model-side rewording can never blank every row.

    Catalog_Tag_Generated is rendered as the single governed pair
    `data_product = <value>` — the key is fixed; only the value is the model's."""
    async with sem:
        ctx = f" It contains schemas: {json.dumps(schema_names[:40])}." if schema_names else ""
        user = (f"Unity Catalog catalog `{cat}`.{ctx} Based on the catalog name and its "
                "schemas, write a business-meaningful description and a short "
                "lower_snake_case value naming the data product this catalog belongs to. "
                'Return STRICT JSON {"description": "<one business sentence>", '
                '"data_product": "<one lower_snake_case value>"}.')
        data = await _llm_json(model, _SYS_META, user, _what=f"catalog {cat}") or {}
        data_product = _normalize_tag_value(_pick_str(data, "data_product", "tag", "governed_tag"))
        return {"pillar": pillar, "catalog": cat,
                "description": _pick_str(data, "description"),
                "tag": f"data_product = {data_product}" if data_product else ""}


async def _gen_schema_item(model, sem, cat, sch, pillar, entity_names, catalog_tag):
    """One LLM call per schema, grounded in its entity names.

    Schema_Tag_Generated rolls up the parent catalog's OWN generated tag
    (`catalog_tag` — looked up by catalog name in the already-built Catalog sheet;
    omitted if that catalog wasn't itself generated, e.g. it already had a real
    description/tag) plus `quality_tier` (deterministic — Bronze/Silver/Gold from
    medallion-style naming) and `usecase` — a SHORT category label (e.g.
    `customer`, `region`), never a descriptive sentence."""
    async with sem:
        ctx = f" It contains tables: {json.dumps(entity_names[:60])}." if entity_names else ""
        usecase_ask = _USECASE_ASK.format(noun="this schema")
        user = (f"Unity Catalog schema `{cat}.{sch}`.{ctx} Based on the schema name and its "
                f"tables, write a business-meaningful description and {usecase_ask}. "
                'Return STRICT JSON {"description": "<one business sentence>", '
                '"usecase": "<the short category label>"}.')
        data = await _llm_json(model, _SYS_META, user, _what=f"schema {cat}.{sch}") or {}
        desc = _pick_str(data, "description")
        usecase = _normalize_tag_value(_pick_str(data, "usecase"))
        tier = _quality_tier(cat, sch)
        tag = _compose_tag(catalog_tag, f"quality_tier = {tier}", f"usecase = {usecase}" if usecase else "")
        return {"pillar": pillar, "catalog": cat, "schema": sch,
                "description": desc, "usecase": usecase, "quality_tier": tier, "tag": tag}


async def _gen_entity(model, sem, cat, sch, ent, pillar, cols, catalog_tag,
                      schema_quality_tier, schema_usecase):
    """One LLM call per entity: description + table_type + pii + usecase + a
    comment for every listed (uncommented) column. Returns (entity_row,
    column_rows) — kept separate so the Entity sheet stays entity-grain and
    columns land in their own Entity_Columns sheet.

    Entity_Tag_Generated rolls up NAMED COMPONENTS rather than gluing in whole
    ancestor tag strings (which used to duplicate `data_product` — it's already
    inside the schema's own composed tag — and left two anonymous `usecase`
    pairs indistinguishable from each other):
      - `catalog_tag` (already "data_product = <value>", or "" if the catalog
        wasn't itself generated) — included once, verbatim.
      - `schema_quality_tier` / `schema_usecase` — the SCHEMA's own raw values
        (not its composed tag string), rendered here as `quality_tier = …` and
        `schema_usecase = …` so the schema's usecase is never confused with the
        entity's own.
      - `table_type` (dimension/fact/metric — the model's read, backstopped by
        a naming/column-shape heuristic when the model's answer isn't one of
        the three) and `pii` (true if EITHER the model or a column-name
        heuristic flags it) — this entity's own.
      - `entity_usecase` — this entity's own SHORT category label (e.g.
        `customer`, `retail_metrics`), never a descriptive sentence."""
    async with sem:
        ctx = f" It has columns: {json.dumps(cols)}." if cols else ""
        usecase_ask = _USECASE_ASK.format(noun="this table")
        user = (f"Unity Catalog table `{cat}.{sch}.{ent}`.{ctx} Based on the table name, its "
                f"schema, and its column names, analyze it. usecase must be {usecase_ask}. "
                'Return STRICT JSON with these exact fields: {"description": "<one business '
                'sentence>", "table_type": "<dimension, fact, or metric>", '
                '"pii": <true or false — true if any column likely holds personal data>, '
                '"usecase": "<the short category label>", '
                '"columns": {"<column>": "<short business comment>"}}. '
                "table_type guidance: 'dimension' for a descriptive/reference entity (a "
                "business key plus slowly-changing attributes), 'fact' for a "
                "transactional/measurement entity (foreign keys plus numeric measures), "
                "'metric' for a semantic/metric view. Comment every listed column.")
        data = await _llm_json(model, _SYS_META, user, _what=f"entity {cat}.{sch}.{ent}") or {}
        desc = _pick_str(data, "description")
        entity_usecase = _normalize_tag_value(_pick_str(data, "usecase"))
        comments = _pick_dict(data, "columns", "column_comments", "comments")

        table_type = _pick_str(data, "table_type").lower()
        if table_type not in ("dimension", "fact", "metric"):
            table_type = _heuristic_table_type(ent, cols)

        pii_llm = _pick_bool(data, "pii")
        pii = bool(pii_llm) or _looks_like_pii(cols)

        tag = _compose_tag(
            catalog_tag,
            f"quality_tier = {schema_quality_tier}" if schema_quality_tier else "",
            f"schema_usecase = {schema_usecase}" if schema_usecase else "",
            f"table_type = {table_type}",
            f"pii = {'true' if pii else 'false'}",
            f"entity_usecase = {entity_usecase}" if entity_usecase else "",
        )
        entity_row = {"pillar": pillar, "catalog": cat, "schema": sch, "entity": ent,
                      "entity_description": desc, "entity_usecase": entity_usecase, "entity_tag": tag}
        column_rows = [
            {"catalog": cat, "schema": sch, "entity": ent, "column": c,
             "column_comment": _pick_column_comment(comments, c)}
            for c in cols
        ]
        return entity_row, column_rows


async def _gen_agent(model, sem, name, sid):
    async with sem:
        user = (f'Draft concise, high-quality Genie space instructions for the agent "{name}". '
                "Cover: the business domain, key metrics and their definitions, join guidance, "
                "and answer style. Return STRICT JSON {\"instructions\": \"<text>\"}.")
        data = await _llm_json(model, _SYS_META, user, _what=f"genie agent {name}") or {}
        return {"name": name, "space_id": sid, "instructions": _pick_str(data, "instructions")}


async def _gen_metric_view(model, sem, cat, sch, entities_summary):
    async with sem:
        user = (f"Propose ONE Unity Catalog metric view for schema `{cat}.{sch}` based on these "
                f"tables/columns: {json.dumps(entities_summary)[:4000]}. Return STRICT JSON "
                '{"text": "<CREATE VIEW ... WITH METRICS LANGUAGE YAML ... $$ ... $$>"} with '
                "sensible measures and dimensions. Use fully-qualified names.")
        data = await _llm_json(model, _SYS_META, user, _what=f"metric view {cat}.{sch}") or {}
        return {"catalog": cat, "schema": sch, "text": _pick_str(data, "text", "sql", "ddl")}


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

    # Catalog Sheet's OWN generated tag, by catalog name — the ancestor lookup
    # schemas (and, transitively, entities) roll into their own Tag_Generated.
    # A catalog that wasn't itself generated (it already had a real tag) simply
    # has no entry here, and its pair is omitted downstream — never fabricated.
    catalog_tag_by_name: dict[str, str] = {r["catalog"]: r["tag"] for r in payload["catalog"] if r.get("tag")}

    # 2. Schemas — one call per schema, grounded in its own entity names.
    fail_schemas = [r for r in detail.get("schemas", []) if r.get("status") == "FAIL"][:_CAP["schemas"]]
    entities_by_schema: dict[tuple, list] = {}
    for r in detail.get("entities", []):
        entities_by_schema.setdefault((r["catalog"], r["schema"]), []).append(r["entity"])
    await emit("schemas", 0, len(fail_schemas))
    if fail_schemas:
        stasks = [_gen_schema_item(model, sem, r["catalog"], r["schema"], r.get("pillar", ""),
                                   entities_by_schema.get((r["catalog"], r["schema"]), []),
                                   catalog_tag_by_name.get(r["catalog"], "")) for r in fail_schemas]
        done = 0
        for coro in asyncio.as_completed(stasks):
            payload["schema"].append(await coro)
            done += 1
            if done % 5 == 0 or done == len(stasks):
                await emit("schemas", done, len(stasks))
    await emit("schemas", len(fail_schemas), len(fail_schemas))

    # Schema Sheet's OWN generated rows, by (catalog, schema) — the entity's tag
    # pulls the schema's RAW quality_tier/usecase (not its composed tag string;
    # that string already has the catalog's data_product baked in, which would
    # duplicate it in the entity's tag too). Absent if that schema wasn't itself
    # generated — its pairs are then simply omitted, never fabricated.
    schema_row_by_key: dict[tuple, dict] = {(r["catalog"], r["schema"]): r for r in payload["schema"]}

    # 3. Entities (per-entity LLM; needs column names). Each call returns an
    # entity-grain row plus its own column-grain rows — kept in separate payload
    # lists so the workbook's Entity and Entity_Columns sheets stay at their own
    # grain. __materialization* tables are Lakeflow/DLT-internal materialization
    # aliases, not user-facing tables — never generated, never included in the
    # workbook.
    fail_entities = [r for r in detail.get("entities", []) if r.get("status") == "FAIL"
                     and not r["entity"].lower().startswith("__materialization")][:_CAP["entities"]]
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
        sch_row = schema_row_by_key.get((r["catalog"], r["schema"]), {})
        tasks.append(_gen_entity(model, sem, r["catalog"], r["schema"], r["entity"], r.get("pillar", ""), cols,
                                 catalog_tag_by_name.get(r["catalog"], ""),
                                 sch_row.get("quality_tier", ""), sch_row.get("usecase", "")))
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


def _blank(v) -> bool:
    return not (v or "").strip()


def _compute_failures(payload: dict) -> dict:
    """Per-section count of rows whose LLM-drafted field(s) came back blank — the
    call ran (or was attempted) but produced no usable content; `_llm_json` already
    logged why. Deterministic sections (relationship_pk/fk derived from column
    names; quality_tier, table_type's heuristic fallback, and pii's column-name
    OR-signal) are never counted here — checking the rendered Tag_Generated cell
    would under-count, since those deterministic pieces make it non-blank even
    when the LLM portion (description/usecase) came back empty. Surfaced in the
    SSE `complete` event so a partial failure is visible in the UI, not just the
    app log."""
    def count(rows, *fields):
        return sum(1 for r in rows if all(_blank(r.get(f)) for f in fields))
    return {
        "catalog": count(payload.get("catalog", []), "description", "tag"),
        "schema": count(payload.get("schema", []), "description", "usecase"),
        "entity": count(payload.get("entity", []), "entity_description", "entity_usecase"),
        "entity_columns": count(payload.get("entity_columns", []), "column_comment"),
        "genie_agent": count(payload.get("genie_agent", []), "instructions"),
        "metric_views": count(payload.get("metric_views", []), "text"),
    }


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
                failures = _compute_failures(payload)
                await queue.put({"type": "complete", "download_token": tok, "counts": counts, "failures": failures})
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
