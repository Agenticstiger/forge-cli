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

"""Principle, not a name list: a *provider* deploys; an *exporter* does not.

A registered provider whose ``apply()`` is a no-op or raises "does not support
apply" is a spec exporter masquerading as a provider (the ODPS / ODCS bug). This
introspects EVERY registered provider so the next sibling can't slip through —
replacing the per-name invariant with the underlying rule.
"""

from __future__ import annotations

import inspect

from fluid_build import providers as P

# Source markers of a non-deploying apply() — an exporter, not a provider.
_NON_DEPLOYING_MARKERS = (
    "does not support apply",
    "export action acknowledged",
    "use render() for actual export",
    # An apply() whose body is just a refusal is an exporter, not a provider —
    # cover the common refusal shapes so a future sibling can't slip past with
    # different wording.
    "notimplementederror",
    "not implemented",
)


def _apply_is_non_deploying(cls) -> bool:
    apply = getattr(cls, "apply", None)
    if apply is None:
        return False
    try:
        src = inspect.getsource(apply).lower()
    except (OSError, TypeError):
        return False
    return any(marker in src for marker in _NON_DEPLOYING_MARKERS)


def test_no_registered_provider_is_a_non_deploying_exporter():
    P.discover_providers(force=True)
    offenders = []
    for name in sorted(P.list_providers()):
        cls = P.PROVIDERS.get(name)
        if cls is not None and _apply_is_non_deploying(cls):
            offenders.append(name)
    assert not offenders, (
        "these registered providers have a non-deploying apply() — they are spec "
        "EXPORTERS, not deployment providers, and must not be in the provider "
        f"registry (de-register them, keep the class importable): {offenders}"
    )


def test_odcs_specifically_not_a_provider():
    # The instance that motivated the principle: ODCS (Open Data Contract Standard)
    # is an exporter, de-registered like ODPS.
    P.discover_providers(force=True)
    assert "odcs" not in set(P.list_providers())


def test_odcs_exporter_still_importable_directly():
    from fluid_build.providers.odcs import OdcsProvider

    assert hasattr(OdcsProvider(), "render")


def test_exporter_packages_explicitly_opt_out_of_autoregistration():
    # The exporter packages must de-register EXPLICITLY (set
    # __fluid_no_autoregister__), not rely on incidental properties of their
    # module namespace (e.g. exposing >1 BaseProvider subclass). Otherwise a
    # refactor could silently re-register them as providers.
    import importlib

    for modname in (
        "fluid_build.providers.opds",
        "fluid_build.providers.odcs",
        "fluid_build.providers.odps_standard",
    ):
        mod = importlib.import_module(modname)
        assert getattr(mod, "__fluid_no_autoregister__", False) is True, (
            f"{modname} must set __fluid_no_autoregister__ = True (it is an exporter, "
            "not a provider) instead of relying on auto-discovery accidents"
        )
