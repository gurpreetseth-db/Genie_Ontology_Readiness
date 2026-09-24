"""Pure-function tests for the deterministic PK/FK suggestion heuristics that back
the generation workbook's Relationship tabs (no LLM / live deps)."""

from server.routes.generate import _pk_column, _fk_links


def test_pk_column_preference_order():
    assert _pk_column("customers", ["id", "name"]) == "id"
    assert _pk_column("customers", ["customer_id", "name"]) == "customer_id"
    assert _pk_column("orders", ["order_key", "x"]) == "order_key"
    # A plain *_id even when it doesn't match the table name.
    assert _pk_column("t", ["created_by_id", "x"]) == "created_by_id"
    # No key-shaped column → no suggestion.
    assert _pk_column("t", ["a", "b"]) is None


def test_fk_links_match_parent_and_emit_rely_ddl():
    names = {"customers": "customers", "orders": "orders"}
    links = _fk_links("cat", "sch", "orders", ["order_id", "customer_id"], names)
    # customer_id resolves to the customers entity; order_id is orders' own key (skipped).
    assert any(l["parent_entity"] == "customers" and l["foreign_key_column"] == "customer_id" for l in links)
    assert all("NOT ENFORCED RELY" in l["statement"] for l in links)
    assert all(l["catalog"] == "cat" and l["schema"] == "sch" for l in links)


def test_fk_links_ignore_unknown_parents():
    links = _fk_links("cat", "sch", "orders", ["widget_id"], {"orders": "orders"})
    assert links == []
