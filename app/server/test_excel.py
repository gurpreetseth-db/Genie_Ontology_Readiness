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
        # Ignored — metric-view definitions are no longer part of the generation workbook.
        "metric_views": [{"catalog": "main", "schema": "s1", "text": "CREATE VIEW ..."}],
    }
    wb = load_workbook(build_generation_workbook(payload))
    for tab in ("Catalog", "Schema", "Entity", "Entity_Columns", "Relationship_PrimaryKey",
                "Relationship_ForeignKey", "GenieAgent"):
        assert tab in wb.sheetnames, f"missing tab {tab}"
    assert "MetricViews" not in wb.sheetnames
    assert _headers(wb, "Catalog") == ["Catalog", "Catalog_Description_Generated", "Catalog_Tag_Generated",
                                       "Catalog_Description_Command", "Catalog_Tag_Command"]
    assert _headers(wb, "Schema") == ["Catalog", "Schema", "Schema_Description_Generated", "Schema_Tag_Generated",
                                      "Schema_Description_Command", "Schema_Tag_Command"]
    # Entity is entity-grain only — no Column / Column_Comment_Generated / Pillar here.
    assert _headers(wb, "Entity") == ["Catalog", "Schema", "Entity",
                                      "Entity_Description_Generated", "Entity_Tag_Generated",
                                      "Entity_Description_Command", "Entity_Tag_Command"]
    assert _headers(wb, "Entity_Columns") == ["Catalog", "Schema", "Entity", "Column", "Column_Comments_Generated",
                                              "Column_Comment_Command"]
    assert _headers(wb, "Relationship_PrimaryKey") == ["Catalog", "Schema", "Parent_Entity", "Column_Name",
                                                       "Constraint_Type", "Statement"]
    assert _headers(wb, "Relationship_ForeignKey") == ["Catalog", "Schema", "Parent_Entity", "Column_Name",
                                                       "Foreign_Key", "Primary_Key_Column", "Foreign_key_Column",
                                                       "Constraint_Type", "Statement"]
    assert _headers(wb, "GenieAgent") == ["GenieAgent_Name", "GenieAgentID", "Generated_Instructions"]
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
    # Ready-to-run SQL beside each generated value.
    assert wb["Catalog"]["D2"].value == "COMMENT ON CATALOG main IS 'd'"
    assert wb["Catalog"]["E2"].value == "SET TAG ON CATALOG main `data_product` = `sales`"
    assert wb["Schema"]["E2"].value == "COMMENT ON SCHEMA main.s1 IS 'd'"
    assert wb["Schema"]["F2"].value == ("ALTER SCHEMA main.s1 SET TAGS ('data_product' = 'sales', "
                                        "'quality_tier' = 'Gold', 'schema_usecase' = 'u')")
    assert wb["Entity"]["F2"].value == "COMMENT ON TABLE main.s1.orders IS 'd'"
    assert wb["Entity"]["G2"].value == ("ALTER TABLE main.s1.orders SET TAGS ('data_product' = 'sales', "
                                        "'quality_tier' = 'Gold', 'schema_usecase' = 'u', 'table_type' = 'fact', "
                                        "'pii' = 'false', 'entity_usecase' = 'u2')")
    assert wb["Entity_Columns"]["F2"].value == "ALTER TABLE main.s1.orders ALTER COLUMN amount COMMENT 'c'"


def test_generation_commands_escape_and_skip_blanks():
    payload = {
        "catalog": [{"catalog": "main", "description": "", "tag": ""},
                    {"catalog": "dev-sales", "description": "d", "tag": "data_product = sales"}],
        "schema": [{"catalog": "main", "schema": "s1", "description": "Customer's orders", "tag": ""}],
        "entity_columns": [{"catalog": "main", "schema": "s1", "entity": "orders",
                            "column": "order date", "column_comment": "Path C:\\x"}],
    }
    wb = load_workbook(build_generation_workbook(payload))
    # Nothing generated -> no command (not a COMMENT ... IS '' that would wipe a real value).
    assert wb["Catalog"]["D2"].value in (None, "") and wb["Catalog"]["E2"].value in (None, "")
    # Hyphenated names are backtick-quoted.
    assert wb["Catalog"]["D3"].value == "COMMENT ON CATALOG `dev-sales` IS 'd'"
    assert wb["Catalog"]["E3"].value == "SET TAG ON CATALOG `dev-sales` `data_product` = `sales`"
    assert wb["Schema"]["E2"].value == "COMMENT ON SCHEMA main.s1 IS 'Customer\\'s orders'"
    assert wb["Schema"]["F2"].value in (None, "")
    assert wb["Entity_Columns"]["F2"].value == ("ALTER TABLE main.s1.orders ALTER COLUMN `order date` "
                                                "COMMENT 'Path C:\\\\x'")
