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

import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from fluid_build import providers as P
from fluid_build.cli import main

_ROOT = Path(__file__).resolve().parents[2]
_BUILTINS = {"aws", "databricks", "gcp", "local", "snowflake"}


def _run(code: str) -> str:
    """Run ``code`` in this interpreter against THIS checkout, never another copy.

    ``-I`` would ignore ``PYTHONPATH`` and fall back to whichever ``fluid_build`` is
    installed in the environment, so the child imports from ``sys.path[0]`` that is
    pinned explicitly and asserts where ``fluid_build`` came from.
    """
    root = str(_ROOT)
    guard = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import fluid_build, os;"
        f"assert os.path.realpath(fluid_build.__file__).startswith({os.path.realpath(root)!r}), "
        "fluid_build.__file__;"
    )
    out = subprocess.run(
        [sys.executable, "-s", "-c", guard + code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(_ROOT / "tests"),
        env={"PATH": "", "PYTHONPATH": root, "HOME": "/nonexistent"},
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


@pytest.fixture(autouse=True)
def _restore_registry():
    # Every test here rewrites the process-wide registry; put all of it back, or
    # later tests (here and in tests/cli) see this one's blocks and failures.
    before = P.PROVIDERS.copy()
    blocked = set(P.POLICY_BLOCKED)
    failures = dict(P.LOAD_FAILURES)
    errors = list(P.DISCOVERY_ERRORS)
    done, attempts = P._DISCOVERY_DONE, P._DISCOVERY_ATTEMPTS
    meta = dict(P._REGISTRY_META)
    yield
    P.PROVIDERS.clear()
    P.PROVIDERS.update(before)
    P.POLICY_BLOCKED.clear()
    P.POLICY_BLOCKED.update(blocked)
    P.LOAD_FAILURES.clear()
    P.LOAD_FAILURES.update(failures)
    P.DISCOVERY_ERRORS[:] = errors
    P._REGISTRY_META.clear()
    P._REGISTRY_META.update(meta)
    P._DISCOVERY_DONE, P._DISCOVERY_ATTEMPTS = done, attempts


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


# --- security review fixes: code-level capture, granted override, listing, spoofing, policy ---


def _evil_class(module="evilmod"):
    cls = type("Evil", (), {})
    cls.__module__ = module
    return cls


@pytest.mark.parametrize("name", sorted(_BUILTINS))
def test_code_cannot_take_a_builtin_name_before_discovery(name, caplog, _restore_registry):
    # What the iac_providers plugins do while the argparse tree is built.
    P.PROVIDERS.clear()
    evil = _evil_class()
    with caplog.at_level(logging.WARNING):
        P.register_provider(name, evil)
    assert not P.PROVIDERS.is_loaded(name)
    assert "provider_builtin_name_reserved" in caplog.text
    assert repr("evilmod:Evil") in caplog.text
    P.discover_providers(force=True)
    assert P.PROVIDERS.get(name) is not evil
    assert P.PROVIDERS.get(name).__module__.startswith(f"fluid_build.providers.{name}")


def test_a_class_that_only_claims_an_in_tree_module_is_refused(_restore_registry):
    # Imported first: its own import registers the real class, which is not the point here.
    importlib.import_module("fluid_build.providers.gcp")
    P.PROVIDERS.clear()
    fake = _evil_class("fluid_build.providers.gcp")
    fake.__qualname__ = "GcpProvider"

    P.register_provider("gcp", fake)
    assert not P.PROVIDERS.is_loaded("gcp")


def test_a_failed_granted_override_does_not_leave_the_name_open(
    monkeypatch, caplog, _restore_registry
):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    ep = _granting_ep()
    ep.load = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    _discover_with(monkeypatch, ep)
    assert P.PROVIDERS.get("gcp") is None  # the granted plugin failed; slot is now empty
    evil = _evil_class("some_plugin")
    with caplog.at_level(logging.WARNING):
        P.register_provider("gcp", evil)
    assert not P.PROVIDERS.is_loaded("gcp")
    assert "provider_builtin_name_reserved" in caplog.text


def test_the_granted_entry_point_is_what_is_served(monkeypatch, _restore_registry):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    ep = _granting_ep()

    def _load():
        # Importing a subclass imports the built-in, which self-registers GcpProvider.
        from fluid_build.providers.gcp import GcpProvider

        P.register_provider("gcp", GcpProvider)  # what the import's self-registration does

        class AcmeGcp(GcpProvider):
            pass

        AcmeGcp.__module__ = "acmegcp"
        ep.cls = AcmeGcp
        return AcmeGcp

    ep.load = _load
    _discover_with(monkeypatch, ep)
    served = P.PROVIDERS["gcp"]
    assert served is ep.cls
    assert P.provider_origin("gcp")["module"] == "acmegcp"


def test_detailed_listing_does_not_import_a_reserved_provider(monkeypatch, tmp_path):
    from fluid_build import plugin_manager as pm

    marker = tmp_path / "imported"
    reserved = _EP("aws")
    reserved.load = lambda: marker.write_text("x")
    granted = _EP("gcp", dist="acme-fluid-gcp")
    granted.load = lambda: marker.with_name("granted").write_text("x")
    own = _EP("mycloud")
    own.load = lambda: marker.with_name("own").write_text("x")
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-gcp:gcp")
    monkeypatch.setattr(pm, "_entry_points", lambda group: [reserved, granted, own])
    monkeypatch.setattr(pm, "governed_groups", lambda: {"provider": "fluid_build.providers"})

    entries = {e["name"]: e for e in pm.detailed_plugins("provider")["provider"]}

    assert not marker.exists()
    assert entries["aws"]["reserved"] is True and entries["aws"]["allowed"] is False
    # A granted override and an unreserved name are still loaded for their metadata.
    assert (tmp_path / "granted").exists() and (tmp_path / "own").exists()
    assert not entries["gcp"].get("reserved") and not entries["mycloud"].get("reserved")


def test_a_spoofed_first_party_distribution_is_still_reserved(
    monkeypatch, caplog, _restore_registry
):
    spoof = _EP("local", dist="data-product-forge", value="evilmod:P")
    assert P._is_first_party(spoof) is False
    real = _EP("local", dist="data-product-forge", value=P.BUILTIN_PROVIDERS["local"])
    assert P._is_first_party(real) is True
    monkeypatch.setattr(P.importlib.metadata, "entry_points", lambda: _EPs(spoof))
    P.PROVIDERS.clear()
    with caplog.at_level(logging.WARNING):
        P.discover_providers(force=True)
    assert not spoof.loaded
    assert "provider_builtin_name_reserved" in caplog.text
    # It must not be told how to grant itself.
    assert "data-product-forge:local" not in caplog.text
    assert P.PROVIDERS.pending("local").source == "builtin"


def test_a_blocklist_set_after_discovery_blocks_first_use(monkeypatch, _restore_registry):
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "local")
    assert P.PROVIDERS.get("local") is None
    assert "local" in P.POLICY_BLOCKED
    assert not P.PROVIDERS.is_loaded("local")


def test_a_forced_rediscovery_drops_a_pending_entry_that_is_now_blocked(
    monkeypatch, _restore_registry
):
    monkeypatch.setattr(P.importlib.metadata, "entry_points", _EPs)
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    assert P.PROVIDERS.pending("snowflake") is not None
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "snowflake")
    P._DISCOVERY_DONE = False  # keep the cached short-circuit out of the way
    P.discover_providers(force=True)
    assert "snowflake" not in P.PROVIDERS
    assert "snowflake" in P.POLICY_BLOCKED


def test_an_installed_but_broken_snowflake_connector_gives_the_install_hint(monkeypatch):
    from fluid_build.providers.snowflake import connection as C

    monkeypatch.setattr(C, "SNOWFLAKE_AVAILABLE", True, raising=False)
    monkeypatch.setattr(C, "snowflake", None, raising=False)
    conn = C.SnowflakeConnection.__new__(C.SnowflakeConnection)
    with pytest.raises(RuntimeError, match="pip install snowflake-connector-python"):
        conn._connect()


# --- defect-hunt fixes ---------------------------------------------------------------------


def test_generate_iac_accepts_an_iac_cloud_that_is_not_a_provider(capsys, caplog, tmp_path):
    # `confluent` has an IaC plugin and no provider; the post-parse provider check
    # used to reject it ("Unknown provider 'confluent'") before generate_iac ran.
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text("fluidVersion: 0.7.5\nkind: DataProduct\nid: x\n", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        main(["generate", "iac", str(contract), "--provider", "confluent", "--out", str(tmp_path)])
    out = capsys.readouterr()
    assert "Unknown provider" not in caplog.text + out.err + out.out


def test_generate_iac_still_rejects_a_name_that_is_neither(capsys, caplog, tmp_path):
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text("fluidVersion: 0.7.5\nkind: DataProduct\nid: x\n", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        rc = main(["generate", "iac", str(contract), "--provider", "nosuchcloud"])
    out = capsys.readouterr()
    assert rc == 2 and "Unknown provider" in caplog.text + out.err + out.out


def _failing_ep(name="mycloud", dist="mycloud-plugin"):
    ep = _EP(name, dist=dist, value="mycloud.mod:P")
    return ep


def test_a_provider_that_fails_to_import_is_named_and_remembered(
    monkeypatch, caplog, _restore_registry
):
    ep = _failing_ep()
    _discover_with(monkeypatch, ep)
    with caplog.at_level(logging.WARNING):
        assert P.PROVIDERS.get("mycloud") is None
    msg = " ".join(r.getMessage() for r in caplog.records if "mycloud" in r.getMessage())
    assert "mycloud" in msg and "RuntimeError" in msg and "mycloud-plugin" in msg
    for _ in range(2):  # the second lookup says the same, not "unknown"
        with pytest.raises(P.ProviderLoadError) as e:
            P.get_provider("mycloud")
        assert "failed to load" in str(e.value) and "RuntimeError" in str(e.value)
        assert "mycloud-plugin" in str(e.value)


def test_build_provider_reports_a_load_failure_not_unknown(monkeypatch, _restore_registry):
    from fluid_build.cli._common import CLIError, build_provider

    _discover_with(monkeypatch, _failing_ep())
    for _ in range(2):
        with pytest.raises(CLIError) as e:
            build_provider("mycloud", None, None, logging.getLogger("t"))
        assert e.value.event == "provider_load_failed"


_HIDE_SDKS = (
    "import sys\n"
    "class _B:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.startswith(('google.cloud.bigquery','google.api_core','botocore')):\n"
    "            raise ImportError(name)\n"
    "sys.meta_path.insert(0, _B())\n"
)


def test_an_absent_sdk_is_a_missing_attribute_not_an_import_error():
    code = _HIDE_SDKS + (
        "from fluid_build.providers import bigquery_validation as b, aws_validation as a;"
        "print(hasattr(b,'bigquery'), hasattr(b,'google_exceptions'), hasattr(a,'ClientError'))"
    )
    assert _run(code) == "False False False"


def test_an_installed_but_broken_boto3_is_not_available(tmp_path):
    (tmp_path / "boto3.py").write_text("raise ImportError('broken install')\n")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r});"
        "import importlib.util;"
        "assert importlib.util.find_spec('boto3') is not None;"
        "from fluid_build.providers import aws_validation as a;"
        "print(a.BOTO3_AVAILABLE, a.boto3)"
    )
    assert _run(code) == "False None"


def test_an_installed_but_broken_snowflake_connector_is_not_available(tmp_path):
    pkg = tmp_path / "snowflake"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "connector.py").write_text("raise ImportError('broken install')\n")
    code = (
        f"import sys; sys.path.insert(0, {str(tmp_path)!r});"
        "from fluid_build.providers.snowflake import connection as c;"
        "print(c.SNOWFLAKE_AVAILABLE)"
    )
    assert _run(code) == "False"


def test_pop_of_a_pending_name_returns_the_provider_and_removes_it(_restore_registry):
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    popped = P.PROVIDERS.pop("gcp", None)
    assert popped is not None and popped.__name__ == "GcpProvider"
    assert "gcp" not in P.PROVIDERS and P.PROVIDERS.pending("gcp") is None
    with pytest.raises(KeyError):
        P.PROVIDERS.pop("gcp")
    popped = P.PROVIDERS.pop("gcp", "dflt")
    assert popped == "dflt"


def test_pop_of_a_pending_name_that_fails_to_import_gives_the_default(
    monkeypatch, _restore_registry
):
    _discover_with(monkeypatch, _failing_ep())
    popped = P.PROVIDERS.pop("mycloud", "dflt")
    assert popped == "dflt"
    assert "mycloud" not in P.PROVIDERS


def test_keys_is_a_set_like_view(_restore_registry):
    P.PROVIDERS.clear()
    P.discover_providers(force=True)
    keys = P.PROVIDERS.keys()
    assert keys & {"gcp", "nope"} == {"gcp"}
    assert {"aws", "gcp"} <= keys
    assert sorted(keys) == sorted(_BUILTINS)


def test_a_provider_blocked_after_discovery_is_never_imported(monkeypatch, _restore_registry):
    ep = _EP("kube", dist="acme-fluid-kube", value="acme.kube:Kube")
    ep.load = lambda: (setattr(ep, "loaded", True), _Replacement)[1]
    _discover_with(monkeypatch, ep)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "kube")
    assert P.PROVIDERS.get("kube") is None
    assert not ep.loaded
    assert "kube" in P.POLICY_BLOCKED


@pytest.mark.parametrize(
    "module, qualname",
    [
        ("fluid_build.providers.gcp.nonexistent", "GcpProvider"),
        ("fluid_build.providers.gcp.provider", "NoSuchClass"),
    ],
)
def test_a_class_naming_no_real_in_tree_class_is_refused(module, qualname, _restore_registry):
    importlib.import_module("fluid_build.providers.gcp.provider")  # loaded: only the name decides

    P.PROVIDERS.clear()
    fake = _evil_class(module)
    fake.__qualname__ = qualname
    P.register_provider("gcp", fake)
    assert not P.PROVIDERS.is_loaded("gcp")
