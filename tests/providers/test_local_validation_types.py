# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Type names the local validator reads as one logical type."""

from __future__ import annotations

import pytest

from fluid_build.providers.local_validation import LocalValidationProvider

pytestmark = pytest.mark.unit


def test_a_parquet_variant_column_is_read_as_json_like_variant(tmp_path):
    provider = LocalValidationProvider({"base_dir": str(tmp_path)})
    assert provider.normalize_type("parquet_variant") == provider.normalize_type("variant")
