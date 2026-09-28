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

"""``policy.authz.columnRestrictions`` on AWS: Lake Formation's excluded columns.

The base contract's column restriction is the same field the GCP emitter turns
into policy tags; on AWS each Lake Formation grant with ``SELECT`` excludes the
restricted columns its principal may not read. The overlay's hand-written
``governance.lakeFormation.grants[].excludedColumns`` keeps working: with no
restriction it is emitted as before, and with one it must agree.

The rendering tests need nothing. The moto tests run a real ``tofu plan`` /
``apply`` (moto's ``ThreadedMotoServer`` for S3, Glue, STS and Lake Formation, no
account, no credentials) and ``fluid verify``'s Lake Formation check against what
the apply granted, then against a grant made outside the contract. moto stores
Lake Formation permissions but enforces none of them, so that a denied principal's
Athena query is refused rests on the Lake Formation documentation.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.cli._verify_lf_columns import column_restrictions_dimension
from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.column_access import lf_expected_exclusions
from fluid_build.iac.credentials import build_tofu_env
from fluid_build.iac.governance_validation import validate_governance

ACCOUNT = "123456789012"  # moto's
REGION = "us-east-1"
STEWARD = f"arn:aws:iam::{ACCOUNT}:role/fluid-demo-lab-steward"
ANALYST = f"arn:aws:iam::{ACCOUNT}:role/fluid-demo-lab-analyst"
LOGICAL_ANALYSTS = "group:analysts@northwind.example"
DENY = [{"principal": LOGICAL_ANALYSTS, "columns": ["customer_id", "msisdn"], "access": "deny"}]


def _contract(
    *,
    restrictions: Optional[List[Dict[str, Any]]] = None,
    principals: Optional[Dict[str, Any]] = None,
    analyst_excluded: Optional[List[str]] = None,
    analyst_columns: Optional[List[str]] = None,
    lake_formation: bool = True,
    fmt: str = "parquet",
) -> Dict[str, Any]:
    analyst: Dict[str, Any] = {"principal": ANALYST, "permissions": ["SELECT", "DESCRIBE"]}
    if analyst_excluded is not None:
        analyst["excludedColumns"] = analyst_excluded
    if analyst_columns is not None:
        analyst["columns"] = analyst_columns
    binding: Dict[str, Any] = {
        "platform": "aws",
        "format": fmt,
        "location": {
            "database": "demo_gold",
            "table": "retention_candidates",
            "bucket": "cr-moto-lake",
            "path": "gold/retention_candidates/",
        },
    }
    if lake_formation:
        binding["governance"] = {
            "lakeFormation": {
                "registerLocation": True,
                "grants": [
                    {"principal": STEWARD, "permissions": ["SELECT", "DESCRIBE"]},
                    analyst,
                ],
            }
        }
    if principals is not None:
        binding["principals"] = principals
    exposure: Dict[str, Any] = {
        "exposeId": "candidates",
        "binding": binding,
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "string"},
                {"name": "status", "type": "string"},
                {"name": "msisdn", "type": "string"},
            ]
        },
    }
    if restrictions is not None:
        exposure["policy"] = {"authz": {"columnRestrictions": restrictions}}
    return {"fluidVersion": "0.7.6", "id": "cr.moto", "exposes": [exposure]}


def _grants(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    res = get_iac_plugin("aws").emit(contract)
    return {g["principal"]: g for g in res["aws_lakeformation_permissions"].values()}


def _excluded(grant: Dict[str, Any]) -> Optional[List[str]]:
    twc = grant.get("table_with_columns")
    return twc[0].get("excluded_column_names") if twc else None


def _refusal(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        get_iac_plugin("aws").emit(contract)
    return raised.value


MAPPED = {LOGICAL_ANALYSTS: ANALYST}


class TestRendering:
    def test_a_restriction_becomes_the_grants_excluded_columns(self):
        grants = _grants(_contract(restrictions=DENY, principals=MAPPED))
        assert _excluded(grants[ANALYST]) == ["customer_id", "msisdn"]
        # A derived exclusion makes the grant column-limited, so it carries SELECT
        # alone like a hand-written one (#679): Lake Formation refuses DESCRIBE to
        # a principal holding a partial SELECT.
        assert grants[ANALYST]["permissions"] == ["SELECT"]
        # The steward may read everything: a plain table grant, as before.
        assert "table" in grants[STEWARD] and "table_with_columns" not in grants[STEWARD]
        assert grants[STEWARD]["permissions"] == ["SELECT", "DESCRIBE"]

    def test_the_hand_written_exclusion_keeps_working_without_a_restriction(self):
        grants = _grants(_contract(analyst_excluded=["customer_id", "msisdn"]))
        assert _excluded(grants[ANALYST]) == ["customer_id", "msisdn"]

    def test_both_present_and_agreeing_emit_once(self):
        grants = _grants(
            _contract(
                restrictions=DENY, principals=MAPPED, analyst_excluded=["msisdn", "customer_id"]
            )
        )
        assert sorted(_excluded(grants[ANALYST]) or []) == ["customer_id", "msisdn"]

    @pytest.mark.parametrize("excluded", [["msisdn"], ["customer_id", "msisdn", "status"], []])
    def test_both_present_and_disagreeing_is_refused(self, excluded):
        error = _refusal(_contract(restrictions=DENY, principals=MAPPED, analyst_excluded=excluded))
        assert error.kind == "column-restriction-conflict"
        assert "must agree" in str(error)

    def test_a_projection_that_reaches_a_restricted_column_is_refused(self):
        error = _refusal(
            _contract(restrictions=DENY, principals=MAPPED, analyst_columns=["status", "msisdn"])
        )
        assert error.kind == "column-restriction-conflict"

    def test_a_projection_that_avoids_them_is_kept(self):
        grants = _grants(
            _contract(restrictions=DENY, principals=MAPPED, analyst_columns=["status"])
        )
        assert grants[ANALYST]["table_with_columns"][0]["column_names"] == ["status"]

    def test_an_allow_list_excludes_the_column_for_everyone_else(self):
        restrictions = [{"principal": "steward", "columns": ["msisdn"], "access": "allow"}]
        grants = _grants(_contract(restrictions=restrictions, principals={"steward": STEWARD}))
        assert _excluded(grants[ANALYST]) == ["msisdn"]
        assert "table" in grants[STEWARD]

    def test_a_restriction_may_name_an_arn_without_a_mapping(self):
        restrictions = [{"principal": ANALYST, "columns": ["msisdn"], "access": "deny"}]
        grants = _grants(_contract(restrictions=restrictions))
        assert _excluded(grants[ANALYST]) == ["msisdn"]

    def test_an_unmapped_logical_principal_is_refused(self):
        assert _refusal(_contract(restrictions=DENY)).kind == "principal-invalid"
        error = _refusal(_contract(restrictions=DENY, principals={"someone-else": ANALYST}))
        assert error.kind == "principal-unmapped"

    def test_a_restriction_nothing_enforces_is_refused(self):
        error = _refusal(_contract(restrictions=DENY, principals=MAPPED, lake_formation=False))
        assert error.kind == "column-restriction-unenforceable"

    def test_a_restriction_on_a_non_glue_binding_is_refused(self):
        contract = _contract(restrictions=DENY, principals=MAPPED, fmt="kinesis_stream")
        assert _refusal(contract).kind == "column-restriction-unenforceable"

    def test_a_restriction_on_a_binding_with_no_glue_table_is_refused(self):
        contract = _contract(restrictions=DENY, principals=MAPPED)
        del contract["exposes"][0]["binding"]["location"]["table"]
        assert _refusal(contract).kind == "column-restriction-unenforceable"
        errors, _ = validate_governance(contract)
        assert any("names no Glue table" in e for e in errors), errors

    # A grant the restrictions leave with no column. Hand-written as
    # excludedColumns it was refused ("excludes every column of the table"), but
    # derived from columnRestrictions it was emitted: a column wildcard that
    # excludes every column (measured on the integration with 0.16.6's #675).
    ALL_COLUMNS = ["customer_id", "status", "msisdn"]

    @pytest.mark.parametrize(
        "restrictions, principals",
        [
            ([{"principal": LOGICAL_ANALYSTS, "columns": ALL_COLUMNS, "access": "deny"}], MAPPED),
            ([{"principal": STEWARD, "columns": ALL_COLUMNS, "access": "allow"}], None),
        ],
        ids=["denied-every-column", "allowed-to-someone-else-only"],
    )
    def test_restrictions_that_leave_a_reader_no_column_are_refused(self, restrictions, principals):
        contract = _contract(restrictions=restrictions, principals=principals)
        error = _refusal(contract)
        assert error.kind == "lakeformation-grant-columns"
        assert f"grants[1] gives {ANALYST} read access" in str(error)
        assert "read no column of the table" in str(error)
        errors, _ = validate_governance(contract)
        assert any("read no column of the table" in e for e in errors), errors

    def test_restrictions_that_leave_a_reader_one_column_still_emit(self):
        deny = [{"principal": LOGICAL_ANALYSTS, "columns": self.ALL_COLUMNS[:2], "access": "deny"}]
        grants = _grants(_contract(restrictions=deny, principals=MAPPED))
        twc = grants[ANALYST]["table_with_columns"][0]
        assert twc["wildcard"] is True
        assert twc["excluded_column_names"] == ["customer_id", "status"]

    def test_the_grant_check_reads_the_derived_exclusions(self):
        from fluid_build.iac.providers.aws import _check_lf_grant_columns

        contract = _contract()
        exposure = contract["exposes"][0]
        binding = exposure["binding"]
        schema = exposure["contract"]["schema"]
        with pytest.raises(UnsupportedBindingError) as raised:
            _check_lf_grant_columns(
                binding, binding["location"], "parquet", schema, {1: tuple(self.ALL_COLUMNS)}
            )
        assert "excludes every column of the table" in str(raised.value)
        with pytest.raises(UnsupportedBindingError) as raised:
            _check_lf_grant_columns(binding, binding["location"], "parquet", schema, {1: ("x",)})
        assert "['x']" in str(raised.value)

    def test_fluid_validate_reports_the_refusal(self):
        errors, _ = validate_governance(
            _contract(restrictions=DENY, principals=MAPPED, analyst_excluded=["msisdn"])
        )
        assert any("must agree" in e for e in errors)

    def test_verify_holds_every_denied_principal_and_iam_allowed_principals(self):
        exposure = _contract(restrictions=DENY, principals=MAPPED)["exposes"][0]
        expected = lf_expected_exclusions(exposure, exposure["binding"])
        assert expected == {
            ANALYST: ("customer_id", "msisdn"),
            "IAM_ALLOWED_PRINCIPALS": ("customer_id", "msisdn"),
        }


# ── against moto: a real tofu plan / apply, and verify's LF check ────────


def _have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


_SKIP = runner.tofu_path() is None or not _have_moto_server()
_SKIP_REASON = "needs `tofu` on PATH + moto server extra (pip install 'moto[glue,server]')"


@pytest.fixture
def moto_endpoint() -> Iterator[str]:
    import urllib.request

    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        reset = urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")
        with urllib.request.urlopen(reset, timeout=30) as answered:  # noqa: S310 — local moto
            assert answered.status == 200
        yield endpoint
    finally:
        server.stop()


@pytest.fixture(scope="module")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    env = {k: v for k, v in build_tofu_env().items() if not k.startswith("AWS_")}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(tmp_path_factory.mktemp("tofu-plugin-cache")))
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _tofu(workdir: Path, env: Dict[str, str], *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(runner.tofu_path()), *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _write(contract: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), contract))
    services = ("s3", "sts", "iam", "glue", "lakeformation", "kms")
    provider = {
        "provider": {
            "aws": {
                "region": REGION,
                "access_key": "testing",
                "secret_key": "testing",  # pragma: allowlist secret — moto dummy
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in services},
            }
        }
    }
    (workdir / "provider.tf.json").write_text(json.dumps(provider))


def _init(workdir: Path, env: Dict[str, str]) -> None:
    for attempt in range(4):
        done = _tofu(workdir, env, "init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"


def _plan(workdir: Path, env: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    done = _tofu(workdir, env, "plan", "-input=false", "-no-color", "-out=plan.bin")
    assert done.returncode == 0, f"tofu plan failed:\n{done.stdout}\n{done.stderr}"
    shown = _tofu(workdir, env, "show", "-json", "plan.bin")
    return {c["address"]: c for c in json.loads(shown.stdout)["resource_changes"]}


def _planned_grant(planned: Dict[str, Dict[str, Any]], principal: str) -> Dict[str, Any]:
    return next(
        c["change"]["after"]
        for c in planned.values()
        if c["type"] == "aws_lakeformation_permissions"
        and c["change"]["after"]["principal"] == principal
    )


def _boto(service: str, endpoint: str) -> Any:
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106  # pragma: allowlist secret — moto dummy
        region_name=REGION,
    )


def _check(contract: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
    exposure = contract["exposes"][0]
    dimension = column_restrictions_dimension(
        "candidates",
        exposure,
        exposure["binding"],
        region=REGION,
        factory=lambda service, _region: _boto(service, endpoint),
    )
    assert dimension is not None
    return dimension


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
@pytest.mark.parametrize("source", ["restriction", "hand-written"])
def test_the_excluded_columns_plan_through_the_provider(tmp_path, moto_endpoint, tofu_env, source):
    """A real ``tofu plan`` accepts the grant, from the restriction or the overlay.

    The overlay's own ``excludedColumns`` failed this plan before ("one of
    column_names, wildcard must be specified"): the provider needs ``wildcard``
    beside ``excluded_column_names``, and the emitter never wrote it.
    """
    contract = (
        _contract(restrictions=DENY, principals=MAPPED)
        if source == "restriction"
        else _contract(analyst_excluded=["customer_id", "msisdn"])
    )
    _write(contract, tmp_path, moto_endpoint)
    _init(tmp_path, tofu_env)
    planned = _plan(tmp_path, tofu_env)
    twc = _planned_grant(planned, ANALYST)["table_with_columns"][0]
    assert sorted(twc["excluded_column_names"]) == ["customer_id", "msisdn"]
    assert twc["wildcard"] is True
    steward = _planned_grant(planned, STEWARD)
    assert steward["table"] and not steward.get("table_with_columns")


def _lf_table(**columns: Any) -> Dict[str, Any]:
    ref: Dict[str, Any] = {"DatabaseName": "demo_gold", "Name": "retention_candidates"}
    if not columns:
        return {"Table": ref}
    return {"TableWithColumns": {**ref, **columns}}


def _grant_as_apply_would(lf: Any) -> None:
    """The two grants the emitted module makes, made through the API."""
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": STEWARD},
        Resource=_lf_table(),
        Permissions=["SELECT", "DESCRIBE"],
    )
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": ANALYST},
        Resource=_lf_table(ColumnWildcard={"ExcludedColumnNames": ["customer_id", "msisdn"]}),
        Permissions=["SELECT"],
    )


@pytest.mark.skipif(not _have_moto_server(), reason=_SKIP_REASON)
def test_verify_reads_lake_formation_and_fails_on_a_grant_outside_the_contract(moto_endpoint):
    """moto's Lake Formation stores grants; verify reads them as the real API returns them.

    (moto cannot read back a column-level grant for the provider, so the grants
    here are made through the API directly, as the apply would.)
    """
    contract = _contract(restrictions=DENY, principals=MAPPED)
    lf = _boto("lakeformation", moto_endpoint)
    _grant_as_apply_would(lf)
    passed = _check(contract, moto_endpoint)
    assert passed["status"] == "pass", passed
    assert set(passed["checked"]) == {ANALYST, "IAM_ALLOWED_PRINCIPALS"}

    # A column list reaching a denied column, granted outside the contract.
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": ANALYST},
        Resource=_lf_table(ColumnNames=["status", "msisdn"]),
        Permissions=["SELECT"],
    )
    failed = _check(contract, moto_endpoint)
    assert failed["status"] == "fail"
    assert f"{ANALYST} can read msisdn" in failed["message"]


@pytest.mark.skipif(not _have_moto_server(), reason=_SKIP_REASON)
def test_verify_fails_when_the_table_still_grants_iam_allowed_principals(moto_endpoint):
    """Lake Formation's default for a new table lets any IAM principal read every column."""
    contract = _contract(restrictions=DENY, principals=MAPPED)
    lf = _boto("lakeformation", moto_endpoint)
    _grant_as_apply_would(lf)
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
        Resource=_lf_table(),
        Permissions=["ALL"],
    )
    failed = _check(contract, moto_endpoint)
    assert failed["status"] == "fail"
    assert "IAM_ALLOWED_PRINCIPALS can read customer_id, msisdn" in failed["message"]


@pytest.mark.skipif(not _have_moto_server(), reason=_SKIP_REASON)
def test_verify_errors_when_it_cannot_see_the_contracts_own_grants(moto_endpoint):
    """A caller that sees only part of the permissions must not read as a pass."""
    contract = _contract(restrictions=DENY, principals=MAPPED)
    lf = _boto("lakeformation", moto_endpoint)
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": ANALYST},
        Resource=_lf_table(ColumnWildcard={"ExcludedColumnNames": ["customer_id", "msisdn"]}),
        Permissions=["SELECT"],
    )
    unseen = _check(contract, moto_endpoint)
    assert unseen["status"] == "error"
    assert STEWARD in unseen["message"] and "Lake Formation administrator" in unseen["message"]


class _ListedPermissions:
    """A Lake Formation client whose ListPermissions returns what it is given."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries

    def list_permissions(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"PrincipalResourcePermissions": self.entries}


def _entry(principal: str, resource: Dict[str, Any], *permissions: str) -> Dict[str, Any]:
    return {
        "Principal": {"DataLakePrincipalIdentifier": principal},
        "Resource": resource,
        "Permissions": list(permissions or ("SELECT",)),
    }


def test_a_database_wide_select_to_a_denied_principal_fails_verify():
    """``Table: {TableWildcard: {}}`` reaches every column of every table in the database."""
    contract = _contract(restrictions=DENY, principals=MAPPED)
    exposure = contract["exposes"][0]
    own = [
        _entry(STEWARD, _lf_table(), "SELECT", "DESCRIBE"),
        _entry(
            ANALYST, _lf_table(ColumnWildcard={"ExcludedColumnNames": ["customer_id", "msisdn"]})
        ),
    ]

    def check(extra: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = _ListedPermissions(own + extra)
        dimension = column_restrictions_dimension(
            "candidates", exposure, exposure["binding"], region=REGION, factory=lambda *_: client
        )
        assert dimension is not None
        return dimension

    assert check([])["status"] == "pass"
    wildcard = {"Table": {"DatabaseName": "demo_gold", "TableWildcard": {}}}
    failed = check([_entry(ANALYST, wildcard)])
    assert failed["status"] == "fail"
    assert f"{ANALYST} can read customer_id, msisdn" in failed["message"]
    elsewhere = {"Table": {"DatabaseName": "other_db", "TableWildcard": {}}}
    assert check([_entry(ANALYST, elsewhere)])["status"] == "pass"
