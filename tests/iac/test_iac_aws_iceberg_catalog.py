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

"""The AWS emitter reads an Iceberg expose's ``location.catalog``.

``catalog: lakekeeper`` used to stream over REST while the AWS IaC created a
Glue database and table for the same name: a second, metadata-less claim on a
table that lived in Lakekeeper. Now an Iceberg table in a non-Glue catalog gets
its S3 bucket (the catalog's storage profile still needs it) and no Glue
resource, every Lake Formation gate agrees (an LF resource naming an undeclared
``aws_glue_catalog_table`` fails ``tofu validate``), and Lake Formation on it is
refused at emit and at validate rather than dropped. Absent or ``glue`` on AWS
is the Glue catalog, and its emit is unchanged byte for byte.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers import aws as aws_mod

pytestmark = [pytest.mark.unit, pytest.mark.provider]

#: Every non-Glue spelling the emitter must treat as "not in Glue".
NON_GLUE_CATALOGS = ["lakekeeper", "rest", "iceberg-rest", "iceberg_rest", "polaris", "nessie"]
GLUE_TYPES = ("aws_glue_catalog_database", "aws_glue_catalog_table")
ANALYST = "arn:aws:iam::111111111111:role/analyst"
SCHEMA = [{"name": "order_id", "type": "string"}, {"name": "qty", "type": "integer"}]


def _aws():
    return get_iac_plugin("aws")


def _expose(
    fmt: str = "iceberg",
    catalog: Optional[str] = None,
    *,
    expose_id: str = "orders",
    table: str = "orders",
    governance: Optional[Dict[str, Any]] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "sales",
        "table": table,
        "bucket": "lake",
        "path": f"{table}/",
    }
    if catalog is not None:
        location["catalog"] = catalog
    binding: Dict[str, Any] = {"platform": "aws", "format": fmt, "location": location}
    if governance is not None:
        binding["governance"] = governance
    exposure: Dict[str, Any] = {
        "exposeId": expose_id,
        "binding": binding,
        "contract": {"schema": copy.deepcopy(SCHEMA)},
    }
    if policy is not None:
        exposure["policy"] = policy
    return exposure


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": "analytics.lake", "name": "Lake", "exposes": list(exposes)}


def _lake_formation() -> Dict[str, Any]:
    return {
        "lakeFormation": {
            "registerLocation": True,
            "grants": [{"principal": ANALYST, "permissions": ["SELECT", "DESCRIBE"]}],
            "tags": {"tier": "gold"},
            "rowFilter": {"name": "eu", "rowExpression": "qty > 0"},
        }
    }


def _without_contract_yaml(resources: Dict[str, Any]) -> Dict[str, Any]:
    """The emit minus ``parameters.fluid_contract``: the contract's own YAML, which
    differs between two contracts by exactly the ``catalog`` line under test."""
    out = copy.deepcopy(resources)
    for table in (out.get("aws_glue_catalog_table") or {}).values():
        table["parameters"].pop("fluid_contract", None)
    return out


def _dangling_glue_refs(resources: Dict[str, Any]) -> List[str]:
    """``${aws_glue_catalog_*.<key>...}`` references whose resource is not emitted."""
    text = json.dumps(resources, default=str)
    refs = set(re.findall(r"\$\{(aws_glue_catalog_(?:table|database))\.([A-Za-z0-9_]+)\.", text))
    return sorted(f"{t}.{k}" for t, k in refs if k not in (resources.get(t) or {}))


# ---------------------------------------------------------------------------
# A non-Glue Iceberg catalog: the bucket, no Glue
# ---------------------------------------------------------------------------


class TestNonGlueIcebergCatalog:
    @pytest.mark.parametrize("catalog", NON_GLUE_CATALOGS)
    def test_emits_the_bucket_and_no_glue_database_or_table(self, catalog):
        resources = _aws().emit(_contract(_expose("iceberg", catalog)))

        for resource_type in GLUE_TYPES:
            assert resource_type not in resources, (catalog, resource_type)
        # The catalog's storage profile still writes into the bucket.
        assert resources["aws_s3_bucket"]["analytics_lake_lake"]["bucket"] == "lake"

    @pytest.mark.parametrize("catalog", NON_GLUE_CATALOGS)
    def test_imports_no_glue_resource_and_resolves_no_account(self, catalog, monkeypatch):
        """No ``tofu import`` of a Glue table of the same name (it would adopt
        someone else's table into this state), and no STS call to resolve a
        catalog id nothing needs."""

        def _no_sts() -> str:
            raise AssertionError("discover_imports resolved a Glue catalog id")

        monkeypatch.setattr(aws_mod, "_resolve_catalog_id", _no_sts)
        blocks = _aws().discover_imports(_contract(_expose("iceberg", catalog)))

        addresses = [block.to for block in blocks]
        assert not [a for a in addresses if a.startswith("aws_glue_")]
        assert "aws_s3_bucket.analytics_lake_lake" in addresses

    def test_a_glue_database_another_expose_uses_is_kept(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
        contract = _contract(
            _expose("parquet", expose_id="facts", table="facts"),
            _expose("iceberg", "lakekeeper"),
        )

        resources = _aws().emit(contract)
        addresses = {block.to for block in _aws().discover_imports(contract)}

        assert list(resources["aws_glue_catalog_database"]) == ["analytics_lake_sales"]
        assert list(resources["aws_glue_catalog_table"]) == ["analytics_lake_sales_facts"]
        assert "aws_glue_catalog_table.analytics_lake_sales_orders" not in addresses
        assert "aws_glue_catalog_table.analytics_lake_sales_facts" in addresses
        assert "aws_glue_catalog_database.analytics_lake_sales" in addresses

    def test_iceberg_table_format_spelling_is_the_same_table(self):
        resources = _aws().emit(_contract(_expose("ICEBERG", "Lakekeeper")))
        assert not set(GLUE_TYPES) & set(resources)


# ---------------------------------------------------------------------------
# Glue, absent or explicit: unchanged
# ---------------------------------------------------------------------------

#: The emit of an Iceberg expose with no ``catalog`` (minus the contract YAML
#: parameter), as forge-cli emitted it before ``location.catalog`` was read.
ABSENT_CATALOG_GOLDEN: Dict[str, Any] = {
    "aws_glue_catalog_database": {
        "analytics_lake_sales": {"name": "sales", "lifecycle": {"ignore_changes": ["parameters"]}}
    },
    "aws_glue_catalog_table": {
        "analytics_lake_sales_orders": {
            "name": "orders",
            "database_name": "${aws_glue_catalog_database.analytics_lake_sales.name}",
            "table_type": "EXTERNAL_TABLE",
            "parameters": {
                "classification": "iceberg",
                "managed_by": "fluid",
                "table_type": "ICEBERG",
            },
            "storage_descriptor": {
                "columns": [{"name": "order_id", "type": "string"}, {"name": "qty", "type": "int"}],
                "location": "s3://lake/orders/",
            },
            "lifecycle": {"ignore_changes": ["parameters"]},
        }
    },
    "aws_s3_bucket": {
        "analytics_lake_lake": {
            "bucket": "lake",
            "force_destroy": True,
            "tags": {"managed_by": "fluid", "fluid_contract": "analytics_lake"},
        }
    },
}


class TestGlueCatalogUnchanged:
    def test_absent_catalog_emit_is_the_golden(self):
        resources = _aws().emit(_contract(_expose("iceberg")))
        assert json.dumps(_without_contract_yaml(resources), sort_keys=True) == json.dumps(
            ABSENT_CATALOG_GOLDEN, sort_keys=True
        )

    @pytest.mark.parametrize("spelling", ["glue", "GLUE", " Glue "])
    def test_explicit_glue_emits_what_absent_does(self, spelling, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
        absent = _contract(_expose("iceberg", governance=_lake_formation()))
        explicit = _contract(_expose("iceberg", spelling, governance=_lake_formation()))
        plugin = _aws()

        assert json.dumps(_without_contract_yaml(plugin.emit(absent)), sort_keys=True) == (
            json.dumps(_without_contract_yaml(plugin.emit(explicit)), sort_keys=True)
        )
        assert json.dumps(plugin.emit_data(absent), sort_keys=True, default=str) == json.dumps(
            plugin.emit_data(explicit), sort_keys=True, default=str
        )
        assert plugin.discover_imports(absent) == plugin.discover_imports(explicit)

    def test_glue_lake_formation_still_emits_against_the_glue_table(self):
        resources = _aws().emit(_contract(_expose("iceberg", "glue", governance=_lake_formation())))
        assert resources["aws_lakeformation_permissions"]
        assert resources["aws_lakeformation_resource_lf_tags"]
        assert resources["aws_lakeformation_data_cells_filter"]
        assert _dangling_glue_refs(resources) == []

    @pytest.mark.parametrize("catalog", ["lakekeeper", "rest"])
    def test_a_parquet_binding_keeps_its_glue_table_whatever_catalog_says(
        self, catalog, monkeypatch
    ):
        """The catalog kind is an Iceberg property: Glue catalogs a parquet table."""
        monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
        contract = _contract(_expose("parquet", catalog))

        resources = _aws().emit(contract)
        addresses = {block.to for block in _aws().discover_imports(contract)}

        assert "analytics_lake_sales_orders" in resources["aws_glue_catalog_table"]
        assert "aws_glue_catalog_table.analytics_lake_sales_orders" in addresses


# ---------------------------------------------------------------------------
# Lake Formation and column restrictions on a non-Glue table: refused
# ---------------------------------------------------------------------------


class TestLakeFormationNeedsGlue:
    def test_emit_refuses_lake_formation_on_a_lakekeeper_table(self):
        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(_contract(_expose("iceberg", "lakekeeper", governance=_lake_formation())))

        assert excinfo.value.kind == "lake-formation-needs-glue-catalog"
        assert "exposes[orders]" in str(excinfo.value)
        assert "'lakekeeper' catalog" in str(excinfo.value)
        assert any("lakekeeper catalog itself" in r for r in excinfo.value.remediation)

    def test_build_module_refuses_it_too(self):
        contract = _contract(_expose("iceberg", "polaris", governance=_lake_formation()))
        with pytest.raises(UnsupportedBindingError) as excinfo:
            build_module(_aws(), contract)
        assert excinfo.value.kind == "lake-formation-needs-glue-catalog"

    def test_validate_reports_it_with_the_emit_message(self):
        contract = _contract(_expose("iceberg", "lakekeeper", governance=_lake_formation()))

        errors, _ = validate_governance(contract)

        assert len(errors) == 1
        assert "governance.lakeFormation" in errors[0]
        assert "'lakekeeper' catalog" in errors[0]

    def test_validate_passes_a_lakekeeper_table_without_lake_formation(self):
        errors, _ = validate_governance(_contract(_expose("iceberg", "lakekeeper")))
        assert errors == []

    def test_unenforced_access_points_at_the_catalog_not_at_lake_formation(self):
        """The Glue binding's warning says "add governance.lakeFormation.grants";
        for a Lakekeeper table that advice would be refused, so it names the catalog."""
        contract = _contract(
            _expose("parquet", expose_id="facts", table="facts"),
            _expose("iceberg", "lakekeeper"),
        )
        contract["accessPolicy"] = {"grants": [{"principal": ANALYST, "permissions": ["read"]}]}

        errors, warnings = validate_governance(contract)

        assert errors == []
        glue_warning, lakekeeper_warning = warnings
        assert "aws binding(s) facts:" in glue_warning
        assert "governance.lakeFormation.grants" in glue_warning
        assert "aws binding(s) orders (lakekeeper):" in lakekeeper_warning
        assert "Grant access in that catalog" in lakekeeper_warning
        assert "governance.lakeFormation" not in lakekeeper_warning

    def test_column_restrictions_name_the_catalog_not_lake_formation(self):
        """``column_access`` alone would say "add Lake Formation grants", which the
        refusal above then refuses: the author would be sent in a circle."""
        policy = {
            "authz": {
                "readers": [ANALYST],
                "columnRestrictions": [
                    {"principal": ANALYST, "columns": ["qty"], "access": "deny"}
                ],
            }
        }
        contract = _contract(_expose("iceberg", "lakekeeper", policy=policy))

        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(contract)
        errors, _ = validate_governance(contract)

        assert excinfo.value.kind == "column-restriction-unenforceable"
        assert "'lakekeeper' catalog" in str(excinfo.value)
        assert "add governance.lakeFormation" not in str(excinfo.value).lower()
        assert len(errors) == 1 and "'lakekeeper' catalog" in errors[0]


# ---------------------------------------------------------------------------
# Every gate shares one predicate
# ---------------------------------------------------------------------------


class TestRowFilterNeedsGlue:
    """``policy.authz.rowFilters`` is enforced only as a Lake Formation data cells
    filter on a Glue table. On a Lakekeeper table it is refused by catalog name,
    not with the generic "add Lake Formation grants" advice, which
    :func:`refuse_lake_formation_on_external_catalog` would then refuse."""

    ANALYSTS = "group:analysts"

    def _row_filtered(self, catalog: Optional[str]) -> Dict[str, Any]:
        exposure = _expose(
            "iceberg",
            catalog,
            policy={
                "authz": {
                    "rowFilters": [
                        {"principal": self.ANALYSTS, "name": "eu_only", "where": "qty > 0"}
                    ]
                }
            },
        )
        exposure["binding"]["principals"] = {
            self.ANALYSTS: "arn:aws:iam::111122223333:role/analyst"
        }
        return _contract(exposure)

    @pytest.mark.parametrize("catalog", ["lakekeeper", "iceberg-rest", "polaris"])
    def test_emit_refuses_by_catalog(self, catalog):
        with pytest.raises(UnsupportedBindingError) as info:
            _aws().emit(self._row_filtered(catalog))
        assert info.value.kind == "row-filter-unenforceable"
        assert "not in AWS Glue" in str(info.value)
        assert "governance.lakeFormation grants" not in str(info.value)

    def test_validate_refuses_with_the_same_message(self):
        errors, _ = validate_governance(self._row_filtered("lakekeeper"))
        assert any(
            "policy.authz.rowFilters" in e and "'lakekeeper' catalog" in e for e in errors
        ), errors


class TestOnePredicate:
    def test_no_lake_formation_resource_names_an_undeclared_glue_table(self):
        """A mixed contract: a Glue parquet table under Lake Formation beside a
        Lakekeeper table without it. Every LF reference resolves."""
        contract = _contract(
            _expose("parquet", expose_id="facts", table="facts", governance=_lake_formation()),
            _expose("iceberg", "lakekeeper"),
        )
        resources = _aws().emit(contract)
        assert resources["aws_lakeformation_permissions"]
        assert _dangling_glue_refs(resources) == []

    def test_every_glue_format_gate_goes_through_the_predicate(self):
        """The format set is tested in ``_glue_cataloged`` alone: a gate that keys on
        it directly would skip the catalog half, and an LF resource it lets through
        would name an undeclared Glue table."""
        import inspect

        from fluid_build.cli import _diff_live

        assert len(re.findall(r"\bin _GLUE_CATALOG_FORMATS\b", inspect.getsource(aws_mod))) == 1
        assert "_GLUE_CATALOG_FORMATS" not in inspect.getsource(_diff_live)


_TOFU = shutil.which("tofu")


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_a_mixed_module_passes_tofu_validate(tmp_path):
    """The real check the shared predicate exists for: a Glue table under Lake
    Formation beside a Lakekeeper table. An LF resource naming the Lakekeeper
    table's (absent) Glue table fails ``Reference to undeclared resource``."""
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    contract = _contract(
        _expose("parquet", expose_id="facts", table="facts", governance=_lake_formation()),
        _expose("iceberg", "lakekeeper"),
    )
    # The LF-tag the parquet table is associated with must be defined.
    contract["governance"] = {"lakeFormation": {"tagDefinitions": {"tier": ["gold"]}}}
    (tmp_path / "main.tf.json").write_text(build_module(_aws(), contract))
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout
