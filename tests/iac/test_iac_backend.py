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

"""Unit tests for OpenTofu state-backend generation."""

from __future__ import annotations

import pytest

from fluid_build.iac import assemble_tofu_document
from fluid_build.iac.backend import parse_backend

pytestmark = pytest.mark.unit


class TestParseBackend:
    def test_none_or_empty_means_local_state(self):
        assert parse_backend(None) is None
        assert parse_backend("") is None

    def test_s3_backend(self):
        block = parse_backend("s3://my-state-bucket/fluid/prod.tfstate")
        assert block == {"s3": {"bucket": "my-state-bucket", "key": "fluid/prod.tfstate"}}

    def test_s3_backend_supplies_a_default_key(self):
        block = parse_backend("s3://my-state-bucket")
        assert block["s3"]["bucket"] == "my-state-bucket"
        assert block["s3"]["key"]

    def test_gcs_backend(self):
        block = parse_backend("gcs://my-state-bucket/fluid")
        assert block == {"gcs": {"bucket": "my-state-bucket", "prefix": "fluid"}}

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError):
            parse_backend("azurerm://container/key")

    def test_bucketless_spec_raises(self):
        with pytest.raises(ValueError):
            parse_backend("s3://")


class TestBackendInDocument:
    def test_backend_block_lands_in_terraform(self):
        doc = assemble_tofu_document(
            required_providers={"google": {"source": "hashicorp/google", "version": "~> 6.0"}},
            resources={},
            backend={"gcs": {"bucket": "b"}},
        )
        assert doc["terraform"]["backend"] == {"gcs": {"bucket": "b"}}

    def test_no_backend_keeps_local_state(self):
        doc = assemble_tofu_document(
            required_providers={"aws": {"source": "hashicorp/aws", "version": "~> 5.0"}},
            resources={},
        )
        assert "backend" not in doc["terraform"]
