# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify`` and ``fluid diff`` read a nested BigQuery column as apply wrote it.

BigQuery reports an array as its element's type with mode REPEATED, a struct as a
RECORD, and a map (held as key and value records) as a REPEATED RECORD. verify
compared each column with the declared type name and NULLABLE or REQUIRED, so every
array the emitter wrote would have read as drift in both its type and its mode.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.build_runners import _bigquery_load
from fluid_build.cli._diff_live import _bq_contract_key
from fluid_build.cli.verify import verify_bigquery_table

pytestmark = [pytest.mark.unit, pytest.mark.gcp]

_DECLARED = [
    {"name": "id", "type": "string", "required": True},
    {
        "name": "tags",
        "type": "array",
        "required": True,
        "element": {"type": "string", "required": True},
    },
    {"name": "address", "type": "struct", "fields": [{"name": "zip", "type": "string"}]},
    {
        "name": "items",
        "type": "array",
        "required": True,
        "element": {
            "type": "struct",
            "required": True,
            "fields": [{"name": "sku", "type": "string"}],
        },
    },
    {
        "name": "attrs",
        "type": "map",
        "required": True,
        "key": {"type": "string", "required": True},
        "value": {"type": "integer"},
    },
]

#: What BigQuery returns for the table apply created from ``_DECLARED``.
_LIVE = [
    SimpleNamespace(name="id", field_type="STRING", mode="REQUIRED"),
    SimpleNamespace(name="tags", field_type="STRING", mode="REPEATED"),
    SimpleNamespace(name="address", field_type="RECORD", mode="NULLABLE"),
    SimpleNamespace(name="items", field_type="RECORD", mode="REPEATED"),
    SimpleNamespace(name="attrs", field_type="RECORD", mode="REPEATED"),
]


@pytest.fixture
def live_table(monkeypatch):
    def install(schema):
        class Client:
            def __init__(self, project=None, credentials=None):
                self.project = project

            def get_table(self, table_id):
                return SimpleNamespace(schema=schema, num_rows=0, created=None, modified=None)

            def get_dataset(self, ref):
                return SimpleNamespace(location="EU")

        monkeypatch.setattr(
            _bigquery_load, "_bigquery_module", lambda: SimpleNamespace(Client=Client)
        )

    return install


def test_verify_matches_nested_columns_with_the_types_and_modes_apply_wrote(live_table):
    live_table(_LIVE)
    result = verify_bigquery_table("acme", "demo", "orders", _DECLARED, "EU")
    dims = result["dimensions"]
    assert dims["types"] == {"status": "pass", "mismatches": []}
    assert dims["constraints"] == {"status": "pass", "mismatches": []}
    assert result["status"] == "match"


def test_verify_still_reports_an_array_bigquery_holds_as_a_scalar(live_table):
    """A column created NULLABLE STRING where the contract declares an array is drift."""
    live = list(_LIVE)
    live[1] = SimpleNamespace(name="tags", field_type="STRING", mode="NULLABLE")
    live_table(live)
    result = verify_bigquery_table("acme", "demo", "orders", _DECLARED, "EU")
    assert result["dimensions"]["constraints"]["mismatches"] == [
        {"field": "tags", "expected": "repeated", "actual": "nullable"}
    ]


def test_diff_compares_neither_an_array_nor_a_map_by_type_name():
    for declared in ("array", "ARRAY<string>", "array(string)", "map", "map<string,int>"):
        assert _bq_contract_key(declared) is None, declared
    assert _bq_contract_key("varchar") == "string"


@pytest.mark.parametrize("spelling", ["struct", "record", "row", "ROW", "Struct<a int>"])
def test_diff_reads_every_spelling_of_a_struct_as_the_record_bigquery_reports(spelling):
    """A ``row`` column is written as a RECORD; reading it as ``row`` was drift on a
    table apply had just created."""
    assert _bq_contract_key(spelling) == "record"
