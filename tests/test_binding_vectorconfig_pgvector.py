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

"""Regression: v0.7.5 declares the pgvector vector-output-port binding fields.

The vector output port (``fluid_build.output_ports.vector``) reads
``binding.platform: pgvector`` + ``binding.vectorConfig`` to emit the
embeddings-table + ANN-index DDL. This pins that the schema declares those
additively — the ``pgvector`` platform / ``pgvector_table`` format enum
members and the ``vectorConfig`` object with its ``dimensions`` requirement —
and that ``additionalProperties: false`` stays enforced (unknown vectorConfig
keys are still rejected, so the change is additive only).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_075 = REPO / "fluid_build" / "schemas" / "fluid-schema-0.7.5.json"


def _binding_props() -> dict:
    return json.loads(SCHEMA_075.read_text())["$defs"]["binding"]["properties"]


def test_platform_and_format_enums_declare_pgvector() -> None:
    props = _binding_props()
    assert "pgvector" in props["platform"]["enum"]
    assert "pgvector_table" in props["format"]["enum"]


def test_vectorconfig_declared_with_required_dimensions() -> None:
    props = _binding_props()
    assert "vectorConfig" in props, "binding (0.7.5) is missing vectorConfig"
    vc = props["vectorConfig"]
    assert vc["required"] == ["dimensions"]
    assert vc["additionalProperties"] is False
    for field in ("dimensions", "embeddingModel", "indexType", "distanceMetric", "table"):
        assert field in vc["properties"], f"vectorConfig missing {field}"


def _contract(vector_config: dict) -> dict:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "test.rag.vectors",
        "name": "pgvector binding test",
        "metadata": {"layer": "Gold", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [
            {
                "exposeId": "e",
                "kind": "vector",
                "binding": {
                    "platform": "pgvector",
                    "format": "pgvector_table",
                    "location": {"database": "rag"},
                    "vectorConfig": vector_config,
                },
                "contract": {
                    "schema": [
                        {
                            "name": "body",
                            "type": "text",
                            "labels": {"ai-embeddable": "true"},
                        }
                    ]
                },
            }
        ],
    }


def test_pgvector_binding_validates() -> None:
    contract = _contract(
        {
            "dimensions": 1536,
            "embeddingModel": "text-embedding-3-small",
            "indexType": "hnsw",
            "distanceMetric": "cosine",
            "sourceKeyColumn": "id",
            "hnsw": {"m": 16, "efConstruction": 64},
        }
    )
    result = FluidSchemaManager().validate_contract(contract, "0.7.5", offline_only=True)
    assert result.is_valid, result.errors


def test_unknown_vectorconfig_field_still_rejected() -> None:
    """``additionalProperties: false`` still enforced — the change is additive only."""
    result = FluidSchemaManager().validate_contract(
        _contract({"dimensions": 8, "totally_bogus_field_xyz": "x"}), "0.7.5", offline_only=True
    )
    assert not result.is_valid


def test_example_contract_validates() -> None:
    import yaml

    contract = yaml.safe_load(
        (REPO / "examples" / "pgvector-rag-output-port" / "contract.fluid.yaml").read_text()
    )
    result = FluidSchemaManager().validate_contract(contract, "0.7.5", offline_only=True)
    assert result.is_valid, result.errors
