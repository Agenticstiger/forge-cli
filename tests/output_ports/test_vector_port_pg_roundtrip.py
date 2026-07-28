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

"""Gated live round-trip: the emitted pgvector DDL runs on a real database.

Self-skipping. Runs only when ``FLUID_VECTOR_TEST_DSN`` points at a
PostgreSQL instance with the ``vector`` extension available (e.g. the
``pgvector/pgvector`` image). Never a light-suite dependency — no DSN, no run.

Provisioning (CI integration stage / manual):

    docker run -d --rm --name fluid-pgvec -p 5433:5432 \\
        -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16
    FLUID_VECTOR_TEST_DSN="postgresql://postgres:pw@localhost:5433/postgres" \\
        .venv/bin/python -m pytest tests/output_ports/test_vector_port_pg_roundtrip.py -v

Proves that ``compile_pgvector_ddl`` output is executable end-to-end: the
extension + embeddings table + HNSW index are created, an embedding row
inserts, and a cosine-distance nearest-neighbour query returns it — the
contract the emitter promises but a pure unit test can't confirm.
"""

from __future__ import annotations

import os

import pytest

from fluid_build.output_ports.vector import compile_pgvector_ddl

pytestmark = pytest.mark.integration

_DSN = os.environ.get("FLUID_VECTOR_TEST_DSN")

psycopg = pytest.importorskip("psycopg")

if not _DSN:
    pytest.skip(
        "set FLUID_VECTOR_TEST_DSN to a pgvector-capable PostgreSQL DSN to run the "
        "vector-port live round-trip",
        allow_module_level=True,
    )


def _contract(table: str) -> dict:
    return {
        "id": "test.rag.roundtrip",
        "exposes": [
            {
                "exposeId": "docs",
                "binding": {
                    "platform": "pgvector",
                    "format": "pgvector_table",
                    "location": {"table": "docs"},
                    "vectorConfig": {
                        "dimensions": 3,
                        "embeddingModel": "test-model",
                        "indexType": "hnsw",
                        "distanceMetric": "cosine",
                        "sourceKeyColumn": "doc_id",
                        "table": table,
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "doc_id", "type": "bigint"},
                        {"name": "body", "type": "text", "labels": {"ai-embeddable": "true"}},
                    ]
                },
            }
        ],
    }


def test_emitted_ddl_round_trips_on_real_pgvector() -> None:
    table = "fluid_vec_roundtrip_test"
    ddl = compile_pgvector_ddl(_contract(table))

    with psycopg.connect(_DSN, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')
        try:
            # 1. The emitted DDL executes verbatim (extension + table + index).
            cur.execute(ddl)

            # 2. Insert three embeddings; row A is closest to the probe vector.
            cur.execute(
                f'INSERT INTO "{table}" (source_key, source_column, content, embedding, '
                f"embedding_model) VALUES "
                f"('a', 'body', 'alpha', '[1,0,0]', 'test-model'),"
                f"('b', 'body', 'beta',  '[0,1,0]', 'test-model'),"
                f"('c', 'body', 'gamma', '[0,0,1]', 'test-model')"
            )

            # 3. Cosine-distance NN query returns the nearest row first.
            cur.execute(
                f'SELECT source_key FROM "{table}" ORDER BY embedding <=> %s LIMIT 1',
                ("[0.9,0.1,0]",),
            )
            nearest = cur.fetchone()[0]
            assert nearest == "a", f"expected 'a' nearest to the probe, got {nearest!r}"

            # 4. The HNSW index the emitter named exists.
            cur.execute(
                "SELECT 1 FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                (table, f"{table}_embedding_idx"),
            )
            assert cur.fetchone() is not None, "HNSW index was not created"
        finally:
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
