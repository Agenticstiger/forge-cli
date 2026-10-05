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

"""The state move's safety branches, and what its scratch ``tofu init`` installs.

Three branches of ``state_migration.reconcile_state_key`` had no test: a
mutant removing the re-check before the copy, one disabling the check after
it, and one pointing ``fluid diff`` at the new key all passed the suite. Here
the ``tofu`` calls are replaced by a scripted fake, so each branch is driven
on purpose: another job writing the new key between the first check and the
copy (``state_migration_raced``), a copy that reads back different resources
(``state_migration_unverified``), and a probe whose init failed before it
recorded the old backend (an error, never an empty state).

The probe's scratch init used to install the providers the old state names,
at their latest version (OpenTofu installs what the state requires): a
``{"terraform": {}}`` module beside a state naming ``hashicorp/null``
installed ``hashicorp/null v3.3.2``, and it ran on every apply whose new key
was empty. It now installs nothing (``-plugin-dir`` on an empty directory),
and the one-time move installs at the plugin's pins. The offline tests prove
the probe with real ``tofu`` and no registry reachable (a dead proxy): it
attributes the state; before, it reached for the registry and failed.

Also here: the apply's refusal to plan on a key that names no provider when
it holds another cloud's resources, and ``fluid apply --dry-run`` asking the
move not to run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner
from fluid_build.iac import state_migration as mig
from fluid_build.iac.runner import TofuResult

pytestmark = pytest.mark.unit

_AWS = 'provider["registry.opentofu.org/hashicorp/aws"]'
_GOOGLE = 'provider["registry.opentofu.org/hashicorp/google"]'


def _state(lineage: str, *resources: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": 4,
        "terraform_version": "1.12.0",
        "serial": 3,
        "lineage": lineage,
        "outputs": {},
        "resources": list(resources),
    }


def _resource(rtype: str, name: str, provider: str) -> Dict[str, Any]:
    return {
        "mode": "managed",
        "type": rtype,
        "name": name,
        "provider": provider,
        # As `tofu state pull` prints it (it adds the empty list).
        "instances": [
            {"schema_version": 0, "attributes": {"id": name}, "sensitive_attributes": []}
        ],
    }


_EMPTY = _state("")  # what `tofu state pull` prints for a key with no state
_OLD = _state("11111111-aaaa", _resource("aws_s3_bucket", "lake", _AWS))
_OTHER_JOB = _state("22222222-bbbb", _resource("aws_s3_bucket", "someone_else", _AWS))

_CURRENT = {"s3": {"bucket": "b", "key": "fluid/p/aws/terraform.tfstate"}}
_LEGACY = {"s3": {"bucket": "b", "key": "fluid/p/terraform.tfstate"}}


class _FakeTofu:
    """Scripted ``tofu init`` / ``tofu state pull``, keyed by the directory's name.

    ``workdir`` is the apply's own directory (the new key), ``legacy`` the
    probe's, ``legacy-plain`` its fallback and ``move`` the copy's.
    """

    def __init__(self, workdir_pulls: List[Dict[str, Any]], legacy: Dict[str, Any]) -> None:
        self.workdir_pulls = list(workdir_pulls)
        self.legacy = legacy
        self.calls: List[Dict[str, Any]] = []
        #: The probe's -plugin-dir init: 0, or 1 as when it stops at the
        #: provider step; ``records`` says whether it recorded the backend.
        self.probe_init_rc = 0
        self.records = False
        self.plain_init_rc = 0
        self.modules: Dict[str, Any] = {}

    def init(self, workdir: str, **kwargs: Any) -> TofuResult:
        path = Path(workdir)
        name = path.name
        module = json.loads((path / "main.tf.json").read_text(encoding="utf-8"))
        self.modules[f"{name}{'-copy' if kwargs.get('force_copy') else ''}"] = module
        plugin_dir = kwargs.get("plugin_dir")
        self.calls.append(
            {
                "init": name,
                **kwargs,
                "plugin_dir_empty": (
                    plugin_dir is not None and not any(Path(plugin_dir).iterdir())
                ),
            }
        )
        rc = 0
        if name == "legacy":
            rc = self.probe_init_rc
            if self.records:
                recorded = {"version": 3, "backend": {"type": "s3", "config": _LEGACY["s3"]}}
                (path / ".terraform").mkdir(exist_ok=True)
                (path / ".terraform" / "terraform.tfstate").write_text(json.dumps(recorded))
        elif name == "legacy-plain":
            rc = self.plain_init_rc
        return TofuResult("init", rc, "", "Error: Failed to query available provider packages")

    def pull(self, workdir: str, **_kwargs: Any) -> TofuResult:
        name = Path(workdir).name
        self.calls.append({"pull": name})
        doc = self.legacy if name.startswith("legacy") else self.workdir_pulls.pop(0)
        return TofuResult("state-pull", 0, json.dumps(doc), "")

    @property
    def copies(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("force_copy")]


@pytest.fixture
def fake(monkeypatch):
    def install(workdir_pulls, legacy=_OLD):
        tofu = _FakeTofu(workdir_pulls, legacy)
        monkeypatch.setattr(mig.runner, "tofu_init", tofu.init)
        monkeypatch.setattr(mig.runner, "tofu_state_pull", tofu.pull)
        return tofu

    return install


def _reconcile(tmp_path: Path, *, migrate: bool = True):
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    return mig.reconcile_state_key(
        workdir=workdir,
        current=_CURRENT,
        legacy=_LEGACY,
        provider="aws",
        env={},
        migrate=migrate,
    )


# ── the re-check before the copy ─────────────────────────────────────────


def test_a_state_written_to_the_new_key_before_the_copy_is_never_overwritten(fake, tmp_path):
    """Empty at the first look, another job's state at the second: refuse, copy nothing."""
    tofu = fake([_EMPTY, _OTHER_JOB])
    with pytest.raises(mig.StateMigrationError) as exc:
        _reconcile(tmp_path)
    assert exc.value.code == "state_migration_raced"
    assert "nothing was moved" in str(exc.value)
    assert tofu.copies == []


def test_the_same_state_arriving_first_is_used_and_nothing_is_copied(fake, tmp_path):
    """Another run of this apply moved it between the two looks: that is the state."""
    tofu = fake([_EMPTY, _state("33333333-cccc", *_OLD["resources"])])
    outcome = _reconcile(tmp_path)
    assert outcome.outcome == mig.CURRENT
    assert tofu.copies == []


# ── the check after the copy ─────────────────────────────────────────────


def test_a_copy_that_reads_back_different_resources_is_an_error(fake, tmp_path):
    tofu = fake([_EMPTY, _EMPTY, _OTHER_JOB])
    with pytest.raises(mig.StateMigrationError) as exc:
        _reconcile(tmp_path)
    assert exc.value.code == "state_migration_unverified"
    assert "the old object is untouched" in str(exc.value)
    # One copy, and nothing after it: no second attempt, no clean-up write.
    assert len(tofu.copies) == 1
    assert tofu.calls[-1] == {"pull": "workdir"}


def test_a_copy_that_reads_back_the_same_resources_is_the_move(fake, tmp_path):
    fake([_EMPTY, _EMPTY, _state("44444444-dddd", *_OLD["resources"])])
    outcome = _reconcile(tmp_path)
    assert outcome.outcome == mig.MIGRATED
    assert outcome.resources == 1


# ── what the probe installs ──────────────────────────────────────────────


def test_the_probe_installs_nothing(fake, tmp_path):
    """The probe's init takes providers from an empty directory only, never
    from the apply's own ``.terraform/providers`` (whose entries a plugin
    cache links; read as a mirror they broke the workdir)."""
    (tmp_path / "workdir" / ".terraform" / "providers").mkdir(parents=True)
    tofu = fake([_EMPTY])
    assert _reconcile(tmp_path, migrate=False).outcome == mig.PENDING
    (probe,) = [c for c in tofu.calls if c.get("init") == "legacy"]
    assert probe["plugin_dir_empty"] is True
    assert ".terraform" not in probe["plugin_dir"]
    assert [c for c in tofu.calls if "init" in c] == [probe]


def test_a_probe_stopped_at_the_provider_step_reads_the_recorded_backend(fake, tmp_path):
    """The usual path for a state that names providers: init exits 1 after
    recording the old backend, and the state is pulled from it."""
    tofu = fake([_EMPTY])
    tofu.probe_init_rc, tofu.records = 1, True
    assert _reconcile(tmp_path, migrate=False).outcome == mig.PENDING
    assert {"pull": "legacy"} in tofu.calls
    assert not [c for c in tofu.calls if c.get("init") == "legacy-plain"]


def test_a_probe_that_recorded_no_backend_falls_back_to_a_plain_init(fake, tmp_path):
    tofu = fake([_EMPTY])
    tofu.probe_init_rc = 1
    assert _reconcile(tmp_path, migrate=False).outcome == mig.PENDING
    (plain,) = [c for c in tofu.calls if c.get("init") == "legacy-plain"]
    assert plain.get("plugin_dir") is None
    assert {"pull": "legacy"} not in tofu.calls  # never read from an unrecorded backend


def test_a_probe_that_cannot_initialise_at_all_is_an_error_not_an_empty_state(fake, tmp_path):
    tofu = fake([_EMPTY])
    tofu.probe_init_rc, tofu.plain_init_rc = 1, 1
    with pytest.raises(mig.StateMigrationError) as exc:
        _reconcile(tmp_path, migrate=False)
    assert exc.value.code == "state_migration_probe_failed"
    assert not [c for c in tofu.calls if str(c.get("pull", "")).startswith("legacy")]


def test_the_move_installs_what_the_old_state_names_at_the_plugin_s_pins(fake, tmp_path):
    """Once per contract, the copy's init installs providers: the pinned
    ``~> 6.0`` aws, not the latest, and only what the state names."""
    tofu = fake([_EMPTY, _EMPTY, _state("55555555-eeee", *_OLD["resources"])])
    assert _reconcile(tmp_path).outcome == mig.MIGRATED
    want = {"aws": {"source": "hashicorp/aws", "version": "~> 6.0"}}
    assert tofu.modules["move"]["terraform"]["required_providers"] == want
    assert tofu.modules["move"]["terraform"]["backend"] == _LEGACY
    assert tofu.modules["move-copy"]["terraform"]["required_providers"] == want
    assert tofu.modules["move-copy"]["terraform"]["backend"] == _CURRENT


def test_the_module_docstring_no_longer_claims_the_probe_downloads_nothing():
    assert "downloads nothing" not in (mig.__doc__ or "")
    assert "-plugin-dir" in (mig.__doc__ or "")


# ── real tofu, no registry reachable ─────────────────────────────────────

_TOFU = runner.tofu_path()
offline = pytest.mark.skipif(
    _TOFU is None or sys.platform.startswith("win"),
    reason="needs `tofu` on PATH (and a POSIX dead-proxy setup)",
)


def _offline_env(tmp_path: Path) -> Dict[str, str]:
    """Every registry request fails fast: a dead proxy, no CLI config, no cache."""
    cli_config = tmp_path / "empty.tofurc"
    cli_config.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TF_PLUGIN_CACHE_DIR"}
    env.update(
        {
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "TF_CLI_CONFIG_FILE": str(cli_config),
            "TF_IN_AUTOMATION": "1",
        }
    )
    return env


def _local_workdir(tmp_path: Path, env: Dict[str, str]) -> Dict[str, Any]:
    """The apply's workdir, initialised on a local "new key", as the apply leaves it."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    current = {"local": {"path": str(tmp_path / "new" / "terraform.tfstate")}}
    (workdir / "main.tf.json").write_text(
        json.dumps({"terraform": {"backend": current}}), encoding="utf-8"
    )
    assert runner.tofu_init(str(workdir), env=env).ok
    return current


def _legacy_file(tmp_path: Path, doc: Dict[str, Any]) -> Dict[str, Any]:
    path = tmp_path / "old" / "terraform.tfstate"
    path.parent.mkdir()
    path.write_text(json.dumps(doc), encoding="utf-8")
    return {"local": {"path": str(path)}}


@offline
def test_the_probe_of_this_provider_s_state_reaches_no_registry(tmp_path):
    env = _offline_env(tmp_path)
    current = _local_workdir(tmp_path, env)
    legacy = _legacy_file(tmp_path, _OLD)

    outcome = mig.reconcile_state_key(
        workdir=tmp_path / "workdir",
        current=current,
        legacy=legacy,
        provider="aws",
        env=env,
        migrate=False,
    )

    assert outcome.outcome == mig.PENDING
    assert outcome.resources == 1
    assert not Path(current["local"]["path"]).exists()
    assert json.loads(Path(legacy["local"]["path"]).read_text(encoding="utf-8")) == _OLD


@offline
def test_another_cloud_s_state_is_attributed_with_nothing_installed(tmp_path):
    """The gcp apply reading the aws state at the old key: the aws provider is
    not fetched to find out whose state it is."""
    env = _offline_env(tmp_path)
    current = _local_workdir(tmp_path, env)
    legacy = _legacy_file(tmp_path, _OLD)

    outcome = mig.reconcile_state_key(
        workdir=tmp_path / "workdir",
        current=current,
        legacy=legacy,
        provider="gcp",
        env=env,
        migrate=True,
    )

    assert outcome.outcome == mig.OTHER_PROVIDER
    assert "aws" in outcome.detail
    assert not Path(current["local"]["path"]).exists()


# ── one state, two clouds, on a key that names no provider ───────────────


def _target(tmp_path: Path, key: str) -> engine.StateTarget:
    return engine.StateTarget(
        workdir=tmp_path, backend={"s3": {"bucket": "b", "key": key}}, origin="--state-backend"
    )


def _stub_state(monkeypatch, doc: Dict[str, Any]) -> List[Path]:
    seen: List[Path] = []

    def read_state(workdir, env):
        seen.append(Path(workdir))
        return mig.parse_state(json.dumps(doc))

    monkeypatch.setattr(engine, "read_state", read_state)
    return seen


def test_the_shared_key_holding_the_other_cloud_s_resources_is_refused(tmp_path, monkeypatch):
    _stub_state(monkeypatch, _OLD)
    with pytest.raises(CLIError) as exc:
        engine.guard_state_shared_with_another_cloud(
            _target(tmp_path, "fluid/terraform.tfstate"), "gcp", {}
        )
    assert exc.value.event == "state_shared_with_another_provider"
    assert exc.value.context["providers"] == ["aws"]
    assert exc.value.context["state"] == "s3://b/fluid/terraform.tfstate"


@pytest.mark.parametrize(
    "doc",
    [_OLD, _EMPTY, _state("x", _resource("null_resource", "n", 'provider["x/hashicorp/null"]'))],
)
def test_this_cloud_s_own_or_no_one_s_resources_pass(tmp_path, monkeypatch, doc):
    _stub_state(monkeypatch, doc)
    engine.guard_state_shared_with_another_cloud(
        _target(tmp_path, "fluid/terraform.tfstate"), "aws", {}
    )


def test_a_key_that_names_the_provider_is_not_read(tmp_path, monkeypatch):
    seen = _stub_state(monkeypatch, _state("y", _resource("g", "d", _GOOGLE)))
    engine.guard_state_shared_with_another_cloud(
        _target(tmp_path, "fluid/p/aws/terraform.tfstate"), "aws", {}
    )
    local = engine.StateTarget(workdir=tmp_path, backend=None, origin="default")
    engine.guard_state_shared_with_another_cloud(local, "aws", {})
    assert seen == []


def test_the_bucket_only_flag_route_is_refused_end_to_end(tmp_path, monkeypatch):
    """--state-backend s3://<bucket> and a contract without packaging: both
    clouds resolve to fluid/terraform.tfstate, and the gcp apply finds aws there."""
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    contract = {"id": "bronze.customer_subscriptions", "name": "Customer Subscriptions"}
    args = argparse.Namespace(
        state_backend="s3://fluid-demo-lab-state", workspace_dir=str(tmp_path)
    )
    aws = engine.resolve_state_target(args, contract, "aws")
    gcp = engine.resolve_state_target(args, contract, "gcp")
    assert aws.backend == gcp.backend  # the shared legacy key, unchanged
    _stub_state(monkeypatch, _OLD)
    with pytest.raises(CLIError) as exc:
        engine.guard_state_shared_with_another_cloud(gcp, "gcp", {})
    assert exc.value.event == "state_shared_with_another_provider"


# ── fluid apply --dry-run never moves state ──────────────────────────────


class _Stop(Exception):
    pass


def _engine_until_the_guard(monkeypatch, tmp_path: Path, *, dry_run: bool) -> Dict[str, Any]:
    """Run the engine with tofu stubbed, up to the shared-state guard."""
    seen: Dict[str, Any] = {"inits": []}
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.customer_subscriptions",
        "name": "Customer Subscriptions",
        "metadata": {"owner": {"team": "t"}},
        "exposes": [
            {
                "exposeId": "subscriptions",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": "lake", "path": "bronze/", "database": "d"},
                },
                "contract": {"schema": [{"name": "a", "type": "VARCHAR"}]},
            }
        ],
    }
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://state-bucket")
    monkeypatch.setattr(engine, "_verify_plan_binding_for_opentofu", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_load_contract", lambda *a, **k: contract)
    monkeypatch.setattr(engine, "native_actions", lambda *a, **k: [])
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(engine.runner, "require_tofu_version", lambda *a, **k: None)
    monkeypatch.setattr(engine, "cprint", lambda *a, **k: None)

    def init(workdir, **kwargs):
        module = json.loads((Path(workdir) / "main.tf.json").read_text(encoding="utf-8"))
        seen["inits"].append((module["terraform"]["backend"]["s3"]["key"], kwargs))
        return TofuResult("init", 0, "", "")

    def reconcile(**kwargs):
        seen["migrate"] = kwargs["migrate"]
        return mig.StateReconciliation(mig.PENDING, kwargs["legacy"], kwargs["current"], 1)

    def stop(target, provider, env):
        seen["guarded"] = engine.backend_location(target.backend)
        raise _Stop

    monkeypatch.setattr(engine.runner, "tofu_init", init)
    monkeypatch.setattr(engine, "_reconcile_state", reconcile)
    monkeypatch.setattr(engine, "guard_state_shared_with_another_cloud", stop)
    args = argparse.Namespace(
        contract="c.fluid.yaml",
        env=None,
        provider="aws",
        workspace_dir=str(tmp_path),
        state_backend=None,
        dry_run=dry_run,
        allow_data_loss=False,
        no_verify_plan_binding=True,
    )
    with pytest.raises(_Stop):
        engine.apply_via_opentofu(args, logging.getLogger("test.state_migration_safety"))
    return seen


def test_a_dry_run_plans_on_the_old_key_and_never_asks_for_the_move(monkeypatch, tmp_path):
    seen = _engine_until_the_guard(monkeypatch, tmp_path, dry_run=True)
    assert seen["migrate"] is False
    # Initialised on the new key first, then re-pointed at the old one.
    assert [key for key, _ in seen["inits"]] == [
        "fluid/bronze.customer_subscriptions/aws/terraform.tfstate",
        "fluid/bronze.customer_subscriptions/terraform.tfstate",
    ]
    assert seen["inits"][1][1].get("reconfigure") is True
    assert (
        seen["guarded"] == "s3://state-bucket/fluid/bronze.customer_subscriptions/terraform.tfstate"
    )


def test_a_real_apply_asks_for_the_move(monkeypatch, tmp_path):
    seen = _engine_until_the_guard(monkeypatch, tmp_path, dry_run=False)
    assert seen["migrate"] is True
    assert len(seen["inits"]) == 1


# ── OpenTofu's built-in provider names no cloud ──────────────────────────
#
# The GCP plugin writes a ``terraform_data`` beside a table whose partitions
# expire (``lifecycle.retention`` with ``expire: true``): the trigger that
# replaces the table when its partitioning changes. OpenTofu records it under
# ``provider["terraform.io/builtin/terraform"]``. Read as a provider no plugin
# emits, it made the legacy state of every such gcp product "ambiguous", and
# the refusal blocked apply, dry-run and diff (measured on the integration of
# the governance branch with this one).

_BUILTIN = 'provider["terraform.io/builtin/terraform"]'
_GCP_LEGACY_WITH_TRIGGER = _state(
    "55555555-eeee",
    _resource("google_bigquery_dataset", "hunt_retention", _GOOGLE),
    _resource("google_bigquery_table", "hunt_retention_events", _GOOGLE),
    _resource("terraform_data", "hunt_retention_events_partitioning", _BUILTIN),
)


def test_a_gcp_state_holding_the_partition_trigger_is_the_gcp_apply_s():
    resources = _GCP_LEGACY_WITH_TRIGGER["resources"]
    assert mig.classify(resources, "gcp") == ("mine", "gcp")
    assert mig.classify(resources, "aws")[0] == "other"
    assert mig.other_clouds(resources, "aws") == frozenset({"gcp"})


def test_a_state_of_only_built_in_resources_is_still_not_guessed():
    verdict, detail = mig.classify([_resource("terraform_data", "t", _BUILTIN)], "gcp")
    assert verdict == "ambiguous"
    assert "built-in" in detail


def test_the_gcp_state_with_the_trigger_is_moved_and_nothing_pins_the_built_in(fake, tmp_path):
    moved = _state("66666666-ffff", *_GCP_LEGACY_WITH_TRIGGER["resources"])
    tofu = fake([_EMPTY, _EMPTY, moved], legacy=_GCP_LEGACY_WITH_TRIGGER)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    outcome = mig.reconcile_state_key(
        workdir=workdir,
        current={"s3": {"bucket": "b", "key": "fluid/p/gcp/terraform.tfstate"}},
        legacy=_LEGACY,
        provider="gcp",
        env={},
        migrate=True,
    )
    assert outcome.outcome == mig.MIGRATED
    assert outcome.resources == 3
    assert len(tofu.copies) == 1
    pinned = tofu.modules["move-copy"]["terraform"].get("required_providers") or {}
    assert [spec["source"] for spec in pinned.values()] == ["hashicorp/google"]
