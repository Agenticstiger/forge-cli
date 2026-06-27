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

"""ODPS is the Open Data Product STANDARD (a spec/export format), NOT a cloud
provider. It must never appear in the provider registry, `fluid providers`,
`fluid plugins`, or the `--provider` choices — while the OdpsProvider exporter
stays importable for `fluid odps`.
"""

from __future__ import annotations

from fluid_build import providers as P


def test_odps_and_opds_not_in_provider_registry():
    P.discover_providers(force=True)
    names = set(P.list_providers())
    assert "odps" not in names, f"odps must not be a registry provider; got {sorted(names)}"
    assert "opds" not in names, f"opds must not be a registry provider; got {sorted(names)}"


def test_odps_not_a_provider_entry_point():
    # The entry-point reader (what `fluid plugins` uses) must not list odps.
    from fluid_build.plugin_manager import installed_plugins

    provider_names = {e["name"] for e in installed_plugins("provider").get("provider", [])}
    assert (
        "odps" not in provider_names
    ), f"odps must not be a provider entry-point; got {sorted(provider_names)}"
    assert "opds" not in provider_names


def test_odps_package_init_exposes_no_baseprovider_subclass():
    # With no BaseProvider subclass in the package namespace, the auto-register
    # discovery scan can never re-register odps.
    import inspect

    import fluid_build.providers.odps as odps_pkg
    from fluid_build.providers.base import BaseProvider

    subclasses = [
        obj
        for _, obj in inspect.getmembers(odps_pkg, inspect.isclass)
        if issubclass(obj, BaseProvider) and obj is not BaseProvider
    ]
    assert subclasses == [], f"odps/__init__ must expose no BaseProvider subclass; got {subclasses}"


def test_odps_exporter_still_importable_directly():
    # The spec-export commands construct it directly — de-registration must not
    # break that path.
    from fluid_build.providers.odps.odps import OdpsProvider

    provider = OdpsProvider()
    assert hasattr(provider, "render")


def test_provider_choices_drop_odps_opds():
    from fluid_build.cli import build_parser

    parser = build_parser()
    # Find the --provider argument's choices anywhere in the tree.
    found_choices = None

    def _walk(p):
        nonlocal found_choices
        for action in p._actions:
            if "--provider" in getattr(action, "option_strings", []) and action.choices:
                found_choices = set(action.choices)
        subs = [a for a in p._actions if hasattr(a, "choices") and isinstance(a.choices, dict)]
        for s in subs:
            for sp in (s.choices or {}).values():
                _walk(sp)

    _walk(parser)
    if found_choices is not None:
        assert "odps" not in found_choices
        assert "opds" not in found_choices
