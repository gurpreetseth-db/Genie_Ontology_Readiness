"""LLM-call plumbing for the Generate tab: JSON extraction, and the per-item
catalog/schema/entity generators that replaced the batched, name-echo-matched
design. That earlier design asked the model to echo an exact name back in a JSON
array and matched results by that string — any model-side rewording (case,
whitespace, dropping a `catalog.` prefix) made the lookup miss for EVERY row,
which is exactly the "every generated field is blank" bug this guards against.
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from server.routes import generate as gen


def _sse(content: str) -> str:
    """One `data: {"content": ...}` SSE line, matching _stream_from_fmapi's shape."""
    return f"data: {json.dumps({'content': content})}\n\n"


async def _stream_ok(*_a, **_k):
    """Mimic stream_llm_chat's SSE shape for one clean, unfenced JSON reply."""
    yield _sse(json.dumps({"description": "Sales data.", "tag": "sales"}))
    yield "data: [DONE]\n\n"


async def _stream_fenced(*_a, **_k):
    """A reply wrapped in a ```json code fence, split across multiple deltas —
    the shape a real model commonly returns despite "STRICT JSON only"."""
    body = json.dumps({"description": "Order facts.", "tag": "orders"})
    yield _sse("```json\n")
    yield _sse(body[: len(body) // 2])
    yield _sse(body[len(body) // 2 :])
    yield _sse("\n```")
    yield "data: [DONE]\n\n"


async def _stream_error(*_a, **_k):
    yield f"data: {json.dumps({'error': 'model unavailable'})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_entity(*_a, **_k):
    payload = {"description": "Order facts.", "tag": "orders",
               "columns": {"amount": "Order amount in USD."}}
    yield _sse(json.dumps(payload))
    yield "data: [DONE]\n\n"


class LlmJsonTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_plain_object(self):
        with patch.object(gen, "stream_llm_chat", _stream_ok):
            result = await gen._llm_json("model", "sys", "user")
        self.assertEqual(result, {"description": "Sales data.", "tag": "sales"})

    async def test_strips_code_fence_across_chunks(self):
        with patch.object(gen, "stream_llm_chat", _stream_fenced):
            result = await gen._llm_json("model", "sys", "user")
        self.assertEqual(result, {"description": "Order facts.", "tag": "orders"})

    async def test_returns_none_on_error_event(self):
        with patch.object(gen, "stream_llm_chat", _stream_error):
            result = await gen._llm_json("model", "sys", "user")
        self.assertIsNone(result)


class PerItemGenerationTest(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the actual reported bug."""

    async def test_catalog_item_does_not_depend_on_name_echo(self):
        sem = asyncio.Semaphore(2)
        with patch.object(gen, "stream_llm_chat", _stream_ok):
            row = await gen._gen_catalog_item("model", sem, "main", "sales_pillar", ["s1", "s2"])
        self.assertEqual(row, {"pillar": "sales_pillar", "catalog": "main",
                               "description": "Sales data.", "tag": "sales"})

    async def test_schema_item_does_not_depend_on_name_echo(self):
        sem = asyncio.Semaphore(2)
        with patch.object(gen, "stream_llm_chat", _stream_ok):
            row = await gen._gen_schema_item("model", sem, "main", "s1", "sales_pillar", ["orders"])
        self.assertEqual(row, {"pillar": "sales_pillar", "catalog": "main", "schema": "s1",
                               "description": "Sales data.", "tag": "sales"})

    async def test_entity_generation_splits_entity_and_column_rows(self):
        sem = asyncio.Semaphore(2)
        with patch.object(gen, "stream_llm_chat", _stream_entity):
            entity_row, column_rows = await gen._gen_entity(
                "model", sem, "main", "s1", "orders", "sales_pillar", ["amount"]
            )
        self.assertEqual(entity_row["entity_description"], "Order facts.")
        self.assertEqual(entity_row["entity_tag"], "orders")
        self.assertNotIn("column", entity_row)  # Entity sheet is entity-grain only
        self.assertEqual(column_rows, [{"catalog": "main", "schema": "s1", "entity": "orders",
                                        "column": "amount", "column_comment": "Order amount in USD."}])


if __name__ == "__main__":
    unittest.main()
