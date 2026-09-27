"""The Excel builders must emit the exact tabs/columns the spec (and the frontend)
depend on. These open the produced workbook back with openpyxl and assert the
sheet names and header rows — no Databricks/live deps needed."""

from openpyxl import load_workbook

from server.excel import build_assessment_workbook, build_generation_workbook


def _headers(wb, sheet):
    return [c.value for c in wb[sheet][1]]


def test_assessment_workbook_has_all_sections():
    detail = {
        "summary": {"catalogs": {"total": 1, "failing": 1}},
        "catalogs": [{"pillar": "sales", "catalog": "main", "description_present": False,
                      "tags_present": False, "status": "FAIL"}],
        "schemas": [{"pillar": "sales", "catalog": "main", "schema": "s1",
                     "description_present": True, "tags_present": False, "status": "FAIL"}],
        "entities": [{"pillar": "sales", "catalog": "main", "schema": "s1", "entity": "orders",
                      "table_type": "MANAGED", "description_present": False, "tags_present": False,
                      "columns": 5, "commented_columns": 2, "column_comment_pct": 40.0, "status": "FAIL"}],
        "columns_failing": [{"pillar": "sales", "catalog": "main", "schema": "s1",
                             "entity": "orders", "column": "amount", "status": "FAIL"}],
        "relationships": [{"pillar": "sales", "catalog": "main", "schema": "s1", "entity": "orders",
                           "has_pk": False, "has_fk": False, "pk_columns": "", "status": "FAIL"}],
        "metric_views": [{"pillar": "sales", "catalog": "main", "schema": "s1", "entity": "mv_rev",
                          "description_present": False, "status": "FAIL"}],
        "genie_agents": {"agents": [{"name": "Sales", "space_id": "abc", "curation_readable": True,
                                     "instructions": 0, "example_sqls": 0, "benchmarks": 0,
                                     "sample_questions": 1, "functions": 0, "tables": 3,
                                     "missing": ["instructions", "example SQL", "benchmarks"], "status": "FAIL"}]},
        "noncertified": [{"pillar": "sales", "catalog": "main", "schema": "s1", "entity": "orders",
                          "certified": False, "status": "FAIL"}],
        "pages": {"assessable": False, "note": "Pages have no public list API."},
    }
    wb = load_workbook(build_assessment_workbook(detail, {"overall": {"score": 42, "readiness_stage": "Foundational"}}))
    for tab in ("Summary", "Catalogs", "Schemas", "Entities", "Columns (uncommented)",
                "Relationships", "MetricViews", "GenieAgents", "NonCertified", "Domains & Pages"):
        assert tab in wb.sheetnames, f"missing tab {tab}"
    assert _headers(wb, "Entities")[:5] == ["Pillar", "Catalog", "Schema", "Entity", "Type"]
    # FAIL is colored (font set) — a smoke check that status styling ran.
    assert wb["Catalogs"]["E2"].value == "FAIL"


def test_generation_workbook_exact_spec():
    # No "pillar" key anywhere below — Catalog/Schema/Entity dropped the Pillar
    # column entirely from the generation workbook (assessment report still has
    # it; that's a separate, unrelated sheet builder).
    payload = {
        "catalog": [{"catalog": "main", "description": "d", "tag": "data_product = sales"}],
        "schema": [{"catalog": "main", "schema": "s1", "description": "d",
                   "tag": "data_product = sales, quality_tier = Gold, schema_usecase = u"}],
        "entity": [{"catalog": "main", "schema": "s1", "entity": "orders",
                    "entity_description": "d",
                    "entity_tag": "data_product = sales, quality_tier = Gold, schema_usecase = u, "
                                  "table_type = fact, pii = false, entity_usecase = u2"}],
        "entity_columns": [{"catalog": "main", "schema": "s1", "entity": "orders",
                            "column": "amount", "column_comment": "c"}],
        "relationship_pk": [{"catalog": "main", "schema": "s1", "parent_entity": "orders",
                             "column_name": "order_id", "statement": "ALTER TABLE ..."}],
        "relationship_fk": [{"catalog": "main", "schema": "s1", "parent_entity": "customers",
                             "column_name": "customer_id", "foreign_key": "orders.customer_id",
                             "primary_key_column": "customer_id", "foreign_key_column": "customer_id",
                             "statement": "ALTER TABLE ... NOT ENFORCED RELY"}],
        "genie_agent": [{"name": "Sales", "space_id": "abc", "instructions": "..."}],
        "metric_views": [{"catalog": "main", "schema": "s1", "text": "CREATE VIEW ..."}],
    }
    wb = load_workbook(build_generation_workbook(payload))
    for tab in ("Catalog", "Schema", "Entity", "Entity_Columns", "Relationship_PrimaryKey",
                "Relationship_ForeignKey", "GenieAgent", "MetricViews"):
        assert tab in wb.sheetnames, f"missing tab {tab}"
    assert _headers(wb, "Catalog") == ["Catalog", "Catalog_Description_Generated", "Catalog_Tag_Generated"]
    assert _headers(wb, "Schema") == ["Catalog", "Schema", "Schema_Description_Generated", "Schema_Tag_Generated"]
    # Entity is entity-grain only — no Column / Column_Comment_Generated / Pillar here.
    assert _headers(wb, "Entity") == ["Catalog", "Schema", "Entity",
                                      "Entity_Description_Generated", "Entity_Tag_Generated"]
    assert _headers(wb, "Entity_Columns") == ["Catalog", "Schema", "Entity", "Column", "Column_Comments_Generated"]
    assert _headers(wb, "Relationship_PrimaryKey") == ["Catalog", "Schema", "Parent_Entity", "Column_Name",
                                                       "Constraint_Type", "Statement"]
    assert _headers(wb, "Relationship_ForeignKey") == ["Catalog", "Schema", "Parent_Entity", "Column_Name",
                                                       "Foreign_Key", "Primary_Key_Column", "Foreign_key_Column",
                                                       "Constraint_Type", "Statement"]
    assert _headers(wb, "GenieAgent") == ["GenieAgent_Name", "GenieAgentID", "Generated_Instructions"]
    assert _headers(wb, "MetricViews") == ["Catalog", "Schema", "Text"]
    # Constraint_Type constants are stamped by the builder, not the payload.
    assert wb["Relationship_PrimaryKey"]["E2"].value == "PrimaryKey"
    assert wb["Relationship_ForeignKey"]["H2"].value == "ForeignKey"
    # The generated description/tag/comment actually landed, in the key=value
    # tag format (regression guard for the per-item generation fix — a batched,
    # name-echo-matched call used to blank every row silently).
    assert wb["Catalog"]["B2"].value == "d" and wb["Catalog"]["C2"].value == "data_product = sales"
    assert wb["Schema"]["C2"].value == "d"
    assert wb["Schema"]["D2"].value == "data_product = sales, quality_tier = Gold, schema_usecase = u"
    assert wb["Entity"]["D2"].value == "d"
    assert wb["Entity"]["E2"].value == ("data_product = sales, quality_tier = Gold, schema_usecase = u, "
                                        "table_type = fact, pii = false, entity_usecase = u2")
    assert wb["Entity_Columns"]["E2"].value == "c"
