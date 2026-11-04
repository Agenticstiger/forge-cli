# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from fluid_build.forge_datamodel.diff import diff_logical_models


def test_diff_logical_models_reports_added_hub(tmp_path: Path):
    old = tmp_path / "old.model.json"
    new = tmp_path / "new.model.json"
    old.write_text(
        """
{
  "name": "orders",
  "description": "old",
  "technique": "data_vault_2",
  "dv2": {"hubs": [], "links": [], "satellites": [], "pits": [], "bridges": [], "hash_key_strategy": "md5"},
  "osi": {"name": "orders", "description": "old", "ai_context": {}, "datasets": [], "relationships": [], "metrics": [], "custom_extensions": []},
  "source_summary": {}
}
""".strip(),
        encoding="utf-8",
    )
    new.write_text(
        """
{
  "name": "orders",
  "description": "new",
  "technique": "data_vault_2",
  "dv2": {
    "hubs": [{"entity_name": "customer", "hub_table_name": "hub_customer", "business_key_columns": ["customer_id"], "mapped_source_tables": ["customers"]}],
    "links": [],
    "satellites": [],
    "pits": [],
    "bridges": [],
    "hash_key_strategy": "md5"
  },
  "osi": {"name": "orders", "description": "new", "ai_context": {}, "datasets": [], "relationships": [], "metrics": [], "custom_extensions": []},
  "source_summary": {}
}
""".strip(),
        encoding="utf-8",
    )
    summary = diff_logical_models(old, new)
    assert "Added hub hub_customer." in summary["changes"]
