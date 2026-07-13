# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the pgvector vector / embeddings output port.

Pins the pure emit surface in :mod:`fluid_build.output_ports.vector`:

* it CONSUMES the ``ai-embeddable`` column labels the ai_ready agent
  (PR #405) stamps — only labelled columns become embedding targets;
* it emits pgvector DDL (extension + embeddings table + ANN index) that
  routes every identifier through the central SQL-safety helpers; and
* it emits a RAG provenance manifest with the schema/metadata a
  retriever needs.
"""

from __future__ import annotations

import pytest

from fluid_build.output_ports.vector import (
    build_rag_manifest,
    compile_pgvector_ddl,
    compile_vector_port,
    iter_vector_exposes,
    validate_vector_binding,
)


def _contract(**overrides):
    """A minimal pgvector-bound contract with two ai-embeddable columns."""
    vector_config = {
        "dimensions": 1536,
        "embeddingModel": "text-embedding-3-small",
        "distanceMetric": "cosine",
        "indexType": "hnsw",
        "sourceKeyColumn": "customer_id",
    }
    vector_config.update(overrides.pop("vectorConfig", {}))
    contract = {
        "id": "acme.customers",
        "name": "customers",
        "exposes": [
            {
                "exposeId": "customer_profiles",
                "kind": "vector",
                "binding": {
                    "platform": "pgvector",
                    "format": "pgvector_table",
                    "location": {"database": "rag", "schema": "public"},
                    "vectorConfig": vector_config,
                },
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "bigint"},
                        {
                            "name": "bio",
                            "type": "text",
                            "description": "Free-text customer bio.",
                            "labels": {"ai-embeddable": "true"},
                        },
                        {
                            "name": "notes",
                            "type": "text",
                            "description": "Support notes.",
                            "labels": {"ai-embeddable": "true"},
                        },
                        # NOT ai-embeddable — must be ignored by the port.
                        {"name": "internal_flag", "type": "text"},
                    ]
                },
            }
        ],
    }
    contract.update(overrides)
    return contract


# --------------------------------------------------------------------------- #
# iter_vector_exposes
# --------------------------------------------------------------------------- #
def test_iter_only_pgvector_exposes():
    contract = _contract()
    contract["exposes"].append({"exposeId": "warehouse", "binding": {"platform": "snowflake"}})
    got = list(iter_vector_exposes(contract))
    assert len(got) == 1
    expose, _binding, vcfg = got[0]
    assert expose["exposeId"] == "customer_profiles"
    assert vcfg["dimensions"] == 1536


def test_non_vector_contract_emits_nothing():
    contract = {"id": "x", "exposes": [{"exposeId": "t", "binding": {"platform": "gcp"}}]}
    assert compile_pgvector_ddl(contract).strip() == ""
    assert build_rag_manifest(contract)["targets"] == []
    assert compile_vector_port(contract).targets == []


# --------------------------------------------------------------------------- #
# DDL emit
# --------------------------------------------------------------------------- #
def test_ddl_emits_extension_table_and_index():
    ddl = compile_pgvector_ddl(_contract())
    assert "CREATE EXTENSION IF NOT EXISTS vector" in ddl
    # Embeddings table for the expose, default name <exposeId>_embeddings.
    assert "customer_profiles_embeddings" in ddl
    assert "embedding vector(1536)" in ddl
    # RAG provenance columns a retriever needs.
    assert "source_key" in ddl
    assert "source_column" in ddl
    assert "content" in ddl
    assert "embedding_model" in ddl
    # HNSW ANN index with cosine ops + tuning defaults.
    assert "USING hnsw (embedding vector_cosine_ops)" in ddl
    assert "m = 16" in ddl
    assert "ef_construction = 64" in ddl


def test_ddl_ivfflat_l2_with_lists():
    ddl = compile_pgvector_ddl(
        _contract(
            vectorConfig={"indexType": "ivfflat", "distanceMetric": "l2", "ivfflat": {"lists": 100}}
        )
    )
    assert "USING ivfflat (embedding vector_l2_ops)" in ddl
    assert "lists = 100" in ddl


def test_ddl_halfvec_and_inner_product():
    ddl = compile_pgvector_ddl(
        _contract(vectorConfig={"vectorType": "halfvec", "distanceMetric": "inner_product"})
    )
    assert "embedding halfvec(1536)" in ddl
    assert "halfvec_ip_ops" in ddl


def test_ddl_index_none_skips_index():
    ddl = compile_pgvector_ddl(_contract(vectorConfig={"indexType": "none"}))
    assert "CREATE TABLE" in ddl
    assert "CREATE INDEX" not in ddl


def test_ddl_custom_table_name_honoured():
    ddl = compile_pgvector_ddl(_contract(vectorConfig={"table": "cust_vectors"}))
    assert "cust_vectors" in ddl
    assert "customer_profiles_embeddings" not in ddl


def test_ddl_rejects_injection_in_table_name():
    with pytest.raises(ValueError):
        compile_pgvector_ddl(_contract(vectorConfig={"table": "t; DROP TABLE users; --"}))


def test_ddl_comment_cannot_break_out_via_newline():
    """A newline in a comment-echoed field must not terminate the -- comment.

    Column names carry no schema pattern and the embedding-model string is free
    text, so a newline could otherwise land the rest as executable SQL. Every
    generated header line must stay a single comment line.
    """
    contract = _contract(
        vectorConfig={"embeddingModel": "m\nDROP TABLE evil; --", "indexType": "none"}
    )
    contract["exposes"][0]["contract"]["schema"].append(
        {
            "name": "bad\nDROP TABLE also_evil; --",
            "type": "text",
            "labels": {"ai-embeddable": "true"},
        }
    )
    ddl = compile_pgvector_ddl(contract)
    # The malicious text may survive as inert comment text, but it must never
    # escape onto its own line — every line mentioning DROP stays a -- comment.
    for line in ddl.splitlines():
        if "DROP" in line.upper():
            assert line.lstrip().startswith("--"), f"injection escaped the comment: {line!r}"


# --------------------------------------------------------------------------- #
# RAG manifest
# --------------------------------------------------------------------------- #
def test_manifest_carries_rag_provenance():
    manifest = build_rag_manifest(_contract())
    assert manifest["vectorStore"] == "pgvector"
    assert len(manifest["targets"]) == 1
    target = manifest["targets"][0]
    assert target["product"] == "acme.customers"
    assert target["expose"] == "customer_profiles"
    assert target["table"] == "customer_profiles_embeddings"
    assert target["dimensions"] == 1536
    assert target["embeddingModel"] == "text-embedding-3-small"
    assert target["distanceMetric"] == "cosine"
    assert target["sourceKeyColumn"] == "customer_id"
    cols = [c["name"] for c in target["embeddableColumns"]]
    assert cols == ["bio", "notes"]  # ai-embeddable only, order preserved
    assert "internal_flag" not in cols


def test_compile_vector_port_bundles_ddl_and_manifest():
    art = compile_vector_port(_contract())
    assert "CREATE EXTENSION" in art.ddl
    assert art.manifest["targets"][0]["expose"] == "customer_profiles"
    assert len(art.targets) == 1
    assert art.targets[0].table == "customer_profiles_embeddings"


# --------------------------------------------------------------------------- #
# validate_vector_binding (plan-time gate)
# --------------------------------------------------------------------------- #
def test_validate_clean_contract_has_no_errors():
    errors, _warnings = validate_vector_binding(_contract())
    assert errors == []


def test_validate_missing_dimensions_is_error():
    contract = _contract()
    del contract["exposes"][0]["binding"]["vectorConfig"]["dimensions"]
    errors, _ = validate_vector_binding(contract)
    assert any("dimensions" in e for e in errors)


def test_validate_no_embeddable_columns_warns():
    contract = _contract()
    for col in contract["exposes"][0]["contract"]["schema"]:
        col.pop("labels", None)
    _errors, warnings = validate_vector_binding(contract)
    assert any("ai-embeddable" in w for w in warnings)


def test_validate_missing_model_and_source_key_warns():
    contract = _contract()
    vcfg = contract["exposes"][0]["binding"]["vectorConfig"]
    del vcfg["embeddingModel"]
    del vcfg["sourceKeyColumn"]
    _errors, warnings = validate_vector_binding(contract)
    assert any("embeddingModel" in w for w in warnings)
    assert any("sourceKeyColumn" in w for w in warnings)


def test_validate_table_name_collision_is_error():
    contract = _contract()
    # A second pgvector expose whose explicit table collides with the first's default.
    contract["exposes"].append(
        {
            "exposeId": "other",
            "binding": {
                "platform": "pgvector",
                "format": "pgvector_table",
                "location": {},
                "vectorConfig": {"dimensions": 8, "table": "customer_profiles_embeddings"},
            },
            "contract": {
                "schema": [{"name": "t", "type": "text", "labels": {"ai-embeddable": "true"}}]
            },
        }
    )
    errors, _ = validate_vector_binding(contract)
    assert any("same" in e.lower() and "table" in e.lower() for e in errors)


# --------------------------------------------------------------------------- #
# End-to-end CLI wiring (proves the cli/validate.py + cli/generate_vector.py hooks)
# --------------------------------------------------------------------------- #
def _write_contract(tmp_path, vector_config):
    import yaml

    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "test.rag.cli",
        "name": "pgvector CLI wiring test",
        "metadata": {"layer": "Gold", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [
            {
                "exposeId": "docs",
                "kind": "vector",
                "binding": {
                    "platform": "pgvector",
                    "format": "pgvector_table",
                    "location": {"database": "rag"},
                    "vectorConfig": vector_config,
                },
                "contract": {
                    "schema": [
                        {"name": "body", "type": "text", "labels": {"ai-embeddable": "true"}}
                    ]
                },
            }
        ],
    }
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract))
    return path


def _run_cli(args):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.integration
def test_cli_validate_rejects_missing_dimensions(tmp_path):
    """The cli/validate.py hook surfaces the vector binding error end-to-end."""
    contract = _write_contract(tmp_path, {"embeddingModel": "m", "sourceKeyColumn": "id"})
    result = _run_cli(["validate", str(contract)])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "dimensions" in (result.stdout + result.stderr)


@pytest.mark.integration
def test_cli_generate_vector_writes_artifacts(tmp_path):
    """`fluid generate vector` emits embeddings.sql + vector_manifest.json."""
    import json

    contract = _write_contract(
        tmp_path, {"dimensions": 8, "embeddingModel": "m", "sourceKeyColumn": "id"}
    )
    out = tmp_path / "vec"
    result = _run_cli(["generate", "vector", str(contract), "--out", str(out)])
    assert result.returncode == 0, result.stdout + result.stderr
    sql = (out / "embeddings.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "embedding vector(8)" in sql
    manifest = json.loads((out / "vector_manifest.json").read_text())
    assert manifest["targets"][0]["expose"] == "docs"
