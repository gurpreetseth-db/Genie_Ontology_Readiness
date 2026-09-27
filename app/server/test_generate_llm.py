"""LLM-call plumbing for the Generate tab: the ai_query() invocation, JSON
extraction, and the per-item catalog/schema/entity generators.

Two bugs are guarded against here, in order of discovery:

1. The ORIGINAL design sent the LLM a BATCH of catalog/schema names and asked
   it to echo each "name" back in a JSON array, matching results by that
   string — any model-side rewording (case, whitespace, dropping a `catalog.`
   prefix) made the lookup miss for EVERY row. Fixed by making every
   catalog/schema/entity a separate, grounded call (see PerItemGenerationTest).

2. Even after that fix, every generated field stayed blank. The remaining
   cause: `_complete` called the model via a direct REST call to
   `/serving-endpoints/.../invocations` (the same path the Plan tab's chat
   uses) — while this app's own metadata-ai-comments accelerator proves
   `ai_query()` (via `spark.sql()`) is the working invocation path in this
   deployment. `_complete` now runs `ai_query()` through the SAME SQL-warehouse
   connection (`execute_sql`) the assessment itself already uses successfully,
   mocked here via `execute_sql` rather than the old `stream_llm_chat` SSE shape.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from server.routes import generate as gen


class CompleteTest(unittest.IsolatedAsyncioTestCase):
    """_complete: the ai_query() call itself, mocked at the execute_sql boundary
    (the same mocking convention this repo's other async tests use, e.g.
    server/assessment/test_genie_audit.py's `patch.object(probes, "execute_sql", ...)`)."""

    async def test_returns_the_resp_column_trimmed(self):
        execute = AsyncMock(return_value=[{"resp": "  hello  "}])
        with patch.object(gen, "execute_sql", execute):
            result = await gen._complete("model", "prompt text", "catalog main")
        self.assertEqual(result, "hello")
        # The endpoint name is an escaped SQL literal (ai_query's endpoint arg
        # is resolved at query-analysis time on some DBR versions, so it can't
        # always be a bind parameter); the prompt itself IS a bind parameter.
        query = execute.await_args.args[0]
        self.assertIn("ai_query('model', :prompt)", query)
        self.assertEqual(execute.await_args.kwargs["parameters"], {"prompt": "prompt text"})
        # Many generation calls can fire in one run; none should flood the
        # assessment's "SQL behind this score" disclosure.
        self.assertFalse(execute.await_args.kwargs["record"])

    async def test_escapes_a_quote_in_the_endpoint_name(self):
        execute = AsyncMock(return_value=[{"resp": "ok"}])
        with patch.object(gen, "execute_sql", execute):
            await gen._complete("o'brien-endpoint", "prompt", "item")
        query = execute.await_args.args[0]
        self.assertIn("ai_query('o''brien-endpoint', :prompt)", query)

    async def test_returns_none_on_query_failure(self):
        execute = AsyncMock(side_effect=RuntimeError("warehouse unreachable"))
        with patch.object(gen, "execute_sql", execute):
            result = await gen._complete("model", "prompt", "item")
        self.assertIsNone(result)

    async def test_returns_none_on_no_rows(self):
        execute = AsyncMock(return_value=[])
        with patch.object(gen, "execute_sql", execute):
            result = await gen._complete("model", "prompt", "item")
        self.assertIsNone(result)


def _resp(execute_mock, text: str):
    """Configure the execute_sql mock to return one ai_query-shaped row."""
    execute_mock.return_value = [{"resp": text}]


class LlmJsonTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_plain_object(self):
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "Sales data.", "tag": "sales"})}])
        with patch.object(gen, "execute_sql", execute):
            result = await gen._llm_json("model", "sys", "user")
        self.assertEqual(result, {"description": "Sales data.", "tag": "sales"})

    async def test_strips_code_fence(self):
        body = json.dumps({"description": "Order facts.", "tag": "orders"})
        execute = AsyncMock(return_value=[{"resp": f"```json\n{body}\n```"}])
        with patch.object(gen, "execute_sql", execute):
            result = await gen._llm_json("model", "sys", "user")
        self.assertEqual(result, {"description": "Order facts.", "tag": "orders"})

    async def test_returns_none_when_the_query_itself_fails(self):
        execute = AsyncMock(side_effect=RuntimeError("endpoint not found"))
        with patch.object(gen, "execute_sql", execute):
            result = await gen._llm_json("model", "sys", "user")
        self.assertIsNone(result)

    async def test_retries_once_with_stricter_reminder_and_recovers(self):
        """First attempt: unparsable prose. Second attempt (after _llm_json
        appends the stricter JSON-only reminder to the combined prompt): valid
        JSON. Proves the retry-with-correction path actually recovers a model
        that ignored the format instruction the first time."""
        execute = AsyncMock()

        async def _side_effect(query, parameters=None, record=True):
            prompt = parameters["prompt"]
            if "did not contain a single valid JSON object" in prompt:
                return [{"resp": json.dumps({"description": "Recovered on retry.", "tag": "sales"})}]
            return [{"resp": "Sure! Here's some information, but not in JSON form."}]

        execute.side_effect = _side_effect
        with patch.object(gen, "execute_sql", execute):
            result = await gen._llm_json("model", "sys", "user prompt")
        self.assertEqual(result, {"description": "Recovered on retry.", "tag": "sales"})
        self.assertEqual(execute.await_count, 2)

    async def test_gives_up_after_two_failed_attempts(self):
        execute = AsyncMock(return_value=[{"resp": "I cannot produce that."}])
        with patch.object(gen, "execute_sql", execute):
            result = await gen._llm_json("model", "sys", "user prompt")
        self.assertIsNone(result)
        self.assertEqual(execute.await_count, 2)


class PerItemGenerationTest(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the original batching bug: catalog/schema/entity
    generation must never depend on the model echoing a name back. Also covers
    the key=value tag format and the ancestor tag rollup (catalog -> schema ->
    entity), each looked up by name in the already-built sheet."""

    async def test_catalog_item_does_not_depend_on_name_echo(self):
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "Sales data.", "data_product": "sales"})}])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_catalog_item("model", sem, "main", "sales_pillar", ["s1", "s2"])
        self.assertEqual(row, {"pillar": "sales_pillar", "catalog": "main",
                               "description": "Sales data.", "tag": "data_product = sales"})

    async def test_catalog_item_blank_data_product_leaves_tag_blank(self):
        """An empty value must not render as the dangling "data_product = " —
        that would (a) look broken and (b) mask a real generation failure from
        _compute_failures, which blank-checks the whole tag field."""
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "", "data_product": ""})}])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_catalog_item("model", sem, "main", "sales_pillar", [])
        self.assertEqual(row["tag"], "")

    async def test_schema_item_rolls_up_catalog_tag_plus_quality_tier_and_usecase(self):
        """Regression test for the reported bug: a model that answers the
        categorical `usecase` ask with a full sentence must land as a short
        label in the tag, not the sentence verbatim."""
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[
            {"resp": json.dumps({"description": "Sales schema.", "usecase": "Order analytics for the business."})}
        ])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_schema_item("model", sem, "main", "gold_orders", "sales_pillar",
                                             ["orders"], "data_product = sales")
        self.assertEqual(row["catalog"], "main")
        self.assertEqual(row["schema"], "gold_orders")
        self.assertEqual(row["description"], "Sales schema.")
        self.assertEqual(row["usecase"], "order_analytics_for_the")  # normalized: first 4 words, lower_snake_case
        self.assertEqual(row["quality_tier"], "Gold")  # from the "gold_" schema name
        self.assertEqual(row["tag"],
                         "data_product = sales, quality_tier = Gold, schema_usecase = order_analytics_for_the")

    async def test_schema_item_omits_catalog_pair_when_catalog_wasnt_generated(self):
        """If the parent catalog already had governance (so it never appears in
        the Catalog sheet), the schema's tag must not fabricate a catalog pair —
        it just carries its own quality_tier/usecase pairs."""
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "d", "usecase": "u"})}])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_schema_item("model", sem, "main", "bronze_raw", "p", [], "")
        self.assertEqual(row["tag"], "quality_tier = Bronze, schema_usecase = u")

    async def test_entity_generation_rolls_up_named_components_without_duplicating_data_product(self):
        """Regression test for the reported duplication: the entity's tag must
        carry data_product exactly ONCE, and the schema's usecase and the
        entity's own usecase must be distinguishable (schema_usecase vs
        entity_usecase) rather than two anonymous, indistinguishable `usecase`
        pairs."""
        sem = asyncio.Semaphore(2)
        payload = {"description": "Order facts.", "table_type": "fact", "pii": False,
                   "usecase": "customer", "columns": {"amount": "Order amount in USD."}}
        execute = AsyncMock(return_value=[{"resp": json.dumps(payload)}])
        with patch.object(gen, "execute_sql", execute):
            entity_row, column_rows = await gen._gen_entity(
                "model", sem, "main", "gold_orders", "fact_orders", "sales_pillar", ["amount"],
                "data_product = sales", "Gold", "region",
            )
        self.assertEqual(entity_row["entity_description"], "Order facts.")
        self.assertEqual(entity_row["entity_usecase"], "customer")
        self.assertEqual(entity_row["entity_tag"],
                         "data_product = sales, quality_tier = Gold, schema_usecase = region, "
                         "table_type = fact, pii = false, entity_usecase = customer")
        self.assertEqual(entity_row["entity_tag"].count("data_product"), 1)
        self.assertNotIn("column", entity_row)  # Entity sheet is entity-grain only
        self.assertEqual(column_rows, [{"catalog": "main", "schema": "gold_orders", "entity": "fact_orders",
                                        "column": "amount", "column_comment": "Order amount in USD."}])

    async def test_entity_omits_schema_pairs_when_schema_wasnt_generated(self):
        sem = asyncio.Semaphore(2)
        payload = {"description": "d", "table_type": "fact", "pii": False, "usecase": "customer", "columns": {}}
        execute = AsyncMock(return_value=[{"resp": json.dumps(payload)}])
        with patch.object(gen, "execute_sql", execute):
            entity_row, _ = await gen._gen_entity(
                "model", sem, "main", "s1", "orders", "p", [], "data_product = sales", "", ""
            )
        self.assertEqual(entity_row["entity_tag"],
                         "data_product = sales, table_type = fact, pii = false, entity_usecase = customer")

    async def test_entity_pii_true_when_column_name_heuristic_fires_even_if_model_says_false(self):
        """pii is an OR of the model's judgment and the column-name heuristic —
        a model that misses an obvious PII column must not suppress the flag."""
        sem = asyncio.Semaphore(2)
        payload = {"description": "d", "table_type": "dimension", "pii": False,
                   "usecase": "u", "columns": {}}
        execute = AsyncMock(return_value=[{"resp": json.dumps(payload)}])
        with patch.object(gen, "execute_sql", execute):
            entity_row, _ = await gen._gen_entity(
                "model", sem, "main", "s1", "customers", "p", ["email_address"], "", "", ""
            )
        self.assertIn("pii = true", entity_row["entity_tag"])

    async def test_entity_table_type_falls_back_to_heuristic_when_model_reply_invalid(self):
        payload = {"description": "d", "table_type": "not-a-real-type", "pii": False, "usecase": "u", "columns": {}}
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps(payload)}])
        with patch.object(gen, "execute_sql", execute):
            entity_row, _ = await gen._gen_entity(
                "model", sem, "main", "s1", "dim_customers", "p", [], "", "", ""
            )
        self.assertIn("table_type = dimension", entity_row["entity_tag"])  # from the dim_ prefix, not the model


class TaggingHeuristicsTest(unittest.TestCase):
    """The deterministic pieces that back-stop the LLM: quality_tier and
    table_type always have a value (never blank, unlike description/tag/usecase,
    which stay blank on a real generation failure), and pii is a safety-net
    OR-signal alongside the model's own judgment."""

    def test_quality_tier_from_medallion_naming(self):
        self.assertEqual(gen._quality_tier("main", "gold_orders"), "Gold")
        self.assertEqual(gen._quality_tier("main", "silver_orders"), "Silver")
        self.assertEqual(gen._quality_tier("main", "bronze_orders"), "Bronze")
        self.assertEqual(gen._quality_tier("main", "orders"), "Bronze")  # no signal -> conservative default

    def test_looks_like_pii_matches_common_pii_column_names(self):
        self.assertTrue(gen._looks_like_pii(["email_address", "order_id"]))
        self.assertTrue(gen._looks_like_pii(["date_of_birth"]))
        self.assertFalse(gen._looks_like_pii(["order_id", "amount", "status"]))

    def test_heuristic_table_type_prefix_wins_over_column_shape(self):
        self.assertEqual(gen._heuristic_table_type("dim_customers", ["customer_id", "amount"]), "dimension")
        self.assertEqual(gen._heuristic_table_type("fact_orders", []), "fact")
        self.assertEqual(gen._heuristic_table_type("mv_revenue", []), "metric")

    def test_heuristic_table_type_column_shape_fallback(self):
        # No naming prefix: several FK-like columns + a measure column reads as fact.
        self.assertEqual(
            gen._heuristic_table_type("orders", ["customer_id", "product_id", "order_amount"]), "fact"
        )
        self.assertEqual(gen._heuristic_table_type("customers", ["customer_id", "name"]), "dimension")

    def test_pick_bool_tolerates_string_and_case(self):
        self.assertIs(gen._pick_bool({"pii": True}, "pii"), True)
        self.assertIs(gen._pick_bool({"PII": "true"}, "pii"), True)
        self.assertIs(gen._pick_bool({"pii": "No"}, "pii"), False)
        self.assertIsNone(gen._pick_bool({}, "pii"))

    def test_compose_tag_drops_blank_pairs(self):
        self.assertEqual(gen._compose_tag("a = 1", "", "b = 2"), "a = 1, b = 2")
        self.assertEqual(gen._compose_tag("", ""), "")

    def test_normalize_tag_value_passes_through_a_real_category_label(self):
        self.assertEqual(gen._normalize_tag_value("customer"), "customer")
        self.assertEqual(gen._normalize_tag_value("date_dimension"), "date_dimension")
        self.assertEqual(gen._normalize_tag_value("Retail Metrics"), "retail_metrics")

    def test_normalize_tag_value_degrades_a_sentence_to_a_short_label(self):
        """The exact reported bug: usecase came back as a full description
        instead of a short category. Normalization can't recover the model's
        intent, but it guarantees the tag never carries a whole sentence."""
        value = gen._normalize_tag_value("Tracks customer orders and their full purchase history.")
        self.assertEqual(value, "tracks_customer_orders_and")  # first 4 words only
        self.assertNotIn(".", value)
        self.assertNotIn(" ", value)

    def test_normalize_tag_value_blank_or_missing_returns_empty(self):
        self.assertEqual(gen._normalize_tag_value(""), "")
        self.assertEqual(gen._normalize_tag_value(None), "")


class PickHelpersTest(unittest.TestCase):
    """The tolerant field lookups that stand in for a strict dict.get(): a model
    that capitalizes a key ("Description") or renames it ("desc") must not read as
    permanently blank."""

    def test_pick_str_matches_case_insensitively_and_trims(self):
        self.assertEqual(gen._pick_str({"Description": "  Hello.  "}, "description"), "Hello.")
        self.assertEqual(gen._pick_str({"tag": "sales"}, "tag", "governed_tag"), "sales")

    def test_pick_str_falls_through_alias_list(self):
        self.assertEqual(gen._pick_str({"governed_tag": "sales"}, "tag", "governed_tag"), "sales")

    def test_pick_str_blank_or_missing_returns_empty(self):
        self.assertEqual(gen._pick_str({}, "description"), "")
        self.assertEqual(gen._pick_str({"description": "   "}, "description"), "")

    def test_pick_dict_matches_case_insensitively(self):
        self.assertEqual(gen._pick_dict({"Columns": {"a": "x"}}, "columns"), {"a": "x"})
        self.assertEqual(gen._pick_dict({}, "columns"), {})

    def test_pick_column_comment_case_insensitive_column_name(self):
        self.assertEqual(gen._pick_column_comment({"AMOUNT": "The order total."}, "amount"),
                         "The order total.")
        self.assertEqual(gen._pick_column_comment({"amount": "  "}, "amount"), "")
        self.assertEqual(gen._pick_column_comment({}, "amount"), "")


class ComputeFailuresTest(unittest.TestCase):
    """The completion banner's "N items could not be generated" count — a row whose
    LLM-drafted field(s) came back blank is counted as a failure; a row that never
    ran (section absent) counts zero, not an error."""

    def test_counts_blank_rows_per_section(self):
        payload = {
            "catalog": [{"catalog": "a", "description": "d", "tag": "data_product = t"},
                       {"catalog": "b", "description": "", "tag": ""}],
            # Blank description+usecase is a real failure even though `tag` is
            # non-blank (quality_tier alone renders a pair) — the whole point of
            # checking usecase instead of the rendered tag.
            "schema": [{"schema": "s1", "description": "", "usecase": "", "tag": "quality_tier = Bronze"}],
            "entity": [{"entity": "orders", "entity_description": "d", "entity_usecase": "u",
                       "entity_tag": "table_type = fact, pii = false"}],
            "entity_columns": [{"column": "amount", "column_comment": ""},
                               {"column": "id", "column_comment": "The primary key."}],
            "genie_agent": [{"name": "Sales", "instructions": ""}],
            "metric_views": [],
        }
        failures = gen._compute_failures(payload)
        self.assertEqual(failures["catalog"], 1)
        self.assertEqual(failures["schema"], 1)
        self.assertEqual(failures["entity"], 0)
        self.assertEqual(failures["entity_columns"], 1)
        self.assertEqual(failures["genie_agent"], 1)
        self.assertEqual(failures["metric_views"], 0)

    def test_missing_sections_count_as_zero_not_error(self):
        self.assertEqual(gen._compute_failures({}), {
            "catalog": 0, "schema": 0, "entity": 0,
            "entity_columns": 0, "genie_agent": 0, "metric_views": 0,
        })


class MaterializationFilterTest(unittest.IsolatedAsyncioTestCase):
    """__materialization* tables are Lakeflow/DLT-internal materialization
    aliases, not user-facing tables — must never be generated for, or appear
    in, the Entity / Entity_Columns sheets."""

    async def test_materialization_tables_are_excluded_from_generation(self):
        detail = {
            "catalogs": [], "schemas": [],
            "entities": [
                {"catalog": "main", "schema": "gold", "entity": "fact_orders",
                 "status": "FAIL", "pillar": "p"},
                {"catalog": "main", "schema": "gold", "entity": "__materialization_mat_abc123",
                 "status": "FAIL", "pillar": "p"},
            ],
            "columns_failing": [], "relationships": [], "genie_agents": {"agents": []},
        }

        async def _execute(query, parameters=None, record=True):
            if "ai_query" in query:
                return [{"resp": json.dumps({"description": "d", "table_type": "fact",
                                             "pii": False, "usecase": "u", "columns": {}})}]
            return []  # the _entity_columns query — no columns needed for this test

        async def _noop_emit(stage, done, total):
            pass

        with patch.object(gen, "execute_sql", AsyncMock(side_effect=_execute)):
            payload = await gen._generate(detail, {"system_ok": True, "catalogs": []}, "model", _noop_emit)

        entities = [r["entity"] for r in payload["entity"]]
        self.assertIn("fact_orders", entities)
        self.assertNotIn("__materialization_mat_abc123", entities)
        self.assertFalse(any(r["entity"] == "__materialization_mat_abc123" for r in payload["entity_columns"]))


if __name__ == "__main__":
    unittest.main()
