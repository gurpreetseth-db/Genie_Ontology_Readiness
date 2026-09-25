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
    generation must never depend on the model echoing a name back."""

    async def test_catalog_item_does_not_depend_on_name_echo(self):
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "Sales data.", "tag": "sales"})}])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_catalog_item("model", sem, "main", "sales_pillar", ["s1", "s2"])
        self.assertEqual(row, {"pillar": "sales_pillar", "catalog": "main",
                               "description": "Sales data.", "tag": "sales"})

    async def test_schema_item_does_not_depend_on_name_echo(self):
        sem = asyncio.Semaphore(2)
        execute = AsyncMock(return_value=[{"resp": json.dumps({"description": "Sales data.", "tag": "sales"})}])
        with patch.object(gen, "execute_sql", execute):
            row = await gen._gen_schema_item("model", sem, "main", "s1", "sales_pillar", ["orders"])
        self.assertEqual(row, {"pillar": "sales_pillar", "catalog": "main", "schema": "s1",
                               "description": "Sales data.", "tag": "sales"})

    async def test_entity_generation_splits_entity_and_column_rows(self):
        sem = asyncio.Semaphore(2)
        payload = {"description": "Order facts.", "tag": "orders",
                   "columns": {"amount": "Order amount in USD."}}
        execute = AsyncMock(return_value=[{"resp": json.dumps(payload)}])
        with patch.object(gen, "execute_sql", execute):
            entity_row, column_rows = await gen._gen_entity(
                "model", sem, "main", "s1", "orders", "sales_pillar", ["amount"]
            )
        self.assertEqual(entity_row["entity_description"], "Order facts.")
        self.assertEqual(entity_row["entity_tag"], "orders")
        self.assertNotIn("column", entity_row)  # Entity sheet is entity-grain only
        self.assertEqual(column_rows, [{"catalog": "main", "schema": "s1", "entity": "orders",
                                        "column": "amount", "column_comment": "Order amount in USD."}])


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
            "catalog": [{"catalog": "a", "description": "d", "tag": "t"},
                       {"catalog": "b", "description": "", "tag": ""}],
            "schema": [{"schema": "s1", "description": "", "tag": ""}],
            "entity": [{"entity": "orders", "entity_description": "d", "entity_tag": "t"}],
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


if __name__ == "__main__":
    unittest.main()
