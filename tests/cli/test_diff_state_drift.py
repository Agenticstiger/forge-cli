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

"""The apply-state drift pass in ``fluid diff`` and ``fluid verify --state-drift``.

``tofu`` never runs here: the runner is replaced, so these pin the wiring
(where the state is looked for, what is not checked and why, how a result
reaches the gate, what the pass leaves behind). The real ``tofu`` against
moto is ``tests/iac/test_iac_state_drift_moto.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli._common import CLIError
from fluid_build.iac import drift, runner
from fluid_build.iac.naming import safe_ident

pytestmark = pytest.mark.unit

LOG = logging.getLogger("test.diff_state_drift")
CID = "silver.orders"

# An S3 prefix with no Glue table: the SDK check has no inspector for it, so
# before the state pass the gate had nothing to compare and was downgraded.
S3_ONLY = {
    "platform": "aws",
    "format": "parquet",
    "location": {"bucket": "orders-lake", "path": "silver/orders/"},
}
LOCAL = {"platform": "local", "format": "parquet", "location": {"path": "./out/orders.parquet"}}


def _contract(binding: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": CID,
        "name": "Orders",
        "domain": "Sales",
        "metadata": {"layer": "Silver", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": [{"name": "order_id", "type": "VARCHAR"}]},
            }
        ],
    }


def _write(root: Path, contract: Dict[str, Any]) -> Path:
    path = root / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _workdir(root: Path) -> Path:
    return root / ".fluid" / "iac" / "aws" / safe_ident(CID)


def _applied(root: Path) -> Path:
    """What an apply leaves: the module and local state."""
    workdir = _workdir(root)
    workdir.mkdir(parents=True)
    (workdir / "main.tf.json").write_text('{"applied": true}', encoding="utf-8")
    (workdir / "terraform.tfstate").write_text('{"resources": [1]}', encoding="utf-8")
    return workdir


def _invoke(argv: List[str]):
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(argv)
    try:
        return args.func(args, LOG), None
    except CLIError as exc:
        return exc.exit_code, exc.event


def _bucket_entry(actions: List[str], before: Any, after: Any) -> Dict[str, Any]:
    return {
        "address": "aws_s3_bucket.silver_orders_orders_lake",
        "mode": "managed",
        "type": "aws_s3_bucket",
        "change": {"actions": actions, "before": before, "after": after},
    }


def _plan_doc(*, tagged: bool) -> Dict[str, Any]:
    tags = {"managed_by": "fluid"}
    live = dict(tags, owner="someone-else") if tagged else tags
    return {
        "prior_state": {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "aws_s3_bucket.silver_orders_orders_lake",
                            "mode": "managed",
                            "type": "aws_s3_bucket",
                            "values": {"tags": live},
                        }
                    ]
                }
            }
        },
        "resource_drift": (
            [_bucket_entry(["update"], {"tags": tags}, {"tags": live})] if tagged else []
        ),
        "resource_changes": [
            _bucket_entry(["update"] if tagged else ["no-op"], {"tags": live}, {"tags": tags})
        ],
    }


class _Tofu:
    """The runner calls the state pass makes, recorded and answered."""

    def __init__(self, monkeypatch, *, plan_rc: int = 0, doc: Optional[Dict] = None) -> None:
        self.calls: List[str] = []
        self.plan_rc = plan_rc
        self.doc = doc
        monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
        monkeypatch.setattr(runner, "require_tofu_version", lambda: None)
        monkeypatch.setattr(runner, "tofu_init", self._init)
        monkeypatch.setattr(runner, "tofu_state_resources", lambda *a, **k: [])
        monkeypatch.setattr(runner, "tofu_plan_detailed", self._plan)
        monkeypatch.setattr(runner, "tofu_show_plan", self._show)
        # The native planner is best-effort and builds an AWS client.
        from fluid_build.cli import _apply_opentofu_engine as engine

        monkeypatch.setattr(engine, "native_actions", lambda contract, logger: [])
        # Moving a pre-provider-key state (``iac.state_migration``) is not
        # under test here: nothing to move.
        from fluid_build.iac.state_migration import CURRENT, StateReconciliation

        monkeypatch.setattr(
            engine,
            "_reconcile_state",
            lambda **kw: StateReconciliation(CURRENT, kw["legacy"], kw["current"]),
        )

    def _init(self, workdir, *, backend=True, env=None, reconfigure=False, force_copy=False):
        self.calls.append(f"init backend={backend}")
        self.module_during_run = (Path(workdir) / "main.tf.json").read_text(encoding="utf-8")
        return runner.TofuResult("init", 0, "", "")

    def _plan(self, workdir, *, out_file, env=None):
        self.calls.append(f"plan {out_file}")
        (Path(workdir) / out_file).write_text("binary plan with secrets", encoding="utf-8")
        return runner.TofuResult("plan-detailed", self.plan_rc, "", "Error: AccessDenied")

    def _show(self, workdir, *, plan_file, env=None):
        self.calls.append(f"show {plan_file}")
        return self.doc


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for var in ("FLUID_PROVIDER", "FLUID_PROJECT", "FLUID_REGION", "FLUID_STATE_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.chdir(tmp_path)
    from fluid_build.cli import diff as diff_mod

    class _PlanOnly:
        def plan(self, contract):
            return [{"op": "ensure", "resource_type": "bucket", "resource_id": "orders-lake"}]

    monkeypatch.setattr(diff_mod, "build_provider", lambda *a, **k: _PlanOnly())
    return tmp_path


def _state(out: Path) -> Dict[str, Any]:
    return json.loads(out.read_text(encoding="utf-8"))["state_drift"]


# ---------------------------------------------------------------------------
# fluid diff
# ---------------------------------------------------------------------------


def test_no_apply_here_is_not_checked_and_runs_no_tofu(workspace, monkeypatch, capsys):
    def _no_tofu(*a, **k):
        raise AssertionError("tofu must not run when there is no state to read")

    monkeypatch.setattr(runner, "_run", _no_tofu)
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (0, None)
    section = _state(out)
    assert section["status"] == "not_checked"
    assert "no apply state for this contract" in section["detail"]
    assert "--workspace-dir" in section["detail"] and "FLUID_STATE_BACKEND" in section["detail"]
    assert "State drift check: not run" in capsys.readouterr().out
    assert not (workspace / ".fluid").exists()


def test_a_local_contract_keeps_no_opentofu_state(workspace, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda *a, **k: pytest.fail("tofu ran"))
    contract = _write(workspace, _contract(LOCAL))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--out", str(out)]) == (0, None)
    section = _state(out)
    assert section["status"] == "not_checked"
    assert "local engine" in section["detail"]


def test_state_without_tofu_on_path_is_not_checked(workspace, monkeypatch):
    _applied(workspace)
    monkeypatch.setattr(runner, "tofu_path", lambda: None)
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (0, None)
    section = _state(out)
    assert section["status"] == "not_checked"
    assert "OpenTofu is not installed" in section["detail"]
    assert section["state"].startswith("local: ")


def test_ensure_opentofu_provisions_tofu_when_state_is_there(workspace, monkeypatch):
    from fluid_build.iac import opentofu_install

    _applied(workspace)
    tofu = _Tofu(monkeypatch, plan_rc=2, doc=_plan_doc(tagged=True))
    installed: List[str] = []

    def _ensure(*, version=None, logger=None):
        installed.append("tofu")
        monkeypatch.setattr(runner, "tofu_path", lambda: "/provisioned/tofu")
        return "/provisioned/tofu"

    monkeypatch.setattr(runner, "tofu_path", lambda: None)
    monkeypatch.setattr(opentofu_install, "ensure_opentofu", _ensure)
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    code, _ = _invoke(
        ["diff", str(contract), "--exit-on-drift", "--ensure-opentofu", "--out", str(out)]
    )
    assert code == 1
    assert installed == ["tofu"]
    assert tofu.calls[0] == "init backend=False"


def test_an_unexpected_failure_is_could_not_look_not_drift(workspace, monkeypatch):
    """A crash in the pass must not surface as ``diff_failed`` (exit 1, which
    reads as drift) nor pass the gate."""
    from fluid_build.cli import _apply_opentofu_engine as engine

    _applied(workspace)
    _Tofu(monkeypatch)

    def _boom(*a, **k):
        raise ValueError("emitter broke")

    monkeypatch.setattr(engine, "emit_module", _boom)
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (
        2,
        "diff_state_inspection_failed",
    )
    assert _state(out)["detail"] == "ValueError: emitter broke"


def test_drift_in_state_fails_the_gate_where_the_live_check_sees_nothing(workspace, monkeypatch):
    workdir = _applied(workspace)
    tofu = _Tofu(monkeypatch, plan_rc=2, doc=_plan_doc(tagged=True))
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (1, None)
    assert tofu.calls == [
        "init backend=False",
        "plan fluid-drift.tfplan",
        "show fluid-drift.tfplan",
    ]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"]["has_drift"] is True
    assert [e["status"] for e in report["live"]["exposes"]] == ["not_checked"]
    section = report["state_drift"]
    assert section["status"] == "checked"
    assert section["plan_exit_code"] == 2
    assert section["resources"][0]["drifted"] == ["tags.owner"]
    # The module the pass planned is the apply's emit, and the apply's own
    # module and a plan full of attribute values are not left behind.
    assert '"aws_s3_bucket"' in tofu.module_during_run
    assert (workdir / "main.tf.json").read_text(encoding="utf-8") == '{"applied": true}'
    assert not (workdir / "fluid-drift.tfplan").exists()


def test_a_clean_state_is_a_real_pass_not_a_downgrade(workspace, monkeypatch, caplog):
    _applied(workspace)
    _Tofu(monkeypatch, plan_rc=0, doc=_plan_doc(tagged=False))
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    with caplog.at_level(logging.INFO):
        assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (0, None)
    assert _state(out)["counts"] == {"drift": 0, "pending": 0, "match": 1}
    assert "diff_exit_on_drift_skipped" not in caplog.text


def test_a_failed_refresh_is_an_inspection_failure_not_a_pass(workspace, monkeypatch):
    workdir = _applied(workspace)
    _Tofu(monkeypatch, plan_rc=1)
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (
        2,
        "diff_state_inspection_failed",
    )
    section = _state(out)
    assert section["status"] == "error"
    assert "AccessDenied" in section["detail"]
    # Informational without the gate.
    assert _invoke(["diff", str(contract), "--out", str(out)]) == (0, None)
    assert (workdir / "main.tf.json").read_text(encoding="utf-8") == '{"applied": true}'


def test_the_state_backend_is_resolved_as_apply_resolves_it(workspace, monkeypatch):
    """``FLUID_STATE_BACKEND`` names remote state: no local workdir is needed,
    the backend is initialised, and the location is the one apply prints."""
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://team-state")
    tofu = _Tofu(monkeypatch, plan_rc=0, doc=_plan_doc(tagged=False))
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--out", str(out)]) == (0, None)
    assert tofu.calls[0] == "init backend=True"
    assert '"s3"' in tofu.module_during_run
    assert _state(out)["state"] == f"remote: s3://team-state/fluid/{CID}/aws/terraform.tfstate"
    # The module the pass wrote into a fresh workdir is not left there.
    assert not (_workdir(workspace) / "main.tf.json").exists()


def test_an_unusable_state_backend_is_an_error(workspace, monkeypatch):
    monkeypatch.setenv("FLUID_STATE_BACKEND", "ftp://nope")
    contract = _write(workspace, _contract(S3_ONLY))
    out = workspace / "diff.json"

    assert _invoke(["diff", str(contract), "--exit-on-drift", "--out", str(out)]) == (
        2,
        "diff_state_inspection_failed",
    )
    assert "state backend is not usable" in _state(out)["detail"]


# ---------------------------------------------------------------------------
# fluid verify --state-drift
# ---------------------------------------------------------------------------

_VERIFY_CONTRACT = _contract(LOCAL)


@pytest.fixture
def verify_contract(tmp_path, monkeypatch) -> Path:
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path, _VERIFY_CONTRACT)
    (tmp_path / "out").mkdir()
    duckdb.sql(
        f"COPY (SELECT 'o1'::VARCHAR AS order_id) TO '{(tmp_path / 'out' / 'orders.parquet').as_posix()}' "
        "(FORMAT parquet)"
    )
    return path


def _verify(contract: Path, monkeypatch, report: Optional[drift.StateDriftReport], *flags: str):
    from fluid_build.cli import _diff_state
    from fluid_build.cli import verify as verify_cmd

    calls: List[Any] = []

    def _check(contract_doc, args, logger):
        calls.append(args)
        assert report is not None
        return report

    monkeypatch.setattr(_diff_state, "check_state_drift", _check)
    parser = argparse.ArgumentParser(prog="fluid")
    verify_cmd.register(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["verify", str(contract), "--out", "report.json", *flags])
    return verify_cmd.run(args, LOG), calls


def _drifted() -> drift.StateDriftReport:
    return drift.StateDriftReport(
        status=drift.CHECKED,
        state="local: x",
        resources=[
            drift.ResourceState(
                address="aws_s3_bucket.b",
                type="aws_s3_bucket",
                status=drift.DRIFT,
                actions=["update"],
                drifted=["tags.owner"],
            )
        ],
    )


def test_verify_runs_no_state_pass_unless_asked(verify_contract, monkeypatch):
    code, calls = _verify(verify_contract, monkeypatch, None)
    assert code == 0
    assert calls == []
    assert "state_drift" not in json.loads(Path("report.json").read_text(encoding="utf-8"))


def test_verify_state_drift_fails_on_drift(verify_contract, monkeypatch, capsys):
    code, calls = _verify(
        verify_contract,
        monkeypatch,
        _drifted(),
        "--state-drift",
        "--workspace-dir",
        "applied",
        "--state-backend",
        "",
    )
    assert code == 1
    assert (calls[0].workspace_dir, calls[0].state_backend) == ("applied", "")
    assert "changed outside the apply" in capsys.readouterr().out
    doc = json.loads(Path("report.json").read_text(encoding="utf-8"))
    assert doc["state_drift"]["has_drift"] is True


def test_verify_state_drift_warn_only_reports_and_passes(verify_contract, monkeypatch):
    code, _ = _verify(verify_contract, monkeypatch, _drifted(), "--state-drift", "--warn-only")
    assert code == 0


def test_verify_state_drift_that_could_not_run_fails(verify_contract, monkeypatch):
    failed = drift.StateDriftReport(status=drift.ERROR, state="s", detail="AccessDenied")
    code, _ = _verify(verify_contract, monkeypatch, failed, "--state-drift", "--warn-only")
    assert code == 1


def test_verify_state_drift_without_state_is_a_note(verify_contract, monkeypatch, capsys):
    none = drift.StateDriftReport(status=drift.NOT_CHECKED, detail="no apply state here")
    code, _ = _verify(verify_contract, monkeypatch, none, "--state-drift")
    assert code == 0
    assert "State drift check: not run (no apply state here)" in capsys.readouterr().out


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attrs: Dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_status(self, _status: Any) -> None:
        return None

    def __enter__(self) -> "_Span":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class _Tracer:
    def __init__(self) -> None:
        self.spans: List[_Span] = []

    def start_as_current_span(self, name: str) -> _Span:
        self.spans.append(_Span(name))
        return self.spans[-1]


def test_verify_with_tracing_on_opens_one_verify_span_around_the_whole_run(
    verify_contract, monkeypatch
):
    """With OpenTelemetry on, ``fluid verify --state-drift`` is one ``fluid.verify``
    span around ``run``: the stage span carries run's exit code, and the state
    pass (which returns a report, not an exit code) is not a stage of its own."""
    tracer = _Tracer()
    monkeypatch.setattr("fluid_build.observability.tracing._get_tracer", lambda: tracer)
    monkeypatch.setenv("FLUID_RUN_ID", "run-under-test")

    code, _ = _verify(verify_contract, monkeypatch, _drifted(), "--state-drift")

    assert code == 1
    assert [span.name for span in tracer.spans] == ["fluid.verify"]
    assert tracer.spans[0].attrs["fluid.exit_code"] == 1
    assert tracer.spans[0].attrs["fluid.run_id"] == "run-under-test"
