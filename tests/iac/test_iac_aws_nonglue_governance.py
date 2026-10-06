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

"""A row filter or a column mask on a non-Glue AWS binding is refused, never dropped.

The AWS emitter enforces ``policy.authz.rowFilters`` and ``columnRestrictions`` (a deny
or a mask) only through Lake Formation: a data cells filter, an excluded column or a
protected view, all on a Glue-catalog table. ``_emit_lakeformation`` returns early for
any other format (Redshift, Kinesis, ...) and for a binding with no
``location.database``, so a filter that nothing refused was written nowhere: the
module held the Redshift namespace and workgroup and no filter, and ``fluid apply``
reported success.

Column restrictions (deny and mask) were already refused on such a binding
(``column-restriction-unenforceable``); row filters were not. These pin both, on each
command that can surface the refusal: the emitter (``fluid generate iac`` and
``fluid apply``) and ``fluid validate``, which must report it at stage 2 with the same
message.
"""

from __future__ import annotations

import argparse
import copy
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine, generate_iac
from fluid_build.cli._common import CLIError
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers.aws import (
    _GLUE_CATALOG_FORMATS,
    lf_column_exclusions,
    lf_governance,
    lf_masked_views,
    lf_row_filters,
)

ACCOUNT = "123456789012"
ANALYSTS = "group:analysts@northwind.example"
STEWARDS = "group:data-stewards@northwind.example"
ANALYST_ARN = f"arn:aws:iam::{ACCOUNT}:role/analyst"
STEWARD_ARN = f"arn:aws:iam::{ACCOUNT}:role/steward"
DEFINER_ARN = f"arn:aws:iam::{ACCOUNT}:role/view-definer"
PRINCIPALS = {ANALYSTS: ANALYST_ARN, STEWARDS: STEWARD_ARN}

CONSENT = {"principal": ANALYSTS, "name": "analysts_consented", "where": "consent = true"}
MASK_MSISDN = {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask", "mask": "last_four"}
DENY_ID = {"principal": ANALYSTS, "columns": ["customer_id"], "access": "deny"}
SCHEMA = [
    {"name": "customer_id", "type": "string"},
    {"name": "msisdn", "type": "string"},
    {"name": "consent", "type": "boolean"},
]

# The location of each format as the existing emitter tests write it, with the
# ``database`` and ``table`` a binding needs for the old guard (``not loc.get("table")``)
# to let it through to the silent drop.
GLUE = {
    "database": "silver",
    "table": "profile",
    "bucket": "nw-silver",
    "path": "silver/profile/",
}
NON_GLUE_BINDINGS = {
    # The reported repro: a Redshift Serverless namespace + workgroup.
    "redshift_serverless": {
        "namespace": "nw_ns",
        "workgroup": "nw_wg",
        "database": "dev",
        "table": "profile",
    },
    "redshift_table": {
        "namespace": "nw_ns",
        "workgroup": "nw_wg",
        "database": "dev",
        "table": "profile",
    },
    "redshift_external_schema": {
        "workgroup": "nw_wg",
        "database": "dev",
        "table": "profile",
        "external_schema": "ext_silver",
        "glue_database": "silver",
        "iam_role_arn": f"arn:aws:iam::{ACCOUNT}:role/spectrum",
    },
    "kinesis_stream": {"stream": "profile", "database": "silver", "table": "profile"},
}


def _contract(
    fmt: Optional[str],
    location: Dict[str, Any],
    *,
    restrictions: Optional[List[Dict[str, Any]]] = None,
    row_filters: Optional[List[Dict[str, Any]]] = None,
    masked_views: bool = True,
    grants: bool = True,
) -> Dict[str, Any]:
    authz: Dict[str, Any] = {}
    if restrictions is not None:
        authz["columnRestrictions"] = restrictions
    if row_filters is not None:
        authz["rowFilters"] = row_filters
    lake_formation: Dict[str, Any] = {
        "registerLocation": True,
        "grants": [
            {"principal": STEWARD_ARN, "permissions": ["SELECT", "DESCRIBE"]},
            {"principal": ANALYST_ARN, "permissions": ["SELECT", "DESCRIBE"]},
        ],
    }
    if masked_views:
        lake_formation["maskedViews"] = {
            "definer": DEFINER_ARN,
            "validationConnection": "views-conn",
        }
    binding: Dict[str, Any] = {
        "platform": "aws",
        "location": copy.deepcopy(location),
        "principals": dict(PRINCIPALS),
    }
    if fmt is not None:
        binding["format"] = fmt
    if grants:
        binding["governance"] = {"lakeFormation": lake_formation}
    return {
        "fluidVersion": "0.7.6",
        "id": "silver.profile",
        "exposes": [
            {
                "exposeId": "profile",
                "binding": binding,
                "contract": {"schema": copy.deepcopy(SCHEMA)},
                "policy": {"authz": authz},
            }
        ],
    }


def _refusal(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        get_iac_plugin("aws").emit(contract)
    return raised.value


def _without(location: Dict[str, Any], key: str) -> Dict[str, Any]:
    return {k: v for k, v in location.items() if k != key}


def _analyst_grant_describe_only(contract: Dict[str, Any]) -> Dict[str, Any]:
    grants = contract["exposes"][0]["binding"]["governance"]["lakeFormation"]["grants"]
    grants[1]["permissions"] = ["DESCRIBE"]
    return contract


# ── the emitter: `fluid generate iac`, `fluid apply` ─────────────────────


class TestTheEmitterRefusesWhatItWouldDrop:
    @pytest.mark.parametrize("fmt", sorted(NON_GLUE_BINDINGS))
    def test_a_row_filter_on_a_non_glue_binding_is_refused_not_dropped(self, fmt):
        """The repro. Before the fix this emitted the Redshift pair and no filter."""
        contract = _contract(fmt, NON_GLUE_BINDINGS[fmt], row_filters=[CONSENT])
        with pytest.raises(UnsupportedBindingError) as raised:
            resources = get_iac_plugin("aws").emit(contract)
            pytest.fail(
                f"a rowFilter on a {fmt} binding was dropped: the module holds "
                f"{sorted(resources)} and no data cells filter, no refusal"
            )
        assert raised.value.kind == "row-filter-unenforceable"
        message = str(raised.value)
        assert "policy.authz.rowFilters" in message
        assert fmt in message, "the refusal names the format that cannot carry the filter"
        assert raised.value.remediation, "a refusal says what to do instead"

    def test_the_refusal_lists_the_formats_that_can_carry_a_filter(self):
        """A ``glue_table`` author is told which formats work, not to "use a Glue table"."""
        error = _refusal(
            _contract("glue_table", NON_GLUE_BINDINGS["kinesis_stream"], row_filters=[CONSENT])
        )
        assert error.kind == "row-filter-unenforceable"
        message = str(error)
        for fmt in _GLUE_CATALOG_FORMATS:
            assert fmt in message, f"{fmt} is a format the filter could be moved to"
        assert "It would not be enforced." in message
        (remedy,) = error.remediation
        assert remedy.endswith("."), "one sentence, as the other refusals give"

    @pytest.mark.parametrize("missing", ["database", "table"])
    def test_a_row_filter_on_a_glue_format_with_no_glue_table_is_refused(self, missing):
        """``_emit_lakeformation`` returns early with no database too: the same drop."""
        contract = _contract("parquet", _without(GLUE, missing), row_filters=[CONSENT])
        error = _refusal(contract)
        assert error.kind == "row-filter-unenforceable"
        assert "names no Glue-catalog table" in str(error)

    def test_the_same_filter_on_a_glue_table_is_still_enforced(self):
        """The control: the refusal is about the format, not about the filter."""
        res = get_iac_plugin("aws").emit(_contract("parquet", GLUE, row_filters=[CONSENT]))
        (flt,) = res["aws_lakeformation_data_cells_filter"].values()
        assert flt["table_data"][0]["row_filter"] == [{"filter_expression": "consent = true"}]

    @pytest.mark.parametrize("fmt", sorted(_GLUE_CATALOG_FORMATS) + ["PARQUET", None], ids=str)
    def test_every_glue_catalog_format_still_enforces_the_filter(self, fmt):
        """No format the emitter writes a filter for is refused: case-blind, parquet default."""
        res = get_iac_plugin("aws").emit(_contract(fmt, GLUE, row_filters=[CONSENT]))
        assert len(res.get("aws_lakeformation_data_cells_filter") or {}) == 1, sorted(res)

    @pytest.mark.parametrize("fmt", sorted(NON_GLUE_BINDINGS))
    @pytest.mark.parametrize("restriction", [MASK_MSISDN, DENY_ID], ids=["mask", "deny"])
    def test_a_column_restriction_on_a_non_glue_binding_is_refused_not_dropped(
        self, fmt, restriction
    ):
        """Pinned, not new: a mask is a column restriction, and these are refused already.

        ``lf_column_exclusions`` runs first and refuses any restriction on a non-Glue
        binding, so ``column-mask-unenforceable`` cannot fire there and the kind stays
        ``column-restriction-unenforceable`` (a stable tag log parsers key on).
        """
        contract = _contract(fmt, NON_GLUE_BINDINGS[fmt], restrictions=[restriction])
        error = _refusal(contract)
        assert error.kind == "column-restriction-unenforceable"
        assert fmt in str(error)

    def test_a_filter_and_a_mask_on_a_non_glue_binding_is_refused(self):
        """Column restrictions are derived first, so their refusal is the one raised."""
        contract = _contract(
            "redshift_serverless",
            NON_GLUE_BINDINGS["redshift_serverless"],
            restrictions=[MASK_MSISDN],
            row_filters=[CONSENT],
        )
        assert _refusal(contract).kind == "column-restriction-unenforceable"

    def test_a_non_glue_binding_with_no_filter_and_no_restriction_still_emits(self):
        """Redshift compute with Lake Formation grants and no policy to drop is unchanged."""
        res = get_iac_plugin("aws").emit(
            _contract("redshift_serverless", NON_GLUE_BINDINGS["redshift_serverless"])
        )
        assert "aws_redshiftserverless_namespace" in res
        assert "aws_redshiftserverless_workgroup" in res


class TestOneDerivationForBoth:
    def test_lf_governance_is_the_three_derivations_in_the_emitters_order(self):
        """What ``fluid validate`` runs is what the emitter writes from."""
        contract = _contract("parquet", GLUE, restrictions=[MASK_MSISDN], row_filters=[CONSENT])
        exposure = contract["exposes"][0]
        binding = exposure["binding"]
        exclusions = lf_column_exclusions(exposure, binding, 0)
        row_filters = lf_row_filters(exposure, binding, 0)
        views = lf_masked_views(exposure, binding, 0, exclusions or {}, row_filters)
        assert lf_governance(exposure, binding, 0) == (exclusions, row_filters, views)
        # The filter reaches the masked principal's protected view as its WHERE.
        (view,) = views
        assert view.sql.endswith(" WHERE consent = true")


# ── the commands: `fluid generate iac` and `fluid apply` exit 1, write nothing ─


class TestTheCommandSurfacesTheRefusal:
    def test_generate_iac_exits_one_with_an_unsupported_binding_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(generate_iac, "native_actions", lambda contract, logger, **_: [])
        contract = _contract(
            "redshift_serverless",
            NON_GLUE_BINDINGS["redshift_serverless"],
            row_filters=[CONSENT],
        )
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        out = tmp_path / "iac"
        args = argparse.Namespace(contract=str(path), provider="auto", out=str(out), env=None)
        with pytest.raises(CLIError) as exc:
            generate_iac.run(args, logging.getLogger("test"))
        assert exc.value.exit_code == 1
        assert exc.value.event == "unsupported_binding"
        assert exc.value.context["kind"] == "row-filter-unenforceable"
        assert not (out / "main.tf.json").exists(), "nothing is written for a later apply"

    def test_apply_refuses_before_any_module_reaches_tofu(self, monkeypatch: pytest.MonkeyPatch):
        """``fluid apply`` (and ``fluid diff``'s state check) emit through ``emit_module``."""
        monkeypatch.setattr(
            _apply_opentofu_engine, "native_actions", lambda contract, logger, **_: []
        )
        contract = _contract(
            "redshift_serverless",
            NON_GLUE_BINDINGS["redshift_serverless"],
            row_filters=[CONSENT],
        )
        with pytest.raises(CLIError) as exc:
            _apply_opentofu_engine.emit_module(
                get_iac_plugin("aws"),
                contract,
                SimpleNamespace(backend=None),
                logging.getLogger("test"),
            )
        assert exc.value.exit_code == 1
        assert exc.value.event == "unsupported_binding"
        assert exc.value.context["kind"] == "row-filter-unenforceable"


# ── `fluid validate`: stage 2 reports what the emitter refuses ───────────


#: Every refusal ``lf_governance`` can raise for an aws expose, as a contract.
_REFUSED: Dict[str, Callable[[], Dict[str, Any]]] = {
    **{
        f"row-filter-on-{fmt}": (
            lambda fmt=fmt: _contract(fmt, NON_GLUE_BINDINGS[fmt], row_filters=[CONSENT])
        )
        for fmt in NON_GLUE_BINDINGS
    },
    **{
        f"mask-on-{fmt}": (
            lambda fmt=fmt: _contract(fmt, NON_GLUE_BINDINGS[fmt], restrictions=[MASK_MSISDN])
        )
        for fmt in NON_GLUE_BINDINGS
    },
    "deny-on-kinesis_stream": lambda: _contract(
        "kinesis_stream", NON_GLUE_BINDINGS["kinesis_stream"], restrictions=[DENY_ID]
    ),
    "row-filter-no-database": lambda: _contract(
        "parquet", _without(GLUE, "database"), row_filters=[CONSENT]
    ),
    "row-filter-no-grants": lambda: _contract("parquet", GLUE, row_filters=[CONSENT], grants=False),
    "row-filter-no-read-grant": lambda: _analyst_grant_describe_only(
        _contract("parquet", GLUE, row_filters=[CONSENT])
    ),
    "row-filter-unmapped-principal": lambda: _contract(
        "parquet",
        GLUE,
        row_filters=[{**CONSENT, "principal": "group:auditors@northwind.example"}],
    ),
    "mask-no-masked-views": lambda: _contract(
        "parquet", GLUE, restrictions=[MASK_MSISDN], masked_views=False
    ),
    "text-mask-on-a-boolean": lambda: _contract(
        "parquet", GLUE, restrictions=[{**MASK_MSISDN, "columns": ["consent"]}]
    ),
}


class TestValidateReportsTheSameRefusal:
    """``validate_governance`` ran only ``lf_column_exclusions`` for an aws binding.

    Its docstring says it runs the same derivations as the emitter so each refusal is
    reported at stage 2. ``lf_row_filters`` and ``lf_masked_views`` were never run, so
    ``fluid validate`` passed every contract those refuse at ``generate iac``.
    """

    @pytest.mark.parametrize("fmt", sorted(NON_GLUE_BINDINGS))
    def test_a_row_filter_on_a_non_glue_binding_fails_validate(self, fmt):
        contract = _contract(fmt, NON_GLUE_BINDINGS[fmt], row_filters=[CONSENT])
        errors, _warnings = validate_governance(contract)
        assert any("policy.authz.rowFilters" in e for e in errors), errors

    def test_validate_and_the_emitter_refuse_a_mask_with_no_view_definer_alike(self):
        """Not specific to non-Glue: the drift this item's fix must not widen."""
        contract = _contract("parquet", GLUE, restrictions=[MASK_MSISDN], masked_views=False)
        assert _refusal(contract).kind == "column-mask-unenforceable"
        errors, _warnings = validate_governance(contract)
        assert any("maskedViews" in e for e in errors), errors

    def test_validate_and_the_emitter_refuse_a_row_filter_with_no_grant_alike(self):
        contract = _contract("parquet", GLUE, row_filters=[CONSENT], grants=False)
        assert _refusal(contract).kind == "row-filter-unenforceable"
        errors, _warnings = validate_governance(contract)
        assert any("policy.authz.rowFilters" in e for e in errors), errors

    @pytest.mark.parametrize("case", sorted(_REFUSED))
    def test_validate_reports_every_emitter_refusal_with_its_message(self, case):
        """Stage 2 and the emitter cannot disagree: the same refusal, word for word."""
        contract = _REFUSED[case]()
        error = _refusal(contract)
        expected = f"{error} {' '.join(error.remediation)}".strip()
        errors, _warnings = validate_governance(contract)
        assert errors == [expected], f"{error.kind}: {errors}"

    def test_a_name_the_masked_view_cannot_quote_fails_validate_and_ends_no_check(self):
        """``validate_ident`` raises a plain ``ValueError``; it must not end the whole check.

        ``fluid validate`` skips the governance check on any exception it raises, so the
        other expose's refusal went unreported and the contract passed stage 2.
        """
        masked = _contract(
            "parquet", {**GLUE, "database": "silver-zone"}, restrictions=[MASK_MSISDN]
        )
        with pytest.raises(ValueError, match="silver-zone"):
            get_iac_plugin("aws").emit(masked)
        contract = _contract(
            "redshift_serverless", NON_GLUE_BINDINGS["redshift_serverless"], row_filters=[CONSENT]
        )
        contract["exposes"].append({**masked["exposes"][0], "exposeId": "masked"})
        errors, _warnings = validate_governance(contract)
        assert len(errors) == 2, errors
        assert "policy.authz.rowFilters" in errors[0]
        assert errors[1] == "exposes[masked]: Invalid SQL identifier: 'silver-zone'"

    def test_a_glue_contract_the_emitter_writes_validates_clean(self):
        """The counterpart: a filter, a mask and a deny the emitter enforces pass stage 2."""
        contract = _contract(
            "parquet", GLUE, restrictions=[MASK_MSISDN, DENY_ID], row_filters=[CONSENT]
        )
        resources = get_iac_plugin("aws").emit(contract)
        assert resources["aws_lakeformation_data_cells_filter"]
        assert validate_governance(contract) == ([], [])


# ── the real `fluid validate` command ────────────────────────────────────


_CLI_CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: silver.profile
name: Profile
description: A profile with a row filter
domain: customer
metadata:
  layer: Silver
  owner:
    team: data-platform
    email: dp@northwind.example
exposes:
  - exposeId: profile
    kind: table
    title: Profile
    version: "1.0.0"
    binding:
      platform: aws
      format: {fmt}
      location:
{location}
      principals:
        "{analysts}": "{analyst_arn}"
      governance:
        lakeFormation:
          grants:
            - principal: "{analyst_arn}"
              permissions: [SELECT, DESCRIBE]
    contract:
      schema:
        - {{name: customer_id, type: string}}
        - {{name: consent, type: boolean}}
    policy:
      authz:
        rowFilters:
          - principal: "{analysts}"
            name: analysts_consented
            where: "consent = true"
"""


class TestTheValidateCommand:
    def _validate(self, tmp_path: Path, fmt: str, location: Dict[str, Any]):
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(
            _CLI_CONTRACT.format(
                fmt=fmt,
                location="\n".join(f"        {k}: {v}" for k, v in location.items()),
                analysts=ANALYSTS,
                analyst_arn=ANALYST_ARN,
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "validate", str(path)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    def test_the_redshift_repro_fails_fluid_validate(self, tmp_path):
        """It printed "Valid FLUID contract" and exited 0, then applied with no filter."""
        result = self._validate(
            tmp_path, "redshift_serverless", NON_GLUE_BINDINGS["redshift_serverless"]
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "filters rows" in result.stdout
        assert "redshift_serverless" in result.stdout

    def test_the_same_filter_on_a_glue_table_passes_fluid_validate(self, tmp_path):
        result = self._validate(tmp_path, "parquet", GLUE)
        assert result.returncode == 0, result.stdout + result.stderr
