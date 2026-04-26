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

"""End-to-end coverage for the shipped-but-underkissed v1.1+ emit flags.

* ``--emit-ddl-dir`` → ``emit_ddl_files`` + CLI ``_write_auxiliary_artifacts``
* ``--emit-dimensional-variants`` → ``emit_dimensional_variants`` + CLI

These flags were shipped in v1.1+ and listed in the roadmap but had no
end-to-end test that materialised files on disk through the CLI
adapter. This suite builds minimal ``LogicalDraft`` fixtures (one DV2,
one dimensional) and drives both emit paths through the CLI helper,
asserting both function outputs and on-disk artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.cli.forge_data_model import _write_auxiliary_artifacts
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    FieldDefinition,
    HubDefinition,
    LinkDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.forge_datamodel.emit.ddl import emit_ddl_files
from fluid_build.forge_datamodel.emit.variants import emit_dimensional_variants

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_dv2_logical() -> LogicalDraft:
    return LogicalDraft(
        name="orders_domain",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                )
            ],
            links=[
                LinkDefinition(
                    link_name="customer_order",
                    link_table_name="lnk_customer_order",
                    hubs_involved=["hub_customer", "hub_order"],
                )
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="customer",
                    satellite_table_name="sat_customer",
                    parent_hub="hub_customer",
                    attributes=["email", "name"],
                )
            ],
        ),
        osi=OSISemanticModel(name="orders_domain_osi"),
    )


def _make_dimensional_logical() -> LogicalDraft:
    return LogicalDraft(
        name="sales_domain",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_order_line",
                    grain_statement="one row per order line item",
                    measures=[
                        FieldDefinition(name="quantity", data_type="INTEGER"),
                        FieldDefinition(name="unit_price", data_type="DECIMAL(18,2)"),
                    ],
                    foreign_keys=["dim_customer_key", "dim_product_key"],
                )
            ],
            dimensions=[
                DimensionTable(
                    name="dim_customer",
                    attributes=[FieldDefinition(name="customer_name", data_type="STRING")],
                ),
                DimensionTable(
                    name="dim_product",
                    attributes=[FieldDefinition(name="product_name", data_type="STRING")],
                ),
            ],
        ),
        osi=OSISemanticModel(name="sales_domain_osi"),
    )


# ---------------------------------------------------------------------------
# emit_ddl_files (pure function)
# ---------------------------------------------------------------------------


class TestEmitDdlFiles:
    def test_dv2_emits_sql_per_hub_link_sat(self):
        logical = _make_dv2_logical()
        files = emit_ddl_files(logical)
        assert set(files.keys()) == {
            "hub_customer.sql",
            "lnk_customer_order.sql",
            "sat_customer.sql",
        }
        for name, content in files.items():
            assert content.startswith("create table")
            assert name[:-4] in content  # table name embedded in DDL

    def test_dimensional_emits_sql_per_fact_dim(self):
        logical = _make_dimensional_logical()
        files = emit_ddl_files(logical)
        assert "fact_order_line.sql" in files
        assert "dim_customer.sql" in files
        assert "dim_product.sql" in files
        assert "quantity INTEGER" in files["fact_order_line.sql"]

    def test_empty_logical_emits_nothing(self):
        empty = LogicalDraft(
            name="empty",
            technique="data_vault_2",
            dv2=DV2Model(),
            osi=OSISemanticModel(name="empty_osi"),
        )
        assert emit_ddl_files(empty) == {}


# ---------------------------------------------------------------------------
# emit_dimensional_variants (pure function)
# ---------------------------------------------------------------------------


class TestEmitDimensionalVariants:
    def test_dimensional_emits_four_variants(self):
        logical = _make_dimensional_logical()
        variants = emit_dimensional_variants(logical)
        expected = {
            "sales_domain.star.model.json",
            "sales_domain.snowflake.model.json",
            "sales_domain.galaxy.model.json",
            "sales_domain.flat.model.json",
        }
        assert set(variants.keys()) == expected
        for name, content in variants.items():
            doc = json.loads(content)
            variant = name.split(".")[1]
            assert doc["source_summary"]["dimensional_variant"] == variant
            # Base model preserved across variants (facts + dims present).
            assert doc["dimensional"]["facts"][0]["name"] == "fact_order_line"

    def test_dv2_technique_emits_nothing(self):
        """Variants are dimensional-only; DV2 drafts must produce {}."""
        logical = _make_dv2_logical()
        assert emit_dimensional_variants(logical) == {}


# ---------------------------------------------------------------------------
# CLI adapter (_write_auxiliary_artifacts)
# ---------------------------------------------------------------------------


class TestWriteAuxiliaryArtifacts:
    def test_ddl_dir_flag_writes_files_on_disk(self, tmp_path: Path):
        logical = _make_dv2_logical()
        ddl_dir = tmp_path / "ddl"
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=str(ddl_dir),
            emit_dimensional_variants=None,
        )
        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)
        assert ddl_dir.is_dir()
        files = {p.name for p in ddl_dir.glob("*.sql")}
        assert "hub_customer.sql" in files
        assert "lnk_customer_order.sql" in files
        assert "sat_customer.sql" in files

    def test_dimensional_variants_flag_writes_files_on_disk(self, tmp_path: Path):
        logical = _make_dimensional_logical()
        variants_dir = tmp_path / "variants"
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=None,
            emit_dimensional_variants=str(variants_dir),
        )
        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)
        assert variants_dir.is_dir()
        files = {p.name for p in variants_dir.glob("*.model.json")}
        assert "sales_domain.star.model.json" in files
        assert "sales_domain.snowflake.model.json" in files
        assert "sales_domain.galaxy.model.json" in files
        assert "sales_domain.flat.model.json" in files

    def test_both_flags_coexist(self, tmp_path: Path):
        logical = _make_dimensional_logical()
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=str(tmp_path / "ddl"),
            emit_dimensional_variants=str(tmp_path / "variants"),
        )
        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)
        assert (tmp_path / "ddl" / "fact_order_line.sql").exists()
        assert (tmp_path / "variants" / "sales_domain.star.model.json").exists()

    def test_neither_flag_creates_nothing(self, tmp_path: Path):
        logical = _make_dimensional_logical()
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=None,
            emit_dimensional_variants=None,
        )
        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)
        assert list(tmp_path.iterdir()) == []

    def test_ddl_filenames_are_sanitized_inside_output_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        logical = _make_dv2_logical()
        ddl_dir = tmp_path / "ddl"
        escaped = tmp_path / "escaped.sql"
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=str(ddl_dir),
            emit_dimensional_variants=None,
        )

        monkeypatch.setattr(
            "fluid_build.cli.forge_data_model.emit_ddl_files",
            lambda _logical: {
                "../escaped.sql": "create table escaped (id string);\n",
                "/tmp/absolute.sql": "create table absolute (id string);\n",
            },
        )

        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)

        assert not escaped.exists()
        assert (ddl_dir / "escaped.sql").exists()
        assert (ddl_dir / "absolute.sql").exists()
        assert {p.name for p in ddl_dir.iterdir()} == {"escaped.sql", "absolute.sql"}

    def test_variant_filenames_are_sanitized_and_deduplicated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        logical = _make_dimensional_logical()
        variants_dir = tmp_path / "variants"
        args = SimpleNamespace(
            emit_osi_sidecar=False,
            emit_ddl_dir=None,
            emit_dimensional_variants=str(variants_dir),
        )

        monkeypatch.setattr(
            "fluid_build.cli.forge_data_model.emit_dimensional_variants",
            lambda _logical: {
                "../../sales.model.json": "{}",
                "sales.model.json": "{}",
                "sales model?.json": "{}",
            },
        )

        _write_auxiliary_artifacts(args, output_path=tmp_path / "contract.yaml", logical=logical)

        assert {p.name for p in variants_dir.iterdir()} == {
            "sales.model.json",
            "sales_2.model.json",
            "sales_model_.json",
        }
