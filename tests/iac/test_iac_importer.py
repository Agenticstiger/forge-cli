# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
