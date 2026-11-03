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


"""The provider registry lists providers without importing them, and reserves the built-ins.

* ``fluid providers`` / ``--provider`` read NAMES from entry-point metadata; a
  provider class is imported once, on first use.
* A third-party entry point cannot claim a built-in name (aws, databricks, gcp,
  local, snowflake) -- it is skipped before ``ep.load()`` with a WARNING naming it.
* The remaining hardcoded provider lists agree with the registry.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

from fluid_build import providers as P
from fluid_build.cli import main

_ROOT = Path(__file__).resolve().parents[2]
_BUILTINS = {"aws", "databricks", "gcp", "local", "snowflake"}


def _run(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(_ROOT / "tests"),
        env={"PATH": "", "PYTHONPATH": str(_ROOT), "HOME": "/nonexistent"},
    )
    return out.stdout.strip()


def test_the_entry_point_block_and_the_builtin_table_agree():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["entry-points"]["fluid_build.providers"]
    assert declared == P.BUILTIN_PROVIDERS
    assert set(declared) == _BUILTINS


def test_discovery_lists_every_builtin_and_imports_none_of_them():
    code = (
        "import sys;"
        "from fluid_build import providers as P;"
        "P.discover_providers();"
        "names = P.list_providers();"
        "loaded = [m for m in sys.modules if m.startswith('fluid_build.providers.') "
        "and m.split('.')[2] in ('aws','gcp','snowflake','databricks','local')];"
        "helpers = [m for m in sys.modules if m.endswith(('_validation','quality_engine'))];"
        "heavy = [m for m in ('boto3','botocore','snowflake.connector','requests') if m in sys.modules];"
        "print(names, loaded, helpers, heavy)"
    )
    out = _run(code)
    assert out == "['aws', 'databricks', 'gcp', 'local', 'snowflake'] [] [] []"


def test_a_provider_is_imported_on_first_lookup_and_cached():
    code = (
        "import sys;"
        "from fluid_build import providers as P;"
        "P.discover_providers();"
        "assert 'fluid_build.providers.local' not in sys.modules;"
        "cls = P.PROVIDERS.get('local');"
        "assert cls.__name__ == 'LocalProvider', cls;"
        "assert P.PROVIDERS.get('local') is cls;"
        "assert P.provider_origin('local')['loaded'] is True;"
        "assert 'fluid_build.providers.gcp' not in sys.modules;"
        "print('ok')"
    )
    assert _run(code) == "ok"


class _EP:
    """An entry point from another distribution; ``load`` must never run for a reserved name."""

    def __init__(self, name, dist="evil-plugin", value="evil_pkg.provider:Evil"):
        self.name = name
        self.value = value
        self.dist = SimpleNamespace(name=dist, version="9.9")
        self.loaded = False

    def load(self):
        self.loaded = True
        raise RuntimeError("the plugin module was imported")


class _EPs:
    def __init__(self, *eps):
        self._eps = eps

    def select(self, group):
        return list(self._eps) if group == "fluid_build.providers" else []

    def get(self, group, default=()):
        return self.select(group) or list(default)


@pytest.fixture
def _restore_registry():
    before = P.PROVIDERS.copy()
    blocked = set(P.POLICY_BLOCKED)
    done = P._DISCOVERY_DONE
    meta = dict(P._REGISTRY_META)
    yield
    P.PROVIDERS.clear()
    P.PROVIDERS.update(before)
    P.POLICY_BLOCKED.clear()
    P.POLICY_BLOCKED.update(blocked)
    P._REGISTRY_META.clear()
    P._REGISTRY_META.update(meta)
    P._DISCOVERY_DONE = done


@pytest.mark.parametrize("name", sorted(_BUILTINS))
def test_a_third_party_plugin_cannot_claim_a_builtin_name(
    name, monkeypatch, caplog, _restore_registry
):
    ep = _EP(name)
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(ep))
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert not ep.loaded
    # The built-in still owns the name, and the real class is what a lookup returns.
    assert P.PROVIDERS.pending(name).source == "builtin"
    records = [
        r for r in caplog.records if getattr(r, "evt", "") == "provider_builtin_name_reserved"
    ]
    assert records, caplog.text
    assert repr("evil-plugin 9.9") in records[0].getMessage()
    assert name in records[0].getMessage()


def test_a_first_party_entry_point_may_declare_a_builtin(monkeypatch, _restore_registry):
    ep = _EP("local", dist="data_product_forge", value="fluid_build.providers.local:LocalProvider")
    ep.load = lambda: __import__("fluid_build.providers.local", fromlist=["x"]).LocalProvider
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(ep))
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    assert P.PROVIDERS.pending("local").source == "entrypoint"
    assert P.PROVIDERS.get("local").__name__ == "LocalProvider"


def test_an_entry_point_with_no_distribution_is_not_trusted_with_a_builtin_name(
    monkeypatch, caplog, _restore_registry
):
    ep = _EP("gcp")
    ep.dist = None
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(ep))
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert not ep.loaded
    assert "provider_builtin_name_reserved" in caplog.text


def test_a_plugin_with_its_own_name_is_listed_without_being_imported(
    monkeypatch, _restore_registry
):
    ep = _EP("mycloud")
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(ep))
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    assert "mycloud" in P.list_providers()
    assert not ep.loaded
    # First use imports it; a broken plugin is a typed WARNING, not a crash.
    assert P.PROVIDERS.get("mycloud") is None
    assert ep.loaded
    assert any(
        e["modname"] == "mycloud" and e["error"] == "RuntimeError" for e in P.DISCOVERY_ERRORS
    )


def test_a_plugin_that_self_registers_at_import_cannot_take_an_unloaded_builtin(
    _restore_registry,
):
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    assert P.PROVIDERS.pending("gcp") is not None and not P.PROVIDERS.is_loaded("gcp")
    P.register_provider("gcp", object)  # no override
    assert not P.PROVIDERS.is_loaded("gcp")
    assert P.PROVIDERS.get("gcp").__name__ == "GcpProvider"


def test_an_explicit_override_in_code_stays_allowed(_restore_registry):
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    P.register_provider("gcp", object, override=True)
    assert P.PROVIDERS["gcp"] is object


def test_the_retired_hint_says_so_when_the_successor_is_blocked(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "aws")
    hint = P.retired_provider_hint("redshift")
    assert hint and "use --provider aws" not in hint and "blocks" in hint
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST")
    assert "use --provider aws" in P.retired_provider_hint("redshift")


@pytest.mark.parametrize("argv", [["export", "c.yaml"], ["init", "x"], ["import", "dbt", "."]])
@pytest.mark.parametrize("name", ["redshift", "azure"])
def test_commands_with_their_own_provider_flag_use_the_registry(argv, name, capsys, monkeypatch):
    monkeypatch.delenv("FLUID_PROVIDER", raising=False)
    rc = main([*argv, "--provider", name])
    text = "".join(capsys.readouterr())
    assert rc == 2
    assert "invalid choice" not in text
    assert ("was retired" in text) if name == "redshift" else ("Unknown provider 'azure'" in text)


def test_the_remaining_lists_do_not_present_azure_or_a_retired_name_as_a_provider():
    from fluid_build.cli.forge_validation import validate_provider

    for name in ("azure", "redshift", "datamesh-manager"):
        ok, _msg = validate_provider(name)
        assert not ok, name
    assert validate_provider("databricks")[0]
    assert validate_provider("local")[0]


def test_the_forge_scaffolding_sets_are_the_forge_provider_modules():
    from fluid_build.cli.forge_copilot_runtime import COPILOT_BUILTIN_PROVIDERS
    from fluid_build.forge.core.engine import COPILOT_WARNING_ONLY_PROVIDERS

    scaffolds = {
        p.stem
        for p in (_ROOT / "fluid_build" / "forge" / "providers").glob("*.py")
        if not p.stem.startswith("_")
    }
    assert set(COPILOT_BUILTIN_PROVIDERS) == scaffolds == COPILOT_WARNING_ONLY_PROVIDERS


def test_every_in_tree_package_is_classified():
    """A new ``fluid_build/providers/<pkg>/`` is a built-in provider or says it is not."""
    packages = {
        p.name
        for p in (_ROOT / "fluid_build" / "providers").iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith(("_", "."))
    }
    assert packages == set(P.BUILTIN_PROVIDERS) | P._NON_PROVIDER_PACKAGES


# --- operator opt-in: FLUID_PLUGINS_ALLOWLIST=<distribution>:<entry-point name> -----------


class _Replacement:
    name = "gcp"


def _granting_ep(name="gcp", dist="Acme_Fluid.GCP"):
    ep = _EP(name, dist=dist, value="acme.gcp:Replacement")

    def _load():
        ep.loaded = True
        return _Replacement

    ep.load = _load
    return ep


def _discover_with(monkeypatch, ep):
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(ep))
    P.PROVIDERS.clear()
    P.discover_providers(force=True)


def test_a_granted_override_replaces_the_builtin(monkeypatch, caplog, _restore_registry):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    ep = _granting_ep()
    with caplog.at_level(logging.INFO):
        _discover_with(monkeypatch, ep)
    assert P.PROVIDERS.pending("gcp").ep is ep
    assert P.PROVIDERS.get("gcp") is _Replacement
    info = [
        r for r in caplog.records if getattr(r, "evt", "") == "provider_builtin_override_granted"
    ]
    assert info and "Acme_Fluid.GCP" in info[0].getMessage()


@pytest.mark.parametrize("variant", ["acme-fluid-gcp", "ACME_FLUID_GCP", "acme.fluid.gcp"])
def test_the_distribution_name_is_matched_normalised(variant, monkeypatch, _restore_registry):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", f"{variant}:GCP")
    _discover_with(monkeypatch, _granting_ep())
    assert P.PROVIDERS.get("gcp") is _Replacement


def test_without_a_grant_it_is_skipped_with_the_opt_in_hint(monkeypatch, caplog, _restore_registry):
    ep = _granting_ep()
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert not ep.loaded
    assert P.PROVIDERS.pending("gcp").source == "builtin"
    assert "add 'Acme_Fluid.GCP:gcp' to FLUID_PLUGINS_ALLOWLIST" in caplog.text


def test_a_bare_name_does_not_grant(monkeypatch, caplog, _restore_registry):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "gcp,local")
    ep = _granting_ep()
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert not ep.loaded and "provider_builtin_name_reserved" in caplog.text


def test_a_qualified_only_allowlist_does_not_restrict_other_plugins(monkeypatch, _restore_registry):
    from fluid_build.plugin_manager import is_allowed

    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    assert is_allowed("anything") and is_allowed("local")
    _discover_with(monkeypatch, _EP("mycloud"))
    assert "mycloud" in P.list_providers()


def test_a_retired_name_cannot_be_granted(monkeypatch, caplog, _restore_registry):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:redshift")
    ep = _granting_ep(name="redshift")
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert not ep.loaded and "redshift" not in P.PROVIDERS
    assert "provider_retired_name_reserved" in caplog.text


# --- security-review hardening of the opt-in -------------------------------------------


def test_a_self_declared_name_with_a_version_suffix_cannot_satisfy_a_grant(
    monkeypatch, caplog, _restore_registry
):
    # Name: "acme-fluid-gcp spoof" -> the display string's first word is the granted name.
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    ep = _granting_ep(dist="acme-fluid-gcp spoof")
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert not ep.loaded
    assert P.PROVIDERS.pending("gcp").source == "builtin"
    assert "provider_builtin_name_reserved" in caplog.text


def test_policy_and_reservation_compare_names_on_one_key(monkeypatch, caplog, _restore_registry):
    from fluid_build.plugin_manager import is_allowed

    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "my_cloud")
    assert not is_allowed("My-Cloud")
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST")
    # A differently-cased claim of a built-in name is still a claim.
    ep = _granting_ep(name="GCP")
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert not ep.loaded and "provider_builtin_name_reserved" in caplog.text


def test_the_opt_in_hint_prints_no_raw_control_characters(monkeypatch, caplog, _restore_registry):
    ep = _granting_ep(dist="evil\x1b[31m-gcp")
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, ep)
    assert "\x1b" not in caplog.text
    assert "to allow it" not in caplog.text  # no valid distribution name: no grant can work
    # A valid name is offered, quoted with repr.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _discover_with(monkeypatch, _granting_ep(dist="acme-gcp"))
    assert "add 'acme-gcp:gcp' to FLUID_PLUGINS_ALLOWLIST" in caplog.text


def test_two_distributions_with_a_built_in_name(monkeypatch, caplog, _restore_registry):
    a, b = _granting_ep(dist="acme-a"), _granting_ep(dist="acme-b")
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(a, b))

    # Neither granted: each claim is warned about.
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert caplog.text.count("provider_builtin_name_reserved") == 2

    # Exactly one granted: it loads, the other is still warned about.
    caplog.clear()
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-b:gcp")
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert P.PROVIDERS.pending("gcp").ep is b
    assert caplog.text.count("provider_builtin_name_reserved") == 1

    # Both granted: ambiguous, so neither loads and the built-in stays.
    caplog.clear()
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-a:gcp,acme-b:gcp")
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert P.PROVIDERS.pending("gcp").source == "builtin"
    assert caplog.text.count("several distributions are granted") == 2
