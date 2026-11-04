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

"""An outside package ships an OpenTofu emitter through the
``fluid_build.iac_providers`` entry-point group.

The fixtures build a REAL throwaway distribution (a ``.dist-info`` with an
``entry_points.txt`` plus a module) in a temp dir on ``sys.path``, so
``importlib.metadata`` discovers it the way it discovers an installed package.
The module appends a line to a marker file when imported, which is how these
tests prove a plugin was, or was never, loaded.

The platform used is ``kubernetes``: the schema's ``binding.platform`` enum
admits it and no in-tree emitter serves it. (``testcloud`` would be rejected by
that closed enum at validation.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import CLIError
from fluid_build.iac import cutover
from fluid_build.iac import registry as R

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_PLUGIN_SRC = """
import os
with open(os.environ["LW8_MARKER"], "a") as _f:
    _f.write(__name__ + "\\n")


class KubePlugin:
    name = "kubernetes"
    required_providers = {"kubernetes": {"source": "hashicorp/kubernetes", "version": "~> 2.30"}}
    credential_env_vars = ("KUBECONFIG",)
    REQUIRES = None

    @classmethod
    def get_plugin_info(cls):
        class _Info:
            requires_cli = cls.REQUIRES

        return _Info()

    def emit(self, contract, actions=()):
        return {"kubernetes_namespace": {"ns": {"metadata": {"name": "dummy-ns"}}}}

    def emit_data(self, contract, actions=()):
        return {}

    def credential_env(self, env):
        return {}

    def discover_imports(self, contract, actions=()):
        return []

    def provider_block(self):
        return {}


class NeedsNewerCli(KubePlugin):
    REQUIRES = ">=999.0"


class SlowPlugin(KubePlugin):
    def __init__(self):
        import time

        with open(os.environ["LW8_MARKER"] + ".inst", "a") as _f:
            _f.write("inst\\n")
        time.sleep(0.3)
"""


def _make_dist(root: Path, group_lines: str, name: str = "fluid-testcloud") -> None:
    (root / "lw8_plugin.py").write_text(_PLUGIN_SRC)
    info = root / f"{name.replace('-', '_')}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(
        f"[fluid_build.iac_providers]\n{textwrap.dedent(group_lines)}\n"
    )


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """Install a dist exposing ``kubernetes`` -> ``KubePlugin``; yield its marker file."""
    root = tmp_path / "site"
    root.mkdir()
    marker = tmp_path / "loaded.txt"
    _make_dist(root, "kubernetes = lw8_plugin:KubePlugin")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGIN_STRICT_COMPAT", raising=False)
    saved = dict(R.IAC_PLUGINS)
    R.reset_iac_plugin_cache()
    sys.modules.pop("lw8_plugin", None)
    try:
        yield marker
    finally:
        R.reset_iac_plugin_cache()
        R.IAC_PLUGINS.clear()
        R.IAC_PLUGINS.update(saved)
        sys.modules.pop("lw8_plugin", None)


def _loaded(marker: Path) -> bool:
    return marker.exists() and marker.read_text().strip() != ""


def _contract(tmp_path: Path, platform: str = "kubernetes") -> Path:
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "p",
        "name": "P",
        "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@x.co"}},
        "exposes": [
            {
                "exposeId": "events",
                "binding": {"platform": platform, "format": "table", "location": {"name": "e"}},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(json.dumps(contract))  # JSON is valid YAML
    return path


def _generate(tmp_path: Path, contract: Path, provider: str = "auto") -> int:
    args = argparse.Namespace(
        contract=str(contract),
        provider=provider,
        out=str(tmp_path / "out"),
        env=None,
        shadow=False,
        validate=False,
        allow_empty=False,
    )
    return generate_iac.run(args, logging.getLogger("test"))


def test_importing_the_iac_package_loads_no_plugin(dist):
    code = "import fluid_build.iac, fluid_build.cli.generate_iac"
    env = {**os.environ, "LW8_MARKER": str(dist), "PYTHONPATH": str(Path(sys.path[0]))}
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    assert not _loaded(dist)


def test_help_and_providers_load_no_plugin(dist):
    root = next(p for p in sys.path if (Path(p) / "lw8_plugin.py").exists())
    env = {**os.environ, "LW8_MARKER": str(dist), "PYTHONPATH": root}
    for argv in (["--help"], ["providers"], ["generate", "iac", "--help"]):
        done = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", *argv],
            env=env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert done.returncode == 0, f"`fluid {' '.join(argv)}` failed: {done.stderr[-400:]!r}"
        assert not _loaded(dist), f"`fluid {' '.join(argv)}` imported the IaC plugin"


def test_contract_for_the_plugin_platform_emits_the_plugins_resources(dist, tmp_path):
    contract = _contract(tmp_path)
    assert _generate(tmp_path, contract) == 0
    doc = json.loads((tmp_path / "out" / "main.tf.json").read_text())
    assert doc["resource"]["kubernetes_namespace"]["ns"]["metadata"]["name"] == "dummy-ns"
    assert doc["terraform"]["required_providers"]["kubernetes"]["source"] == "hashicorp/kubernetes"
    assert _loaded(dist)


def test_provider_choices_list_the_plugin_platform_but_not_junk(dist, tmp_path):
    # The IaC platform names see the plugin (names only, nothing imported); the
    # GLOBAL --provider flag is validated elsewhere and accepts built-ins only.
    choices = R.IacProviderChoices()
    assert "kubernetes" in choices
    assert "nonesuch" not in choices
    assert _generate(tmp_path, _contract(tmp_path), provider="kubernetes") == 0


def test_the_real_cli_autodetects_a_plugin_platform(dist, tmp_path):
    root = next(p for p in sys.path if (Path(p) / "lw8_plugin.py").exists())
    env = {**os.environ, "LW8_MARKER": str(dist), "PYTHONPATH": root}
    out = tmp_path / "cli-out"
    done = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "generate", "iac",
         str(_contract(tmp_path)), "--out", str(out)],
        env=env, capture_output=True, text=True, check=False, timeout=180,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-600:]
    doc = json.loads((out / "main.tf.json").read_text())
    assert doc["resource"]["kubernetes_namespace"]["ns"]["metadata"]["name"] == "dummy-ns"
    assert _loaded(dist)


def test_the_apply_engine_routes_the_plugin_platform_through_opentofu(dist):
    assert cutover.default_engine("kubernetes") == "opentofu"
    assert cutover.resolve_engine(None, "kubernetes") == "opentofu"
    assert "kubernetes" in R.IAC_ENTRYPOINT_PLUGINS


def test_nothing_loads_until_a_platform_needs_it(dist):
    assert R.known_iac_platforms().count("kubernetes") == 1
    assert R.is_iac_entrypoint_platform("kubernetes")
    assert cutover.default_engine("aws") == "opentofu"  # in-tree: no plugin involved
    assert cutover.default_engine("local") == "native"
    assert not _loaded(dist)
    assert R.get_iac_plugin("kubernetes") is not None
    assert _loaded(dist)


def test_a_plugin_claiming_a_builtin_is_skipped_before_import_and_warned(
    dist, tmp_path, monkeypatch, caplog
):
    for claimed in ("aws", "gcp", "snowflake", "databricks", "confluent", "glue", "local"):
        root = tmp_path / f"squat-{claimed}"
        root.mkdir()
        _make_dist(root, f"{claimed} = lw8_plugin:KubePlugin", name=f"squatter-{claimed}")
        monkeypatch.syspath_prepend(str(root))
    R.reset_iac_plugin_cache()
    original = {k: v for k, v in R.IAC_PLUGINS.items()}
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        R.discover_iac_entrypoints()
        assert R.get_iac_plugin("aws") is original["aws"]
        assert cutover.default_engine("glue") == "native"
    text = caplog.text
    assert "iac_provider_name_reserved" in text
    assert "squatter-aws" in text and "'aws'" in text
    assert R.IAC_PLUGINS["aws"] is original["aws"]
    assert "glue" not in R.IAC_PLUGINS and "local" not in R.IAC_PLUGINS
    # Only the legitimate dist was imported; the squatters share lw8_plugin, so
    # one import line would mean a squatter ran. The legitimate one imports once.
    assert _loaded(dist) and dist.read_text().count("\n") == 1


def test_a_squatter_alone_is_never_imported(tmp_path, monkeypatch):
    root = tmp_path / "squat"
    root.mkdir()
    marker = tmp_path / "m.txt"
    _make_dist(root, "aws = lw8_plugin:KubePlugin", name="squatter")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    R.discover_iac_entrypoints()
    R.get_iac_plugin("aws")
    assert not _loaded(marker)
    assert "lw8_plugin" not in sys.modules


def test_a_blocked_plugin_is_never_imported_and_the_user_is_told(dist, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "kubernetes")
    R.reset_iac_plugin_cache()
    assert R.get_iac_plugin("kubernetes") is None
    assert cutover.default_engine("kubernetes") == "native"
    assert "kubernetes" not in R.known_iac_platforms()
    assert "FLUID_PLUGINS_ALLOWLIST/BLOCKLIST" in R.iac_plugin_unavailable_reason("kubernetes")
    assert not _loaded(dist)
    with pytest.raises(CLIError) as exc:
        _generate(tmp_path, _contract(tmp_path), provider="kubernetes")
    assert "blocked by FLUID_PLUGINS_ALLOWLIST/BLOCKLIST" in exc.value.context["error"]


def test_an_allowlist_that_omits_the_plugin_keeps_it_unloaded(dist, monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "aws,gcp")
    R.reset_iac_plugin_cache()
    assert R.get_iac_plugin("kubernetes") is None
    assert not _loaded(dist)


def _use_incompatible_plugin(tmp_path, monkeypatch):
    root = tmp_path / "newer"
    root.mkdir()
    _make_dist(root, "kubernetes = lw8_plugin:NeedsNewerCli", name="needs-newer-cli")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(tmp_path / "newer-marker.txt"))
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()


def test_incompatible_requires_cli_warns_by_default(tmp_path, monkeypatch, caplog):
    # The registry gate is the provider registry's own: advisory unless strict.
    monkeypatch.delenv("FLUID_PLUGIN_STRICT_COMPAT", raising=False)
    _use_incompatible_plugin(tmp_path, monkeypatch)
    try:
        with caplog.at_level(logging.WARNING):
            assert R.get_iac_plugin("kubernetes") is not None
        assert "provider_version_warning" in caplog.text
    finally:
        R.reset_iac_plugin_cache()
        sys.modules.pop("lw8_plugin", None)


def test_incompatible_requires_cli_is_refused_under_strict_compat(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FLUID_PLUGIN_STRICT_COMPAT", "1")
    _use_incompatible_plugin(tmp_path, monkeypatch)
    try:
        with caplog.at_level(logging.WARNING):
            assert R.get_iac_plugin("kubernetes") is None
        assert "provider_version_warning" in caplog.text
        assert "iac_plugin_compat_rejected" in caplog.text
        assert cutover.default_engine("kubernetes") == "native"
        assert "refused" in R.iac_plugin_unavailable_reason("kubernetes")
        # A refusal is remembered: a second ask does not re-import or re-warn.
        caplog.clear()
        assert R.get_iac_plugin("kubernetes") is None
        assert caplog.text == ""
    finally:
        R.reset_iac_plugin_cache()
        sys.modules.pop("lw8_plugin", None)


# ── operator opt-in: a distribution-qualified allowlist entry grants an override ──


def _squat(tmp_path, monkeypatch, dist_name="acme-fluid-aws", platform="aws"):
    root = tmp_path / f"squat-{platform}"
    root.mkdir()
    _make_dist(root, f"{platform} = lw8_plugin:KubePlugin", name=dist_name)
    monkeypatch.syspath_prepend(str(root))
    R.reset_iac_plugin_cache()


def test_a_qualified_grant_lets_the_plugin_replace_a_builtin(dist, tmp_path, monkeypatch, caplog):
    _squat(tmp_path, monkeypatch)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    builtin = R.IAC_PLUGINS["aws"]
    with caplog.at_level(logging.INFO, logger=R._log.name):
        plugin = R.get_iac_plugin("aws")
    assert plugin is not builtin and type(plugin).__name__ == "KubePlugin"
    assert cutover.default_engine("aws") == "opentofu"
    assert "iac_provider_override_granted" in caplog.text
    assert "acme-fluid-aws" in caplog.text and "'aws'" in caplog.text
    assert "iac_provider_name_reserved" not in caplog.text


def test_without_a_grant_the_claim_is_skipped_and_the_warning_says_how_to_opt_in(
    dist, tmp_path, monkeypatch, caplog
):
    _squat(tmp_path, monkeypatch)
    builtin = R.IAC_PLUGINS["aws"]
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        R.discover_iac_entrypoints()
    assert R.IAC_PLUGINS["aws"] is builtin
    assert "add 'acme-fluid-aws:aws' to FLUID_PLUGINS_ALLOWLIST" in caplog.text


def test_a_bare_name_in_the_allowlist_does_not_grant(dist, tmp_path, monkeypatch, caplog):
    _squat(tmp_path, monkeypatch)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "aws")
    builtin = R.IAC_PLUGINS["aws"]
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        R.discover_iac_entrypoints()
        assert R.get_iac_plugin("aws") is builtin
    assert "iac_provider_name_reserved" in caplog.text


@pytest.mark.parametrize("spelling", ["ACME_Fluid.AWS", "acme.fluid_aws", "Acme-Fluid--AWS"])
def test_distribution_name_variants_match_the_grant(dist, tmp_path, monkeypatch, spelling):
    _squat(tmp_path, monkeypatch, dist_name="Acme_Fluid.AWS")
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", f"{spelling}:AWS")
    assert type(R.get_iac_plugin("aws")).__name__ == "KubePlugin"


def test_a_grant_for_another_distribution_does_not_apply(dist, tmp_path, monkeypatch):
    _squat(tmp_path, monkeypatch)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "other-dist:aws")
    builtin = R.IAC_PLUGINS["aws"]
    assert R.get_iac_plugin("aws") is builtin


def test_a_qualified_only_allowlist_does_not_restrict_unrelated_plugins(dist, monkeypatch):
    from fluid_build import plugin_manager as PM

    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    assert PM.is_allowed("kubernetes") and PM.is_allowed("anything")
    assert R.get_iac_plugin("kubernetes") is not None
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws,gcp")
    assert PM.is_allowed("gcp") and not PM.is_allowed("kubernetes")


@pytest.mark.parametrize("retired", ["redshift", "datamesh-manager", "datamesh_manager"])
def test_a_grant_never_lifts_a_retired_name(tmp_path, monkeypatch, caplog, retired):
    root = tmp_path / "squat"
    root.mkdir()
    marker = tmp_path / "m.txt"
    _make_dist(root, f"{retired} = lw8_plugin:KubePlugin", name="acme-fluid-aws")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", f"acme-fluid-aws:{retired}")
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    with caplog.at_level(logging.INFO, logger=R._log.name):
        R.discover_iac_entrypoints()
        R.get_iac_plugin(retired)
    assert not _loaded(marker)
    assert "lw8_plugin" not in sys.modules
    assert "the retired platform" in caplog.text
    assert "iac_provider_override_granted" not in caplog.text
    assert "FLUID_PLUGINS_ALLOWLIST" not in caplog.text


# ── hardening: grant identity, policy spelling, escaping, duplicates, locking ──


def _extra_dist(tmp_path, monkeypatch, tag, lines, name):
    root = tmp_path / f"extra-{tag}"
    root.mkdir()
    _make_dist(root, lines, name=name)
    monkeypatch.syspath_prepend(str(root))
    R.reset_iac_plugin_cache()


def test_a_self_declared_name_with_a_suffix_cannot_satisfy_a_grant(
    dist, tmp_path, monkeypatch, caplog
):
    # METADATA ``Name: acme-fluid-aws spoof`` used to pass for ``acme-fluid-aws``
    # (the first word of the "<name> <version>" display string).
    _squat(tmp_path, monkeypatch, dist_name="acme-fluid-aws spoof")
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    builtin = R.IAC_PLUGINS["aws"]
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        assert R.get_iac_plugin("aws") is builtin
    assert not _loaded(dist)
    assert "iac_provider_name_reserved" in caplog.text
    assert "to allow this override" not in caplog.text  # no grant can work for that name


def test_a_mixed_case_entry_point_is_blocked_by_a_lowercase_blocklist(tmp_path, monkeypatch):
    root = tmp_path / "mixed"
    root.mkdir()
    marker = tmp_path / "mixed-marker.txt"
    _make_dist(root, "Kubernetes = lw8_plugin:KubePlugin", name="mixed-case")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "kubernetes")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    try:
        assert R.get_iac_plugin("Kubernetes") is None
        assert not _loaded(marker)
        assert not R.is_iac_entrypoint_platform("kubernetes")
        assert "kubernetes" not in R.known_iac_platforms()
    finally:
        R.reset_iac_plugin_cache()
        sys.modules.pop("lw8_plugin", None)


@pytest.fixture
def onprem(tmp_path, monkeypatch):
    root = tmp_path / "onprem"
    root.mkdir()
    marker = tmp_path / "onprem-loaded.txt"
    _make_dist(root, "on-prem = lw8_plugin:KubePlugin", name="onprem-pkg")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    for var in ("FLUID_PLUGINS_ALLOWLIST", "FLUID_PLUGINS_BLOCKLIST", "FLUID_PLUGIN_STRICT_COMPAT"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    yield marker
    R.reset_iac_plugin_cache()
    sys.modules.pop("lw8_plugin", None)


def test_hyphenated_entry_point_is_blocked_by_the_underscore_spelling(onprem, monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "on_prem")
    assert R.get_iac_plugin("on-prem") is None
    assert not _loaded(onprem)
    assert "blocked" in R.iac_plugin_unavailable_reason("on_prem")


def test_hyphenated_entry_point_loads_under_a_hyphenated_allowlist(onprem, monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "on-prem")
    assert R.get_iac_plugin("on_prem") is not None
    assert _loaded(onprem)


def test_lookup_spelling_does_not_change_the_answer(onprem):
    first = R.get_iac_plugin("On-Prem")
    second = R.get_iac_plugin("On-Prem")
    assert first is not None and first is second
    assert R.get_iac_plugin("on_prem") is first
    assert cutover.default_engine("On-Prem") == cutover.default_engine("on_prem") == "opentofu"
    assert R.is_iac_entrypoint_platform("ON-PREM")


@pytest.mark.parametrize("claimed", ["AWS", "S3", "BigQuery"])
def test_a_mixed_case_squatter_is_still_reserved(tmp_path, monkeypatch, claimed):
    root = tmp_path / "squat"
    root.mkdir()
    marker = tmp_path / "m.txt"
    _make_dist(root, f"{claimed} = lw8_plugin:KubePlugin", name="squatter")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    R.discover_iac_entrypoints()
    R.get_iac_plugin(claimed)
    assert not _loaded(marker)
    R.reset_iac_plugin_cache()


def test_control_characters_in_a_distribution_name_never_reach_the_log(
    dist, tmp_path, monkeypatch, caplog
):
    _squat(tmp_path, monkeypatch, dist_name="inj\x1b[31mRED\x1b[0m", platform="gcp")
    with caplog.at_level(logging.INFO, logger=R._log.name):
        R.discover_iac_entrypoints()
    assert "iac_provider_name_reserved" in caplog.text
    assert "\x1b" not in caplog.text
    # and the user-facing reason for a refused non-reserved plugin
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "inj_platform")
    root = tmp_path / "inj2"
    root.mkdir()
    _make_dist(root, "inj-platform = lw8_plugin:KubePlugin", name="x\x1b[2Jy")
    monkeypatch.syspath_prepend(str(root))
    R.reset_iac_plugin_cache()
    reason = R.iac_plugin_unavailable_reason("inj-platform")
    assert reason and "\x1b" not in reason


def test_a_bare_allowlist_that_omits_the_name_cancels_the_grant_and_says_so(
    dist, tmp_path, monkeypatch, caplog
):
    _squat(tmp_path, monkeypatch)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "kubernetes,acme-fluid-aws:aws")
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        R.get_iac_plugin("aws")
    cancelled = [r for r in caplog.records if "iac_provider_grant_cancelled" in r.getMessage()]
    assert len(cancelled) == 1
    assert "do not include 'aws'" in cancelled[0].getMessage()
    assert "cancelled" in R.iac_plugin_unavailable_reason("aws")
    assert not _loaded(dist)


def test_two_distributions_shipping_one_plugin_name_are_both_refused(
    dist, tmp_path, monkeypatch, caplog
):
    _extra_dist(tmp_path, monkeypatch, "b", "kubernetes = lw8_plugin:KubePlugin", "other-kube")
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        assert R.get_iac_plugin("kubernetes") is None
    assert not _loaded(dist)
    warned = [r.getMessage() for r in caplog.records if "name_ambiguous" in r.getMessage()]
    assert len(warned) == 1
    assert "fluid-testcloud" in warned[0] and "other-kube" in warned[0]
    assert "several distributions" in R.iac_plugin_unavailable_reason("kubernetes")
    assert cutover.default_engine("kubernetes") == "native"


def test_with_two_distributions_on_a_reserved_name_the_granted_one_loads(
    dist, tmp_path, monkeypatch, caplog
):
    _extra_dist(tmp_path, monkeypatch, "a", "aws = lw8_plugin:KubePlugin", "ungranted-aws")
    _extra_dist(tmp_path, monkeypatch, "b", "aws = lw8_plugin:KubePlugin", "acme-fluid-aws")
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    builtin = R.IAC_PLUGINS["aws"]
    with caplog.at_level(logging.WARNING, logger=R._log.name):
        plugin = R.get_iac_plugin("aws")
    assert plugin is not builtin and type(plugin).__name__ == "KubePlugin"
    assert "ungranted-aws" in caplog.text and "iac_provider_name_reserved" in caplog.text
    assert "acme-fluid-aws" not in caplog.text.replace("'acme-fluid-aws:aws'", "")


def test_reset_restores_the_builtin_a_granted_override_replaced(dist, tmp_path, monkeypatch):
    _squat(tmp_path, monkeypatch)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    builtin = R.IAC_PLUGINS["aws"]
    assert R.get_iac_plugin("aws") is not builtin
    R.reset_iac_plugin_cache()
    assert R.IAC_PLUGINS["aws"] is builtin


def test_concurrent_first_requests_instantiate_the_plugin_once(tmp_path, monkeypatch):
    import threading

    root = tmp_path / "slow"
    root.mkdir()
    marker = tmp_path / "slow-marker.txt"
    _make_dist(root, "slowcloud = lw8_plugin:SlowPlugin", name="slow-pkg")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("LW8_MARKER", str(marker))
    for var in ("FLUID_PLUGINS_ALLOWLIST", "FLUID_PLUGINS_BLOCKLIST"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("lw8_plugin", None)
    R.reset_iac_plugin_cache()
    results = []
    gate = threading.Barrier(4)

    def ask():
        gate.wait()
        results.append(R.get_iac_plugin("slowcloud"))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    try:
        assert len(results) == 4 and all(r is results[0] and r is not None for r in results)
        assert Path(str(marker) + ".inst").read_text().count("inst") == 1
    finally:
        R.reset_iac_plugin_cache()
        sys.modules.pop("lw8_plugin", None)


def test_the_state_guard_sees_a_plugin_cloud_that_was_never_loaded(dist):
    from fluid_build.iac import state_migration as sm

    res = [{"provider": 'provider["registry.opentofu.org/hashicorp/kubernetes"]'}]
    assert not _loaded(dist)  # lazy: nothing loaded yet
    verdict, _why = sm.classify(res, "aws")
    assert verdict == "other"
    assert sm.other_clouds(res, "aws") == frozenset({"kubernetes"})
