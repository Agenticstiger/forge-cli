# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid doctor --contract``: the offline capability preflight and ``--compare``.

Pins, one test group each:

* a BigQuery column the emitter refuses (a bare ``array``) is ``unsupported`` before
  ``generate iac`` runs, and ``generate iac`` refuses the same column;
* the catalog-only AWS sibling needs no AWS credentials, SDK or tool for its offline
  stages, and names the identity as ``requires-live-proof`` only for ``apply``;
* an offline run fetches nothing and installs nothing, with the network, installers
  and every subprocess but ``--version`` blocked;
* the JSON report is stable (a golden file), and two reports compare to named version
  gaps without one environment value in them;
* ``--live`` is read-only, reclassifies the identity, and prints no identity.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import _preflight as pf
from fluid_build.cli import build_parser

pytestmark = [pytest.mark.unit]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "preflight"
BQ = FIXTURES / "bigquery_nested.fluid.yaml"
AWS = FIXTURES / "aws_catalog.fluid.yaml"
GOLDEN = FIXTURES / "golden_preflight.json"  # not *_apply.json: .gitignore drops those

_REQUIRES = [
    "click>=8.1",
    "boto3>=1.34; extra == 'aws'",
    "botocore>=1.34; extra == 'aws'",
    "google-cloud-bigquery>=3.20; extra == 'gcp'",
    "google-auth>=2.29; extra == 'gcp'",
    "dbt-bigquery<2,>=1.7; python_version >= '3.10' and extra == 'gcp'",
    "duckdb>=1.5.0; extra == 'local'",
]


class FakeProbe(pf.Probe):
    """A machine with fixed versions: the report must not depend on the host."""

    def __init__(
        self,
        dists=None,
        tofu=(True, "1.12.1"),
        which=(),
        environ=None,
        requires=_REQUIRES,
        dbt=(False, None, None),
    ):
        self.dists = dict(
            {"click": "8.1.7", "google-cloud-bigquery": "3.25.0", "google-auth": "2.30.0"}
            if dists is None
            else dists
        )
        self._tofu = tofu
        self._which = set(which)
        self._environ = environ if environ is not None else {}
        self._requires = requires
        self._dbt = dbt

    def forge_version(self):
        return "0.32.0"

    def editable(self):
        return False

    def python_version(self):
        return "3.12.4"

    def platform(self):
        return "linux-x86_64"

    def dist_version(self, name):
        return self.dists.get(name)

    def requires(self, name):
        return list(self._requires) if self._requires is not None else None

    def entry_points(self, group):
        return [("acme", "acme-fluid-plugin", "1.2.0")] if group == "fluid_build.providers" else []

    def tofu(self):
        return self._tofu

    def which(self, name):
        return name in self._which

    def dbt(self, detect):
        found, flavor, version = self._dbt
        return (found, flavor, version) if detect else (found, None, None)

    def environ(self):
        return self._environ


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _report(path=BQ, operation="plan", probe=None, **kwargs):
    return pf.build_report(
        _load(path),
        operation=operation,
        probe=probe or FakeProbe(),
        contract_path=str(path),
        **kwargs,
    )


def _caps(report, cls=None, area=None):
    return [
        c
        for c in report["capabilities"]
        if (cls is None or c["class"] == cls) and (area is None or c["area"] == area)
    ]


def _run_cli(argv, capsys):
    parser = build_parser()
    args = parser.parse_args(["doctor", *argv])
    rc = args.func(args, logging.getLogger("test"))
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# BigQuery: the bare array is unsupported before IaC generation
# ---------------------------------------------------------------------------


def test_bare_array_is_unsupported_before_iac_generation():
    report = _report(operation="plan")
    by_subject = {c["subject"]: c for c in _caps(report, area="column-type")}
    assert by_subject["exposes[events].genres"]["class"] == pf.UNSUPPORTED
    assert by_subject["exposes[events].genres"]["target"] == "bigquery"
    assert "element" in by_subject["exposes[events].genres"]["fix"]
    assert by_subject["exposes[events].attributes"]["class"] == pf.LOSSY
    assert by_subject["exposes[events].tags"]["class"] == pf.EXACT
    assert by_subject["exposes[events].id"]["class"] == pf.EXACT
    assert report["summary"]["verdict"] == "not-ready"
    assert pf.exit_code(report) == 1


def test_preflight_and_generate_iac_refuse_the_same_columns():
    """Preflight reads the emitter's own outcome: whatever it calls unsupported, the
    emitter refuses, and nothing else."""
    from fluid_build.iac import get_iac_plugin
    from fluid_build.iac.base import UnsupportedBindingError

    contract = _load(BQ)
    with pytest.raises(UnsupportedBindingError) as refused:
        get_iac_plugin("gcp").emit(contract)
    unsupported = [c["subject"] for c in _caps(_report(), pf.UNSUPPORTED, "column-type")]
    assert unsupported == ["exposes[events].genres"]
    assert "'genres'" in str(refused.value)
    assert "'attributes'" not in str(refused.value)


def test_column_findings_are_not_reported_twice():
    """validate's column-type messages are judged per column, not repeated whole."""
    report = _report()
    contract_rules = [c for c in report["capabilities"] if c["target"] == "contract"]
    assert not [c for c in contract_rules if "genres" in c["detail"]]
    assert report["validation"]["errors"] >= 1


def test_glue_columns_are_classed_by_the_glue_type_table():
    report = _report(AWS)
    by_subject = {c["subject"]: c for c in _caps(report, area="column-type")}
    assert by_subject["exposes[orders].payload"]["class"] == pf.LOSSY
    assert by_subject["exposes[orders].payload"]["target"] == "glue"
    assert by_subject["exposes[orders].amount"]["class"] == pf.EXACT


def test_validate_and_preflight_judge_the_same_tables():
    from fluid_build.iac.providers.aws import glue_tables, validate_aws_column_types
    from fluid_build.iac.providers.gcp import bigquery_tables, validate_bigquery_column_types

    assert [e for e, _ in bigquery_tables(_load(BQ))] == ["events"]
    assert list(glue_tables(_load(BQ))) == []
    assert [e for e, _ in glue_tables(_load(AWS))] == ["orders"]
    errors, warnings = validate_bigquery_column_types(_load(BQ))
    assert len(errors) == 1 and "genres" in errors[0]
    assert validate_aws_column_types(_load(AWS))[0] == []


def test_a_layout_refusal_is_unsupported():
    """The #973 layout refusal reaches the report through validate's own rule."""
    contract = _load(BQ)
    exposure = contract["exposes"][0]
    columns = ["id", "a", "b", "c", "d"]
    exposure["contract"]["schema"] = [{"name": n, "type": "string"} for n in columns]
    exposure["binding"]["properties"] = {"cluster_by": columns}  # BigQuery takes four
    report = pf.build_report(contract, probe=FakeProbe())
    rules = [c for c in _caps(report, pf.UNSUPPORTED) if c["target"] == "contract"]
    assert [c["area"] for c in rules] == ["governance"], report["capabilities"]
    assert "cluster_by" in rules[0]["detail"]
    assert rules[0]["subject"].startswith("/exposes/0")
    assert pf.exit_code(report) == 1


# ---------------------------------------------------------------------------
# The catalog-only sibling needs no AWS credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["validate", "generate", "plan"])
def test_catalog_only_offline_stages_need_no_aws_credentials(operation, monkeypatch):
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "boto3", None)  # importing it would fail
    monkeypatch.setitem(sys.modules, "botocore", None)
    report = _report(AWS, operation=operation, probe=FakeProbe(dists={}, tofu=(False, None)))
    assert report["needs"] == {"clouds": [], "extras": [], "tools": []}
    assert not _caps(report, area="identity")
    assert not _caps(report, pf.LIVE)
    assert not [c for c in _caps(report, pf.UNSUPPORTED) if c["area"] in ("extra", "tool")]
    assert report["summary"]["verdict"] == "ready"
    assert pf.exit_code(report) == 0


def test_apply_on_the_same_contract_needs_the_aws_identity_live():
    report = _report(
        AWS, operation="apply", probe=FakeProbe(dists={"boto3": "1.35.0", "botocore": "1.35.0"})
    )
    identity = _caps(report, area="identity")
    assert [(c["class"], c["target"]) for c in identity] == [(pf.LIVE, "aws")]
    assert report["needs"]["clouds"] == ["aws"]
    assert report["needs"]["extras"] == ["aws"]
    assert report["needs"]["tools"] == ["tofu"]
    assert report["summary"]["verdict"] == "needs-live-proof"


def test_apply_without_the_extra_or_tofu_is_unsupported():
    report = _report(AWS, operation="apply", probe=FakeProbe(dists={}, tofu=(False, None)))
    unsupported = {(c["area"], c["subject"]) for c in _caps(report, pf.UNSUPPORTED)}
    assert ("extra", "aws") in unsupported
    assert ("tool", "tofu") in unsupported
    fixes = {c["fix"] for c in _caps(report, pf.UNSUPPORTED)}
    assert "pip install 'data-product-forge[aws]'" in fixes


def test_an_old_tofu_is_unsupported_against_the_pin():
    report = _report(AWS, operation="apply", probe=FakeProbe(tofu=(True, "1.11.0")))
    tool = [c for c in _caps(report, pf.UNSUPPORTED) if c["area"] == "tool"]
    assert tool and "1.11.0 does not satisfy >=1.12" in tool[0]["detail"]


def test_ship_names_the_state_backend_scheme_but_not_the_bucket():
    probe = FakeProbe(environ={"FLUID_STATE_BACKEND": "s3://very-private-bucket/state/key.tfstate"})
    report = _report(AWS, operation="ship", probe=probe)
    assert report["state_backend"] == {
        "needed": True,
        "origin": "FLUID_STATE_BACKEND",
        "scheme": "s3",
        "valid": True,
    }
    assert "very-private-bucket" not in pf.dumps(report)
    assert [c["class"] for c in _caps(report, area="state-backend")] == [pf.LIVE]


def test_local_state_for_apply_is_lossy():
    report = _report(AWS, operation="apply")
    assert [c["class"] for c in _caps(report, area="state-backend")] == [pf.LOSSY]


# ---------------------------------------------------------------------------
# Offline: nothing fetched, nothing installed
# ---------------------------------------------------------------------------


@pytest.fixture
def no_network_no_installs(monkeypatch):
    """Every socket, installer and subprocess but a local version query fails the test
    (``tofu --version``; GitPython's import-time ``git version``)."""
    attempts = []
    probes = (["--version"], ["version"])

    def _blocked(*args, **kwargs):
        attempts.append(("network", args[:1]))
        raise AssertionError("preflight touched the network")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    real_run, real_popen = subprocess.run, subprocess.Popen

    def _only_version(argv, *args, **kwargs):
        argv_list = list(argv) if isinstance(argv, (list, tuple)) else [argv]
        if argv_list[-1:] not in probes:
            attempts.append(("subprocess", argv_list))
            raise AssertionError(f"preflight ran {argv_list!r}")
        return real_run(argv, *args, **kwargs)

    def _no_popen(argv, *args, **kwargs):
        argv_list = list(argv) if isinstance(argv, (list, tuple)) else [argv]
        if argv_list[-1:] not in probes:
            attempts.append(("subprocess", argv_list))
            raise AssertionError(f"preflight started {argv_list!r}")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _only_version)
    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    monkeypatch.setattr(subprocess, "check_call", _blocked)
    monkeypatch.setattr(subprocess, "check_output", _blocked)

    import fluid_build.iac.opentofu_install as install

    for name in dir(install):
        if name.startswith(("ensure", "install", "download")) and callable(getattr(install, name)):
            monkeypatch.setattr(install, name, _blocked)
    from fluid_build.schema_manager import FluidSchemaManager

    for name in dir(FluidSchemaManager):
        if "fetch" in name or "remote" in name or "download" in name:
            if callable(getattr(FluidSchemaManager, name)):
                monkeypatch.setattr(FluidSchemaManager, name, _blocked)
    return attempts


@pytest.mark.parametrize("operation", ["validate", "plan", "apply", "ship"])
def test_an_offline_run_fetches_and_installs_nothing(no_network_no_installs, operation):
    """The real probe on this machine: a missing tool is reported, never provisioned."""
    report = pf.build_report(_load(BQ), operation=operation, contract_path=str(BQ))
    assert report["mode"] == "offline"
    assert no_network_no_installs == []


def test_a_schema_that_is_not_bundled_is_reported_not_fetched(no_network_no_installs):
    contract = _load(AWS)
    contract["fluidVersion"] = "0.9.99"
    report = pf.build_report(contract, probe=FakeProbe())
    assert report["schema_revision"]["source"] == "missing"
    assert report["schema_revision"]["resolved"] is None
    schema = _caps(report, pf.UNSUPPORTED, "schema")
    assert schema and "fetches none" in schema[0]["detail"]
    assert no_network_no_installs == []


def test_an_offline_cli_run_fetches_nothing(no_network_no_installs, capsys):
    rc, out = _run_cli(["--contract", str(AWS), "--operation", "apply", "--json"], capsys)
    assert json.loads(out)["version"] == pf.REPORT_FORMAT
    assert rc in (0, 1)
    assert no_network_no_installs == []


# ---------------------------------------------------------------------------
# JSON: stable shape, golden file
# ---------------------------------------------------------------------------


def _normalised(report):
    report = json.loads(pf.dumps(report))
    report["schema_revision"]["sha256"] = "<sha256 of the bundled schema>"
    report["schema_revision"]["latest_stable"] = "<latest stable>"
    return report


def test_json_report_matches_the_golden_file():
    report = _normalised(
        _report(operation="apply", probe=FakeProbe(dists={"google-cloud-bigquery": "3.25.0"}))
    )
    if os.environ.get("FLUID_UPDATE_PREFLIGHT_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert report == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_report_top_level_fields_are_stable():
    assert set(_report()) == {
        "version",
        "operation",
        "env",
        "mode",
        "contract",
        "forge",
        "schema_revision",
        "validation",
        "needs",
        "extras",
        "plugins",
        "tools",
        "adapters",
        "state_backend",
        "capabilities",
        "summary",
    }


def test_every_capability_is_one_of_the_four_classes():
    for path in (BQ, AWS):
        for operation in pf.OPERATIONS:
            report = _report(path, operation=operation)
            assert {c["class"] for c in report["capabilities"]} <= set(pf.CLASSES)
            assert set(report["summary"]["counts"]) == set(pf.CLASSES)


def test_the_resolved_schema_carries_its_hash():
    import hashlib

    import fluid_build.schema_manager as sm

    rev = _report()["schema_revision"]
    path = Path(sm.__file__).parent / "schemas" / f"fluid-schema-{rev['resolved']}.json"
    assert rev["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert rev["source"] == "bundled"


def test_extras_are_read_from_the_pins_with_their_markers():
    report = _report(AWS, operation="apply", probe=FakeProbe(dists={"boto3": "1.30.0"}))
    aws = next(e for e in report["extras"] if e["name"] == "aws")
    assert aws["needed"] is True
    assert aws["status"] == "missing"
    boto3 = next(r for r in aws["requirements"] if r["name"] == "boto3")
    assert boto3 == {
        "name": "boto3",
        "specifier": ">=1.34",
        "installed": "1.30.0",
        "satisfied": False,
    }
    gcp = next(e for e in report["extras"] if e["name"] == "gcp")
    assert gcp["needed"] is False
    assert {r["name"] for r in gcp["requirements"]} == {
        "dbt-bigquery",
        "google-auth",
        "google-cloud-bigquery",
    }


def test_no_metadata_reads_as_unknown_not_missing():
    report = _report(AWS, operation="apply", probe=FakeProbe(requires=None))
    assert report["extras"] == [
        {"name": "aws", "needed": True, "status": "unknown", "requirements": []}
    ]


def test_plugins_come_from_entry_point_metadata():
    report = _report()
    assert {
        "group": "fluid_build.providers",
        "name": "acme",
        "dist": "acme-fluid-plugin",
        "version": "1.2.0",
    } in report["plugins"]


def test_dbt_builds_need_dbt_and_the_platform_adapter():
    contract = _load(BQ)
    contract["builds"] = [{"id": "transform", "engine": "dbt", "pattern": "embedded-logic"}]
    report = pf.build_report(
        contract, operation="apply", probe=FakeProbe(dbt=(True, "core", "1.9.4"))
    )
    assert "dbt" in report["needs"]["tools"]
    adapter = {r["name"]: r for r in report["adapters"]["dbt"]}
    assert adapter["dbt-bigquery"]["needed"] is True
    assert ("adapter", "dbt-bigquery") in {
        (c["area"], c["subject"]) for c in _caps(report, pf.UNSUPPORTED)
    }


def test_a_reference_build_needs_no_dbt():
    contract = _load(BQ)
    contract["builds"] = [{"id": "ref", "engine": "dbt", "pattern": "hybrid-reference"}]
    report = pf.build_report(contract, operation="apply", probe=FakeProbe())
    assert "dbt" not in report["needs"]["tools"]


def test_an_unknown_operation_is_refused():
    with pytest.raises(pf.PreflightError):
        pf.build_report({}, operation="deploy")


# ---------------------------------------------------------------------------
# Compare: laptop vs CI, no environment value
# ---------------------------------------------------------------------------

_SECRET = "-".join(["laptop", "only", "id", "4821"])  # built here: no key shape
_SECRET_VALUE = "-".join(["laptop", "only", "value", "9f3a"])
_PATH = "/home/runner/work/private-checkout"


def _laptop_and_ci():
    environ = {
        "AWS_ACCESS_KEY_ID": _SECRET,
        "AWS_SECRET_ACCESS_KEY": _SECRET_VALUE,
        "HOME": _PATH,
        "FLUID_STATE_BACKEND": "s3://private-state-bucket/k",
    }
    laptop = _report(
        AWS,
        operation="apply",
        probe=FakeProbe(dists={"boto3": "1.35.0", "botocore": "1.35.0"}, environ=environ),
    )
    ci = _report(
        AWS,
        operation="apply",
        probe=FakeProbe(dists={"boto3": "1.33.0"}, tofu=(True, "1.11.2"), environ={}),
    )
    return laptop, ci


def test_compare_names_actionable_version_gaps():
    laptop, ci = _laptop_and_ci()
    gaps = {(g["area"], g["name"]): g for g in pf.compare_reports(laptop, ci)}
    assert gaps[("tool", "tofu")]["left"] == "1.12.1"
    assert gaps[("tool", "tofu")]["right"] == "1.11.2"
    assert ">=1.12" in gaps[("tool", "tofu")]["action"]
    assert gaps[("requirement", "aws:boto3")]["right"] == "1.33.0"
    assert ">=1.34" in gaps[("requirement", "aws:boto3")]["action"]
    assert gaps[("requirement", "aws:botocore")]["right"] == "absent"
    assert gaps[("extra", "aws")]["left"] == "installed"
    assert ("capability", "tofu:tool:tofu") in gaps


def test_identical_reports_have_no_gaps():
    laptop, _ = _laptop_and_ci()
    assert pf.compare_reports(laptop, laptop) == []


def test_reports_and_their_comparison_hold_no_environment_value():
    laptop, ci = _laptop_and_ci()
    text = pf.dumps(laptop) + pf.dumps(ci) + pf.dumps(pf.compare_reports(laptop, ci))
    for value in (_SECRET, _SECRET_VALUE, _PATH, "private-state-bucket"):
        assert value not in text
    # The variable names the provider reads may be named; their values never are.
    identity = _caps(laptop, area="identity")[0]
    assert "AWS_ACCESS_KEY_ID" in identity["detail"]


def test_a_rule_message_naming_a_path_reaches_the_report_without_it(tmp_path, capsys):
    """A Databricks dbt build whose repository is missing is refused with the directory
    it was resolved from; the report names the refusal, not the directory."""
    home = tmp_path / "secretuser-home" / "proj"
    home.mkdir(parents=True)
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "demo.dbx",
        "name": "dbx",
        "description": "a dbt build on Databricks",
        "domain": "demo",
        "metadata": {"layer": "Silver", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta",
                    "location": {"catalog": "main", "schema": "demo", "table": "orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "pattern": "embedded-logic",
                "repository": "./nowhere",
                "execution": {"runtime": {"platform": "databricks"}},
            }
        ],
    }
    path = home / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    rc, out = _run_cli(["--contract", str(path), "--json"], capsys)
    _rc, text = _run_cli(["--contract", str(path)], capsys)
    for output in (out, text):
        assert "secretuser-home" not in output
        assert str(tmp_path) not in output
        assert os.path.realpath(str(tmp_path)) not in output


def test_the_path_scrubber_keeps_pointers_and_urls():
    scrub = pf._path_scrubber("/nowhere/contract.fluid.yaml")
    cap = pf.Capability(
        pf.LOSSY,
        "x",
        "t",
        "/exposes/0",
        f"see {os.path.expanduser('~')}/x and /nowhere/y; exposes[0] at s3://b/k",
        "",
    )
    out = scrub(cap)
    assert out.subject == "/exposes/0"
    assert "~/x" in out.detail and "<contract-dir>/y" in out.detail
    assert "s3://b/k" in out.detail and "exposes[0]" in out.detail


def test_output_goes_through_both_redaction_layers(monkeypatch):
    leaked = "password=hunter2-not-a-real-one"
    monkeypatch.setattr(
        pf,
        "_column_capabilities",
        lambda contract: [pf.Capability(pf.LOSSY, "column-type", "bigquery", "x", leaked)],
    )
    text = pf.dumps(_report())
    assert "hunter2" not in text
    assert pf.redact_report({"a": [f"token={_SECRET_VALUE}"]}) != {"a": [f"token={_SECRET_VALUE}"]}


def test_compare_cli_reports_gaps_and_refuses_a_foreign_file(tmp_path, capsys):
    laptop, ci = _laptop_and_ci()
    left, right = tmp_path / "laptop.json", tmp_path / "ci.json"
    left.write_text(pf.dumps(laptop), encoding="utf-8")
    right.write_text(pf.dumps(ci), encoding="utf-8")
    rc, out = _run_cli(["--compare", str(left), str(right), "--json"], capsys)
    assert rc == 1
    payload = json.loads(out)
    assert payload["version"] == "fluid-preflight-compare/1"
    assert payload["differences"]
    rc, out = _run_cli(["--compare", str(left), str(left)], capsys)
    assert rc == 0 and "No differences" in out

    foreign = tmp_path / "other.json"
    foreign.write_text(json.dumps({"version": "1", "installed": []}), encoding="utf-8")
    from fluid_build.cli._common import CLIError

    with pytest.raises(CLIError) as err:
        _run_cli(["--compare", str(left), str(foreign)], capsys)
    assert err.value.exit_code == 2
    assert pf.REPORT_FORMAT in err.value.context["reason"]


# ---------------------------------------------------------------------------
# --live: read-only, reclassifies, prints no identity
# ---------------------------------------------------------------------------


def _fake_boto3(monkeypatch, fail=False):
    calls = []

    class _Client:
        def __init__(self, service):
            self.service = service

        def get_caller_identity(self):
            calls.append((self.service, "get_caller_identity"))
            if fail:
                raise PermissionError("denied")
            return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/alice"}

        def head_bucket(self, Bucket):
            calls.append((self.service, "head_bucket"))
            return {}

        def __getattr__(self, name):
            raise AssertionError(f"--live called {self.service}.{name}")

    module = types.ModuleType("boto3")
    module.client = lambda service, **kw: _Client(service)
    monkeypatch.setitem(sys.modules, "boto3", module)
    return calls


def test_live_identity_ok_becomes_exact_without_printing_it(monkeypatch):
    calls = _fake_boto3(monkeypatch)
    probe = FakeProbe(
        dists={"boto3": "1.35.0", "botocore": "1.35.0"},
        environ={"FLUID_STATE_BACKEND": "s3://private-state-bucket/k"},
    )
    report = _report(AWS, operation="apply", probe=probe, live=True)
    assert report["mode"] == "live"
    assert calls == [("sts", "get_caller_identity"), ("s3", "head_bucket")]
    assert [c["class"] for c in _caps(report, area="identity")] == [pf.EXACT]
    assert [c["class"] for c in _caps(report, area="state-backend")] == [pf.EXACT]
    text = pf.dumps(report)
    assert "123456789012" not in text and "alice" not in text
    assert "private-state-bucket" not in text


def test_live_identity_failure_is_unsupported(monkeypatch):
    _fake_boto3(monkeypatch, fail=True)
    report = _report(AWS, operation="apply", probe=FakeProbe(), live=True)
    identity = _caps(report, area="identity")
    assert [c["class"] for c in identity] == [pf.UNSUPPORTED]
    assert "PermissionError" in identity[0]["detail"]
    assert pf.exit_code(report) == 1


def test_a_failed_live_check_names_the_code_not_the_message():
    class ClientError(Exception):
        response = {"Error": {"Code": "403", "Message": "arn:aws:iam::123456789012:user/alice"}}

    def _denied():
        raise ClientError("User arn:aws:iam::123456789012:user/alice is not authorized")

    row = pf._check("aws-identity", _denied)
    assert row == {"check": "aws-identity", "status": "failed", "detail": "ClientError 403"}


def test_live_without_the_sdk_is_skipped_and_stays_requires_live_proof(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    report = _report(AWS, operation="apply", probe=FakeProbe(), live=True)
    assert report["live_checks"][0]["status"] == "skipped"
    assert [c["class"] for c in _caps(report, area="identity")] == [pf.LIVE]


def test_live_on_an_offline_operation_checks_nothing(monkeypatch):
    calls = _fake_boto3(monkeypatch)
    report = _report(AWS, operation="plan", probe=FakeProbe(), live=True)
    assert report["live_checks"] == []
    assert calls == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_text_output_and_exit_code(capsys):
    rc, out = _run_cli(["--contract", str(BQ)], capsys)
    assert rc == 1
    assert "unsupported" in out and "exposes[events].genres" in out
    assert "fluid doctor --contract: plan (offline)" in out


def test_cli_strict_fails_on_lossy(capsys):
    contract_rc, _ = _run_cli(["--contract", str(AWS), "--operation", "plan"], capsys)
    strict_rc, _ = _run_cli(["--contract", str(AWS), "--operation", "plan", "--strict"], capsys)
    assert (contract_rc, strict_rc) == (0, 1)


def test_cli_env_overlay_is_applied(tmp_path, capsys):
    base = _load(AWS)
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    overlay = {
        "exposes": [
            {"exposeId": "orders", "contract": {"schema": [{"name": "id", "type": "string"}]}}
        ]
    }
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    rc, out = _run_cli(
        ["--contract", str(tmp_path / "contract.fluid.yaml"), "--env", "prod", "--json"], capsys
    )
    assert json.loads(out)["env"] == "prod"


@pytest.mark.parametrize(
    "argv, reason",
    [
        (["--env", "prod"], "--env NAME needs --contract"),
        (["--operation", "apply"], "--operation needs --contract"),
        (["--live"], "--live needs --contract"),
        (["--contract", str(AWS), "--env"], "--env needs an overlay name"),
    ],
)
def test_cli_refuses_flags_out_of_place(argv, reason, capsys):
    from fluid_build.cli._common import CLIError

    with pytest.raises(CLIError) as err:
        _run_cli(argv, capsys)
    assert err.value.exit_code == 2
    assert reason in err.value.context["reason"]


def test_env_alone_still_lists_the_kill_switches(capsys):
    args = build_parser().parse_args(["doctor", "--env"])
    assert args.env is True and args.contract is None
    args = build_parser().parse_args(["doctor"])
    assert args.env is False


# ---------------------------------------------------------------------------
# Review fixes: dbt resolution, extras, secret refs, compare keys, flag clashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["dbt-bigquery", "DBT-BigQuery"])
def test_an_adapter_qualified_dbt_engine_is_a_dbt_build(engine):
    contract = _load(BQ)
    contract["builds"] = [{"id": "t", "engine": engine, "pattern": "embedded-logic"}]
    report = pf.build_report(contract, operation="apply", probe=FakeProbe())
    assert "dbt" in report["needs"]["tools"]
    unsupported = {(c["area"], c["subject"]) for c in _caps(report, pf.UNSUPPORTED)}
    assert ("tool", "dbt") in unsupported
    assert ("adapter", "dbt-bigquery") in unsupported


def test_the_dbt_the_runner_resolves_is_the_one_reported(monkeypatch, tmp_path):
    """$DBT_EXECUTABLE counts, as it does for the runner; its path is never printed."""
    fake = tmp_path / "dbt"
    fake.write_text("#!/bin/sh\necho 'Core: 1.9.4'\n", encoding="utf-8")
    fake.chmod(0o700)
    monkeypatch.setenv("DBT_EXECUTABLE", str(fake))
    monkeypatch.setenv("PATH", "/nonexistent")
    found, flavor, version = pf.Probe().dbt(detect=False)
    assert (found, flavor, version) == (True, None, None)
    contract = _load(BQ)
    contract["builds"] = [{"id": "t", "engine": "dbt", "pattern": "embedded-logic"}]
    report = pf.build_report(
        contract, operation="apply", probe=FakeProbe(dbt=(True, "core", "1.9.4"))
    )
    tool = next(t for t in report["tools"] if t["name"] == "dbt")
    assert (tool["found"], tool["flavor"], tool["version"]) == (True, "core", "1.9.4")
    assert str(tmp_path) not in pf.dumps(report)


def test_dbt_fusion_needs_no_python_adapter():
    contract = _load(BQ)
    contract["builds"] = [{"id": "t", "engine": "dbt-bigquery", "pattern": "embedded-logic"}]
    report = pf.build_report(
        contract, operation="apply", probe=FakeProbe(dbt=(True, "fusion", "2.0.0"))
    )
    assert not [c for c in _caps(report, pf.UNSUPPORTED) if c["area"] in ("adapter", "tool")]


def test_a_databricks_job_build_needs_no_local_dbt():
    contract = _load(BQ)
    contract["builds"] = [
        {
            "id": "t",
            "engine": "dbt",
            "pattern": "embedded-logic",
            "execution": {"runtime": {"platform": "databricks"}},
        }
    ]
    report = pf.build_report(contract, operation="apply", probe=FakeProbe())
    assert "dbt" not in report["needs"]["tools"]


def test_an_extra_that_includes_others_is_judged_by_them():
    requires = [
        "boto3>=1.34; extra == 'aws'",
        "duckdb>=1.5.0; extra == 'local'",
        "data-product-forge[aws,local]; extra == 'all'",
    ]
    report = _report(AWS, probe=FakeProbe(dists={"boto3": "1.35.0"}, requires=requires))
    every = next(e for e in report["extras"] if e["name"] == "all")
    assert {r["name"] for r in every["requirements"]} == {"boto3", "duckdb"}
    assert every["status"] == "partial"


def test_a_secret_reference_warning_needs_live_proof(monkeypatch):
    """validate's secret-ref warnings (no resolver here, a host judged at apply) are
    what only the run decides."""
    from fluid_build.cli import validate as v

    real = v._run_contract_rules

    def rules(contract, version, result, args, logger, locate=None):
        real(contract, version, result, args, logger, locate=locate)
        result.add_warning(
            "exposes[orders]: no resolver for azure-kv:// is installed",
            event="contract_secret_ref",
            pointer="/exposes/0",
        )

    monkeypatch.setattr(v, "_run_contract_rules", rules)
    caps = _caps(_report(AWS), pf.LIVE, "secret-ref")
    assert [c["subject"] for c in caps] == ["/exposes/0"]


def test_compare_counts_capabilities_that_share_a_key():
    """Five schema errors at one pointer against four: the one more is a gap."""
    left = _report(AWS, operation="validate")
    right = json.loads(pf.dumps(left))
    right["capabilities"].append(dict(right["capabilities"][0], detail="one more"))
    gaps = pf.compare_reports(left, right)
    assert [(g["area"], g["left"]) for g in gaps] == [("capability", "absent")]


def test_compare_ignores_extras_neither_side_needs():
    both = {"boto3": "1.35.0", "botocore": "1.35.0"}
    laptop = _report(AWS, operation="apply", probe=FakeProbe(dists={**both, "duckdb": "1.5.1"}))
    ci = _report(AWS, operation="apply", probe=FakeProbe(dists=both))
    assert not [g for g in pf.compare_reports(laptop, ci) if "local" in g["name"]]


@pytest.mark.parametrize(
    "extra, clash",
    [
        (["--scope", "infra"], "--scope"),
        (["--extended"], "--extended"),
        (["--features-only"], "--features-only"),
    ],
)
def test_cli_refuses_doctor_modes_with_contract(extra, clash, capsys):
    from fluid_build.cli._common import CLIError

    with pytest.raises(CLIError) as err:
        _run_cli(["--contract", str(AWS), *extra], capsys)
    assert clash in err.value.context["reason"]


def test_cli_refuses_contract_with_compare(tmp_path, capsys):
    from fluid_build.cli._common import CLIError

    report = tmp_path / "r.json"
    report.write_text(pf.dumps(_report()), encoding="utf-8")
    with pytest.raises(CLIError) as err:
        _run_cli(["--compare", str(report), str(report), "--contract", str(AWS)], capsys)
    assert "--contract" in err.value.context["reason"]


def test_live_on_an_offline_operation_says_so(capsys):
    _rc, out = _run_cli(["--contract", str(AWS), "--live"], capsys)
    assert "no live check applies to plan" in out


def _two_clouds():
    contract = _load(AWS)
    contract["exposes"].append(_load(BQ)["exposes"][0])
    return contract


def test_apply_on_two_clouds_needs_a_provider():
    report = pf.build_report(_two_clouds(), operation="apply", probe=FakeProbe())
    refusal = _caps(report, pf.UNSUPPORTED, "provider")
    assert refusal and "--provider" in refusal[0]["fix"]
    assert report["needs"]["clouds"] == []


def test_provider_scopes_what_apply_needs():
    report = pf.build_report(_two_clouds(), operation="apply", provider="aws", probe=FakeProbe())
    assert report["needs"]["clouds"] == ["aws"]
    assert [c["target"] for c in _caps(report, area="identity")] == ["aws"]
    assert not _caps(report, pf.UNSUPPORTED, "provider")


def test_a_provider_the_contract_does_not_declare_is_unsupported():
    report = pf.build_report(_load(AWS), operation="apply", provider="gcp", probe=FakeProbe())
    assert _caps(report, pf.UNSUPPORTED, "provider")


def test_the_state_backend_flag_beats_the_environment():
    probe = FakeProbe(environ={"FLUID_STATE_BACKEND": "s3://env-bucket/k"})
    report = _report(AWS, operation="apply", probe=probe, state_backend="gcs://flag-bucket/p")
    assert report["state_backend"]["origin"] == "--state-backend"
    assert report["state_backend"]["scheme"] == "gcs"
    local = _report(AWS, operation="apply", probe=probe, state_backend="")
    assert local["state_backend"]["scheme"] == "local"


@pytest.mark.parametrize("version", [5, 0.7, True, "latest"])
def test_an_unreadable_fluid_version_is_a_report_not_a_crash(version):
    report = pf.build_report({"fluidVersion": version, "id": "x"}, probe=FakeProbe())
    assert report["summary"]["verdict"] == "not-ready"
    assert _caps(report, pf.UNSUPPORTED, "schema")


@pytest.mark.parametrize("text", ["- a\n", "hello\n", "a: [unclosed\n"])
def test_a_malformed_contract_is_a_clean_error(text, tmp_path, capsys):
    from fluid_build.cli._common import CLIError

    path = tmp_path / "c.fluid.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CLIError) as err:
        _run_cli(["--contract", str(path)], capsys)
    assert err.value.event == "doctor_preflight_failed"
    assert str(tmp_path) not in err.value.context["reason"]


def test_a_deeply_nested_report_is_refused(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    with pytest.raises(pf.PreflightError):
        pf.load_report(str(path))


def test_the_installed_validators_run_as_validate_runs_them(monkeypatch):
    from fluid_build.cli import validate as v

    def extension(contract, result, logger, locate=None):
        result.add_error("extensions.acme: refused by acme", event="contract_extension")

    monkeypatch.setattr(v, "_run_extension_validators", extension)
    caps = _caps(_report(AWS), pf.UNSUPPORTED, "extension")
    assert [c["detail"] for c in caps] == ["extensions.acme: refused by acme"]


def test_no_schema_cache_is_read_or_written_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _report(AWS)
    assert not (tmp_path / ".fluid").exists()


def test_a_bundle_is_preflighted_like_its_contract(tmp_path, capsys):
    out = tmp_path / "b.tgz"
    args = build_parser().parse_args(["bundle", str(AWS), "--format", "tgz", "--out", str(out)])
    assert args.func(args, logging.getLogger("test")) == 0
    _rc, text = _run_cli(["--contract", str(out), "--operation", "validate", "--json"], capsys)
    bundled = json.loads(text)
    _rc, text = _run_cli(["--contract", str(AWS), "--operation", "validate", "--json"], capsys)
    assert bundled["capabilities"] == json.loads(text)["capabilities"]


@pytest.mark.parametrize(
    "text",
    ["gs://bkt/opt/x", "s3://b/optimized/k", "/optional", "/optx/y", "word/opt/z", "a.opt"],
)
def test_a_root_is_replaced_only_where_it_starts_a_path(text):
    assert pf._unrooted(text, [("/opt", "<cwd>")]) == text


def test_a_root_that_starts_a_path_is_replaced():
    roots = [("/opt", "<cwd>")]
    assert pf._unrooted("/opt", roots) == "<cwd>"
    assert pf._unrooted("see /opt/x and (/opt)", roots) == "see <cwd>/x and (<cwd>)"


def test_a_path_after_a_colon_or_equals_is_still_scrubbed():
    scrub = pf._path_scrubber("/nowhere/contract.fluid.yaml")
    home = os.path.expanduser("~")
    cap = pf.Capability(pf.LOSSY, "x", "t", "/", f"dir:{home}/p path={home}/q", "")
    detail = scrub(cap).detail
    assert home not in detail and detail == "dir:~/p path=~/q"


def test_cli_provider_scopes_apply_and_leaves_the_global_flag_alone(tmp_path, capsys):
    path = tmp_path / "two.fluid.yaml"
    path.write_text(yaml.safe_dump(_two_clouds()), encoding="utf-8")
    argv = ["--contract", str(path), "--operation", "apply", "--provider", "aws", "--json"]
    _rc, out = _run_cli(argv, capsys)
    assert json.loads(out)["needs"]["clouds"] == ["aws"]
    args = build_parser().parse_args(["--provider", "gcp", "doctor"])
    assert (args.provider, args.preflight_provider) == ("gcp", None)
