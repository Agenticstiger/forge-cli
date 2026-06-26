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

"""The provider role is governed by the unified allow/block policy, and the
public DISCOVERY_ERRORS surface no longer leaks exception text or tracebacks."""

from __future__ import annotations

import importlib.metadata as md

import pytest

from fluid_build import providers as P


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(P.PROVIDERS)
    saved_meta = dict(P._REGISTRY_META)
    saved_done = P._DISCOVERY_DONE
    saved_errs = list(P.DISCOVERY_ERRORS)
    try:
        yield
    finally:
        P.PROVIDERS.clear()
        P.PROVIDERS.update(saved)
        P._REGISTRY_META.clear()
        P._REGISTRY_META.update(saved_meta)
        P._DISCOVERY_DONE = saved_done
        P.DISCOVERY_ERRORS.clear()
        P.DISCOVERY_ERRORS.extend(saved_errs)


class _FakeEP:
    def __init__(self, name, cls):
        self.name = name
        self._cls = cls

    def load(self):
        return self._cls


class _ToyProvider:
    def plan(self, contract):
        return []

    def apply(self, actions):
        return None


# ── DISCOVERY_ERRORS redaction (the user-facing diagnostic surface) ───


def test_discovery_error_is_type_only_no_traceback():
    P.DISCOVERY_ERRORS.clear()
    P._add_discovery_error("test", "mymod", ValueError("super-secret-leak-xyz"))
    err = P.DISCOVERY_ERRORS[-1]
    # Type only — no raw message, no traceback key.
    assert err == {"source": "test", "modname": "mymod", "error": "ValueError"}
    assert "traceback" not in err
    assert all("super-secret-leak-xyz" not in str(v) for v in err.values())


# ── provider role honours the unified allow/block policy ──────────────


def test_provider_entrypoint_respects_blocklist(monkeypatch):
    monkeypatch.setattr(
        md,
        "entry_points",
        lambda *a, **k: {"fluid_build.providers": [_FakeEP("blockedprov", _ToyProvider)]},
    )
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "blockedprov")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    P._discover_entrypoints(None)
    assert "blockedprov" not in P.list_providers()  # FLUID_PLUGINS_BLOCKLIST now blocks providers


def test_provider_entrypoint_loads_when_allowed(monkeypatch):
    monkeypatch.setattr(
        md,
        "entry_points",
        lambda *a, **k: {"fluid_build.providers": [_FakeEP("okprov", _ToyProvider)]},
    )
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    P._discover_entrypoints(None)
    assert "okprov" in P.list_providers()


def test_provider_allowlist_excludes_unlisted(monkeypatch):
    monkeypatch.setattr(
        md,
        "entry_points",
        lambda *a, **k: {"fluid_build.providers": [_FakeEP("someprov", _ToyProvider)]},
    )
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "only-this-other-one")
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    P._discover_entrypoints(None)
    assert "someprov" not in P.list_providers()  # allowlist pins; unlisted provider excluded
