# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The BigQuery IaC must emit types BigQuery accepts, in the project the binding names.

Two defects, found applying a Postgres-sourced contract to real GCP. A column
typed ``varchar`` was emitted as ``"type": "VARCHAR"``, which is not a BigQuery
type, because any spelling missing from ``_BQ_TYPES`` was upper-cased verbatim.
And ``binding.location.project`` was ignored, so the project came only from the
ambient environment and a binding for one project could provision into another.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.cli.verify import _bq_canonical_type
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.providers.gcp import _BQ_TYPES, _bq_type

pytestmark = [pytest.mark.unit, pytest.mark.provider]

# Every type the BigQuery schema JSON accepts, standard and legacy spellings.
_VALID = {
    "STRING", "BYTES", "INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC",
    "BOOLEAN", "BOOL", "TIMESTAMP", "DATE", "TIME", "DATETIME", "GEOGRAPHY", "JSON",
    "RECORD", "STRUCT", "INTERVAL", "RANGE",
}  # fmt: skip


@pytest.mark.parametrize(
    "source_type, expected",
    [
        ("varchar", "STRING"),
        ("VARCHAR(255)", "STRING"),
        ("char", "STRING"),
        ("uuid", "STRING"),
        ("smallint", "INT64"),
        ("timestamptz", "TIMESTAMP"),
        ("blob", "BYTES"),
        ("real", "FLOAT64"),
    ],
)
def test_source_native_spellings_become_bigquery_types(source_type, expected):
    assert _bq_type(source_type) == expected
    assert _bq_type(source_type) in _VALID


@pytest.mark.parametrize("fluid_type", sorted(_BQ_TYPES))
def test_every_type_that_already_mapped_is_unchanged(fluid_type):
    """The fallback runs only on a miss, so no existing emit changes."""
    assert _bq_type(fluid_type) == _BQ_TYPES[fluid_type]


def test_an_unknown_type_is_still_passed_through():
    assert _bq_type("geometryz") == "GEOMETRYZ"


def _contract(location):
    return {
        "id": "bronze.orders",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "gcp", "format": "bigquery_table", "location": location},
                "contract": {"schema": [{"name": "id", "type": "varchar", "required": True}]},
            }
        ],
    }


def test_the_bindings_project_is_on_the_dataset_and_the_table():
    res = get_iac_plugin("gcp").emit(
        _contract({"project": "acme-eu", "dataset": "bronze", "table": "orders"})
    )
    ds = next(iter(res["google_bigquery_dataset"].values()))
    tbl = next(iter(res["google_bigquery_table"].values()))
    assert ds["project"] == "acme-eu"
    assert tbl["project"] == "acme-eu"
    assert json.loads(tbl["schema"])[0]["type"] == "STRING"


def test_no_project_key_when_the_binding_names_none():
    """Without one the emit is what it was, and the provider reads the env."""
    res = get_iac_plugin("gcp").emit(_contract({"dataset": "bronze", "table": "orders"}))
    assert "project" not in next(iter(res["google_bigquery_dataset"].values()))
    assert "project" not in next(iter(res["google_bigquery_table"].values()))


def test_import_ids_name_the_bindings_project_over_the_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_PROJECT", "ambient")
    blocks = get_iac_plugin("gcp").discover_imports(
        _contract({"project": "acme-eu", "dataset": "bronze", "table": "orders"})
    )
    ids = sorted(b.id for b in blocks)
    assert ids == [
        "projects/acme-eu/datasets/bronze",
        "projects/acme-eu/datasets/bronze/tables/orders",
    ]


@pytest.mark.parametrize(
    "declared, reported",
    [("INT64", "INTEGER"), ("FLOAT64", "FLOAT"), ("BOOL", "BOOLEAN"), ("STRING", "STRING")],
)
def test_verify_compares_a_type_not_its_spelling(declared, reported):
    """BigQuery answers get_table with the legacy name of what the IaC created."""
    assert _bq_canonical_type(declared) == _bq_canonical_type(reported)


def test_verify_expects_the_type_the_iac_emitted_for_varchar():
    assert _bq_canonical_type(_bq_type("varchar")) == _bq_canonical_type("STRING")


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("double precision", "FLOAT64"),
        ("timestamp with time zone", "TIMESTAMP"),
        ("Timestamp  Without  Time Zone", "TIMESTAMP"),
    ],
)
def test_the_multi_word_spellings_the_schema_accepts_map_too(spelling, expected):
    """The contract schema's type pattern admits these; they were upper-cased
    into "DOUBLE PRECISION" and "TIMESTAMP WITH TIME ZONE", which BigQuery
    rejects."""
    assert _bq_type(spelling) == expected


def test_a_shared_datasets_lookup_names_the_bindings_project():
    """Otherwise the data source reads the provider's default project while the
    table is created in the binding's."""
    contract = {
        "id": "orders-adp",
        "packaging": {"mode": "shared", "pool": "acme-pool"},
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"project": "tenant-a", "dataset": "sales_pool", "table": "orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    data = get_iac_plugin("gcp").emit_data(contract)
    (lookup,) = data["google_bigquery_dataset"].values()
    assert lookup == {"dataset_id": "sales_pool", "project": "tenant-a"}
