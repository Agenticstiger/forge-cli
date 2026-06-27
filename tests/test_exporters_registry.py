# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The spec-exporter registry + `fluid exporters` — exporters have a discoverable
home WITHOUT leaking back into the provider registry."""

from __future__ import annotations

from fluid_build import providers as P
from fluid_build.providers._exporters import list_exporters


def test_builtin_exporters_listed_with_loadable_classes():
    by_name = {e.name: e for e in list_exporters()}
    assert {"odps", "odcs", "odps-bitol"} <= set(by_name), sorted(by_name)
    for name in ("odps", "odcs", "odps-bitol"):
        e = by_name[name]
        assert e.spec, f"{name} must carry a spec name"
        assert e.cls is not None, f"{name} exporter class failed to import"
        assert e.formats, f"{name} must declare at least one --format value"


def test_exporters_never_appear_in_the_provider_registry():
    # The whole point of the de-registration: an exporter must NOT be reachable as
    # a deployment provider. The registry is a separate, read-only home.
    P.discover_providers(force=True)
    providers = set(P.list_providers())
    for e in list_exporters():
        assert e.name not in providers, f"exporter {e.name!r} leaked into the provider registry"
    # belt-and-suspenders on every known exporter format spelling
    assert not (
        {"odps", "opds", "odcs", "odps_bitol", "odps-standard", "odps-bitol"} & providers
    ), f"a spec exporter leaked into providers: {sorted(providers)}"


def test_register_exporter_is_extensible_and_overridable():
    from fluid_build.providers import _exporters

    _exporters.register_exporter("custom-x", spec="My Spec v1", url=None, formats=("x",))
    assert any(e.name == "custom-x" and e.spec == "My Spec v1" for e in list_exporters())
    # cleanup so we don't pollute other tests
    _exporters.EXPORTERS.pop("custom-x", None)
