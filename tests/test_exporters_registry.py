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
    # Keys are the canonical spec ids — the same ones `fluid generate standard
    # --list` prints. The LF/ODPI exporter used to be keyed by fluid's
    # letter-swap ``opds``, so the CLI had two sources of truth that disagreed
    # about which acronym was current.
    assert {"odcs", "odps", "odps-v4.1"} <= set(by_name), sorted(by_name)
    for name in ("odcs", "odps", "odps-v4.1"):
        e = by_name[name]
        assert e.spec, f"{name} must carry a spec name"
        assert e.cls is not None, f"{name} exporter class failed to import"
        assert e.formats, f"{name} must declare at least one --format value"
        assert e.formats[0] == name or name in e.formats, (
            f"{name}: the canonical format must be advertised first, got {e.formats}"
        )


def test_every_advertised_format_is_a_real_generate_standard_choice():
    """`fluid exporters` advertised `--format odps-standard`, which argparse
    rejects with "invalid choice". Every invocation this registry prints must
    actually execute."""
    from fluid_build.cli.generate_standard import SUPPORTED_FORMATS

    supported = set(SUPPORTED_FORMATS)
    for e in list_exporters():
        for fmt in (*e.formats, *e.deprecated_formats):
            assert fmt in supported, (
                f"exporter {e.name!r} advertises --format {fmt!r}, which is not in "
                f"generate standard's choices {sorted(supported)}"
            )


def test_the_deprecated_letter_swap_is_never_advertised_as_canonical():
    by_name = {e.name: e for e in list_exporters()}
    lf = by_name["odps-v4.1"]
    assert "opds" not in lf.formats, "the deprecated alias must not be a canonical format"
    assert "opds" in lf.deprecated_formats
    assert "OPDS" not in lf.spec, f"spec name still carries the letter-swap: {lf.spec!r}"


def test_exporters_is_reachable_from_the_top_level_help_index():
    """The "discoverable home for the spec exporters" was absent from
    `fluid --help`, so the only pointer to it was one `generate standard` line."""
    import inspect

    from fluid_build.cli import help_formatter

    assert '("exporters"' in inspect.getsource(help_formatter.print_main_help)


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
