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

"""Tests for CatalogAdapter-role dispatch + the `fluid publish` wiring."""

from __future__ import annotations

from fluid_build import plugin_manager as PM


class _FakeEP:
    def __init__(self, name, obj):
        self.name = name
        self._obj = obj

    def load(self):
        return self._obj


class _Result:
    def __init__(self, applied=0, failed=0):
        self.applied = applied
        self.failed = failed


class _ToyCatalog:
    """Duck-typed fluid_sdk.CatalogAdapter."""

    def plan(self, contract):
        return [
            {"op": "register_catalog_entry", "resource_id": str(contract.get("id", "x"))},
            {"op": "register_catalog_entry", "resource_id": "extra"},
        ]

    def apply(self, actions):
        return _Result(applied=len(list(actions)), failed=0)


class _BoomCatalog:
    def plan(self, contract):
        raise ValueError("leaked-secret-token-zzz")


# ── has_plugins ───────────────────────────────────────────────────────


def test_has_plugins(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("toy", _ToyCatalog)])
    assert PM.has_plugins("fluid_build.catalog_adapters") is True
    monkeypatch.setattr(PM, "_entry_points", lambda group: [])
    assert PM.has_plugins("fluid_build.catalog_adapters") is False


def test_has_plugins_does_not_load(monkeypatch):
    # has_plugins must read names only — never call .load().
    class _NoLoadEP:
        name = "toy"

        def load(self):
            raise AssertionError("has_plugins must not load the plugin")

    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_NoLoadEP()])
    assert PM.has_plugins("fluid_build.catalog_adapters") is True


# ── dispatch_catalog_adapters ─────────────────────────────────────────


def test_dispatch_plans_and_applies(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("toycat", _ToyCatalog)])
    out = PM.dispatch_catalog_adapters({"id": "p1"}, dry_run=False)
    assert len(out) == 1
    s = out[0]
    assert s["plugin"] == "toycat"
    assert s["planned"] == 2
    assert s["applied"] == 2
    assert s["failed"] == 0
    assert s["ok"] is True


def test_dispatch_dry_run_plans_only(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)

    applied_called = {"n": 0}

    class _Watch(_ToyCatalog):
        def apply(self, actions):
            applied_called["n"] += 1
            return _Result(applied=1)

    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("toycat", _Watch)])
    out = PM.dispatch_catalog_adapters({"id": "p1"}, dry_run=True)
    assert out[0]["planned"] == 2
    assert out[0]["applied"] == 0
    assert applied_called["n"] == 0  # apply() never called on dry-run


def test_dispatch_isolates_raising_plugin_typed(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("badcat", _BoomCatalog)])
    out = PM.dispatch_catalog_adapters({}, dry_run=False)
    assert len(out) == 1
    assert out[0]["ok"] is False
    assert out[0]["error"] == "ValueError"  # typed only; no secret text


def test_dispatch_noop_when_none(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [])
    assert PM.dispatch_catalog_adapters({"id": "x"}) == []


def test_blocklist_suppresses_catalog_adapter(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("toycat", _ToyCatalog)])
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "toycat")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    assert PM.dispatch_catalog_adapters({"id": "p1"}) == []


# ── publish.py fold ───────────────────────────────────────────────────


def test_run_catalog_adapters_skips_when_none(monkeypatch):
    from fluid_build.cli import publish as V

    monkeypatch.setattr(PM, "has_plugins", lambda group: False)
    # load_contract must NOT be called when nothing is installed.
    called = {"load": 0}
    import fluid_build.loader as loader

    monkeypatch.setattr(loader, "load_contract", lambda p: called.__setitem__("load", 1))

    class _Args:
        dry_run = False

    import logging

    V._run_catalog_adapters(["a.yaml"], _Args(), logging.getLogger("t"))
    assert called["load"] == 0  # short-circuited, no contract loaded


def test_run_catalog_adapters_dispatches(monkeypatch):
    from fluid_build.cli import publish as V

    monkeypatch.setattr(PM, "has_plugins", lambda group: True)
    import fluid_build.loader as loader

    monkeypatch.setattr(loader, "load_contract", lambda p: {"id": "loaded"})

    seen = []
    monkeypatch.setattr(
        PM,
        "dispatch_catalog_adapters",
        lambda contract, dry_run=False, logger=None: seen.append((contract, dry_run))
        or [{"plugin": "p", "planned": 1, "ok": True}],
    )

    class _Args:
        dry_run = True

    import logging

    V._run_catalog_adapters(["a.yaml", "b.yaml"], _Args(), logging.getLogger("t"))
    assert len(seen) == 2  # one dispatch per contract
    assert seen[0] == ({"id": "loaded"}, True)


def _patch_adapters(monkeypatch, summaries):
    """Install a fake catalog-adapter dispatch returning ``summaries``."""
    import fluid_build.loader as loader

    monkeypatch.setattr(PM, "has_plugins", lambda group: True)
    monkeypatch.setattr(loader, "load_contract", lambda p: {"id": "prod.a"})
    monkeypatch.setattr(
        PM,
        "dispatch_catalog_adapters",
        lambda contract, dry_run=False, logger=None: list(summaries),
    )


def test_run_catalog_adapters_reports_failure_as_publish_result(monkeypatch):
    """A plugin whose ``plan()`` raises must come back as a FAILED result.

    Regression: ``_run_catalog_adapters`` returned ``None``, so the exit
    code was computed purely from the registrar results and an adapter
    that synced nothing still produced exit 0 under a ✅ Success table.
    """
    import logging

    from fluid_build.cli import publish as V

    _patch_adapters(
        monkeypatch,
        [{"plugin": "lin-test-catalog", "planned": 0, "ok": False, "error": "RuntimeError"}],
    )

    class _Args:
        dry_run = False

    out = V._run_catalog_adapters(["a.yaml"], _Args(), logging.getLogger("t"))
    assert len(out) == 1
    assert out[0].success is False
    assert out[0].catalog_id == "adapter:lin-test-catalog"
    assert "RuntimeError" in (out[0].error or "")


def test_run_catalog_adapters_success_is_a_successful_result(monkeypatch):
    import logging

    from fluid_build.cli import publish as V

    _patch_adapters(monkeypatch, [{"plugin": "toycat", "planned": 2, "ok": True}])

    class _Args:
        dry_run = False

    out = V._run_catalog_adapters(["a.yaml"], _Args(), logging.getLogger("t"))
    assert [r.success for r in out] == [True]
    assert out[0].error is None
