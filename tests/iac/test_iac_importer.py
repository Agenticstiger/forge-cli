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

"""Unit tests for brownfield ``import {}``-block generation."""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import ImportBlock, build_module, get_iac_plugin
from fluid_build.iac.importer import import_section

pytestmark = pytest.mark.unit


class TestImportSection:
    def test_empty_blocks_yield_empty_section(self):
        assert import_section([]) == {}

    def test_blocks_render_to_import_list(self):
        blocks = [
            ImportBlock(to="aws_s3_bucket.raw", id="my-bucket"),
            ImportBlock(to="google_bigquery_dataset.d", id="projects/p/datasets/d"),
        ]
        assert import_section(blocks) == {
            "import": [
                {"to": "aws_s3_bucket.raw", "id": "my-bucket"},
                {"to": "google_bigquery_dataset.d", "id": "projects/p/datasets/d"},
            ]
        }


class TestImportsInModule:
    def test_build_module_embeds_import_blocks(self):
        contract = {
            "id": "d",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ],
        }
        blocks = [ImportBlock(to="google_bigquery_dataset.x", id="projects/p/datasets/d")]
        doc = json.loads(build_module(get_iac_plugin("gcp"), contract, imports=blocks))
        assert doc["import"] == [{"to": "google_bigquery_dataset.x", "id": "projects/p/datasets/d"}]

    def test_no_imports_means_no_import_key(self):
        doc = json.loads(build_module(get_iac_plugin("gcp"), {"id": "d", "exposes": []}))
        assert "import" not in doc


# ─── AWS discover_imports ────────────────────────────────────────────────
#
# Mirrors the Snowflake plugin's brownfield-discovery shape. The apply
# engine's ``_adopt_existing`` tolerates ``tofu import`` failures (the
# resource doesn't exist → ``tofu apply`` creates it), so the plugin
# emits a candidate per declared resource without an upfront live
# check. Import IDs follow the ``hashicorp/aws`` documented format.


class TestAwsDiscoverImports:
    def test_glue_db_and_table_imports(self, monkeypatch):
        """Provider import ids: Glue resources use ``{catalog_id}:{name}``
        — the catalog_id is required by hashicorp/aws (validated by the
        live brownfield test). Without it, ``tofu import`` fails ``Invalid
        import id`` and the apply then fails ``AlreadyExistsException``."""
        monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
        contract = {
            "id": "prod",
            "exposes": [
                {
                    "exposeId": "events",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {
                            "database": "fluid_demo_db",
                            "table": "events",
                            "bucket": "fluid-demo-data",
                            "path": "events/",
                        },
                    },
                }
            ],
        }
        by_addr = {b.to: b.id for b in get_iac_plugin("aws").discover_imports(contract)}
        # Glue DB import — id is ``{catalog_id}:{name}`` per hashicorp/aws docs.
        db_addrs = [a for a in by_addr if a.startswith("aws_glue_catalog_database.")]
        assert len(db_addrs) == 1
        assert by_addr[db_addrs[0]] == "123456789012:fluid_demo_db"
        # Glue table import — id is ``{catalog_id}:{db}:{table}``.
        tbl_addrs = [a for a in by_addr if a.startswith("aws_glue_catalog_table.")]
        assert len(tbl_addrs) == 1
        assert by_addr[tbl_addrs[0]] == "123456789012:fluid_demo_db:events"
        # S3 bucket import — id is the bucket name.
        bkt_addrs = [a for a in by_addr if a.startswith("aws_s3_bucket.")]
        assert len(bkt_addrs) == 1
        assert by_addr[bkt_addrs[0]] == "fluid-demo-data"

    def test_glue_imports_suppressed_when_catalog_id_unresolvable(self, monkeypatch):
        """Without an AWS account id (no env var, no working boto3 sts),
        the Glue import blocks are SUPPRESSED — emitting one with an
        invalid id would cause ``tofu import`` to fail loudly and
        confuse the operator. S3 + Kinesis don't need a catalog_id so
        they're still emitted."""
        monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
        # Force the cached catalog_id resolver to return "" (simulates
        # no creds / network unreachable / no boto3).
        from fluid_build.iac.providers import aws as aws_plugin

        monkeypatch.setattr(aws_plugin, "_resolve_catalog_id", lambda: "")
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "x",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {
                            "database": "demo_db",
                            "table": "events",
                            "bucket": "demo-bk",
                        },
                    },
                }
            ],
        }
        addrs = {b.to for b in get_iac_plugin("aws").discover_imports(contract)}
        assert not any(a.startswith("aws_glue_catalog_database.") for a in addrs)
        assert not any(a.startswith("aws_glue_catalog_table.") for a in addrs)
        # S3 still emitted.
        assert any(a.startswith("aws_s3_bucket.") for a in addrs)

    def test_kinesis_stream_import(self):
        contract = {
            "id": "stream-prod",
            "exposes": [
                {
                    "exposeId": "k",
                    "binding": {
                        "platform": "aws",
                        "format": "kinesis_stream",
                        "location": {"stream": "fluid-events-stream"},
                    },
                }
            ],
        }
        blocks = get_iac_plugin("aws").discover_imports(contract)
        addrs = [b.to for b in blocks if b.to.startswith("aws_kinesis_stream.")]
        assert len(addrs) == 1
        assert next(b for b in blocks if b.to == addrs[0]).id == "fluid-events-stream"

    def test_redshift_namespace_import(self):
        contract = {
            "id": "rs",
            "exposes": [
                {
                    "exposeId": "n",
                    "binding": {
                        "platform": "aws",
                        "format": "redshift_table",
                        "location": {"namespace": "fluid-warehouse-ns"},
                    },
                }
            ],
        }
        blocks = get_iac_plugin("aws").discover_imports(contract)
        ns = [b for b in blocks if b.to.startswith("aws_redshiftserverless_namespace.")]
        assert len(ns) == 1
        assert ns[0].id == "fluid-warehouse-ns"

    def test_no_aws_exposure_no_imports(self):
        """A GCP-only contract emits zero AWS import blocks."""
        contract = {
            "id": "g",
            "exposes": [
                {"exposeId": "x", "binding": {"platform": "gcp", "location": {"dataset": "d"}}}
            ],
        }
        assert get_iac_plugin("aws").discover_imports(contract) == []

    def test_glue_table_skipped_when_format_not_lakehouse(self):
        """A Redshift-flavoured binding (database is internal to the
        workgroup) does NOT emit a Glue DB import — mirrors the emit
        gate in ``_emit_glue``."""
        contract = {
            "id": "rs",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "aws",
                        "format": "redshift_table",
                        "location": {"database": "warehouse_db", "table": "events"},
                    },
                }
            ],
        }
        blocks = get_iac_plugin("aws").discover_imports(contract)
        # No Glue catalog import (Redshift database is not the Glue catalog).
        assert not any(b.to.startswith("aws_glue_catalog_database.") for b in blocks)
        assert not any(b.to.startswith("aws_glue_catalog_table.") for b in blocks)

    def test_duplicate_resources_dedup(self, monkeypatch):
        """Two exposures naming the same bucket emit one import block."""
        monkeypatch.setenv("AWS_ACCOUNT_ID", "111111111111")
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "a",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {
                            "database": "d",
                            "table": "a",
                            "bucket": "shared",
                        },
                    },
                },
                {
                    "exposeId": "b",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {
                            "database": "d",
                            "table": "b",
                            "bucket": "shared",
                        },
                    },
                },
            ],
        }
        blocks = get_iac_plugin("aws").discover_imports(contract)
        bucket_blocks = [b for b in blocks if b.to.startswith("aws_s3_bucket.")]
        assert len(bucket_blocks) == 1  # deduped on the bucket address


# ─── GCP discover_imports ────────────────────────────────────────────────


class TestGcpDiscoverImports:
    def test_bq_dataset_and_table_imports_with_project(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PROJECT", "demo-proj")
        contract = {
            "id": "prod",
            "exposes": [
                {
                    "exposeId": "events",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "demo_ds", "table": "events"},
                    },
                }
            ],
        }
        by_addr = {b.to: b.id for b in get_iac_plugin("gcp").discover_imports(contract)}
        # Dataset import — provider id is ``projects/{p}/datasets/{ds}``.
        ds_addrs = [a for a in by_addr if a.startswith("google_bigquery_dataset.")]
        assert len(ds_addrs) == 1
        assert by_addr[ds_addrs[0]] == "projects/demo-proj/datasets/demo_ds"
        # Table import — provider id is ``projects/{p}/datasets/{ds}/tables/{t}``.
        tbl_addrs = [a for a in by_addr if a.startswith("google_bigquery_table.")]
        assert len(tbl_addrs) == 1
        assert by_addr[tbl_addrs[0]] == "projects/demo-proj/datasets/demo_ds/tables/events"

    def test_gcs_bucket_import(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PROJECT", "demo-proj")
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "f",
                    "binding": {
                        "platform": "gcp",
                        "format": "parquet",
                        "location": {"bucket": "fluid-data-lake"},
                    },
                }
            ],
        }
        blocks = get_iac_plugin("gcp").discover_imports(contract)
        bkt = [b for b in blocks if b.to.startswith("google_storage_bucket.")]
        assert len(bkt) == 1
        assert bkt[0].id == "fluid-data-lake"

    def test_pubsub_topic_import(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PROJECT", "demo-proj")
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "gcp",
                        "format": "pubsub_topic",
                        "location": {"topic": "events-topic"},
                    },
                }
            ],
        }
        blocks = get_iac_plugin("gcp").discover_imports(contract)
        tp = [b for b in blocks if b.to.startswith("google_pubsub_topic.")]
        assert len(tp) == 1
        assert tp[0].id == "projects/demo-proj/topics/events-topic"

    def test_no_project_env_var_falls_back_to_bare_id(self, monkeypatch):
        """Without ``GOOGLE_PROJECT`` set, the import id omits the
        ``projects/{p}/`` prefix — the provider's ADC-resolved project
        kicks in at import time."""
        monkeypatch.delenv("GOOGLE_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "x",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ],
        }
        by_addr = {b.to: b.id for b in get_iac_plugin("gcp").discover_imports(contract)}
        ds_id = next(v for k, v in by_addr.items() if k.startswith("google_bigquery_dataset."))
        assert ds_id == "d"

    def test_no_gcp_exposure_no_imports(self):
        contract = {
            "id": "x",
            "exposes": [
                {"exposeId": "a", "binding": {"platform": "aws", "location": {"bucket": "b"}}}
            ],
        }
        assert get_iac_plugin("gcp").discover_imports(contract) == []
