# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Registry of spec EXPORTERS — the discoverable home for odps / odcs / ….

An *exporter* serializes a FLUID contract to a data-product / data-contract SPEC
(Bitol ODPS, LF/ODPI ODPS, Bitol ODCS). It is NOT a cloud/infrastructure
provider: its ``apply()`` refuses, it only ``render()``s. Exporters are
de-registered from the provider registry (each package sets
``__fluid_no_autoregister__``); this is their parallel, read-only home so they
are still discoverable via ``fluid exporters`` without polluting
``fluid providers`` / ``--provider``.

This registry NEVER writes into :data:`fluid_build.providers.PROVIDERS`. Built-in
exporters are registered lazily (their classes are imported only when the list
is requested), so importing this module costs nothing.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExporterInfo:
    """A registered spec exporter."""

    name: str
    spec: str  # human spec name, e.g. "Bitol Open Data Contract Standard v3.1.0"
    url: Optional[str]  # canonical spec URL
    formats: Tuple[str, ...]  # `fluid generate standard --format <x>` values it serves
    cls: Optional[type] = None  # exporter class (None if it failed to import)


EXPORTERS: Dict[str, ExporterInfo] = {}


def register_exporter(
    name: str,
    *,
    spec: str,
    url: Optional[str] = None,
    formats: Tuple[str, ...] = (),
    cls: Optional[type] = None,
) -> None:
    """Register (or override) a spec exporter. Extensible for out-of-tree exporters."""
    EXPORTERS[name] = ExporterInfo(name, spec, url, tuple(formats), cls)


# (name, module, class, spec, url, formats) — classes imported lazily.
_BUILTINS: Tuple[Tuple[str, str, str, str, str, Tuple[str, ...]], ...] = (
    (
        "odps",
        "fluid_build.providers.odps.odps",
        "OdpsProvider",
        "LF/ODPI Open Data Product Specification v4.1",
        "https://github.com/Open-Data-Product-Initiative/v4.1",
        ("odps", "opds"),
    ),
    (
        "odcs",
        "fluid_build.providers.odcs.provider",
        "OdcsProvider",
        "Bitol Open Data Contract Standard v3.1.0",
        "https://github.com/bitol-io/open-data-contract-standard",
        ("odcs",),
    ),
    (
        "odps-bitol",
        "fluid_build.providers.odps_standard.provider",
        "BitolOdpsProvider",
        "Bitol Open Data Product Standard v1.0.0",
        "https://github.com/bitol-io/open-data-product-standard",
        ("odps-bitol", "odps-standard"),
    ),
)


def _register_builtins() -> None:
    for name, mod, cls_name, spec, url, fmts in _BUILTINS:
        if name in EXPORTERS:
            continue
        cls = None
        try:
            cls = getattr(importlib.import_module(mod), cls_name)
        except Exception as e:  # noqa: BLE001 - listing must not crash on a bad exporter
            log.warning("exporter %r class unavailable: %s", name, type(e).__name__)
        register_exporter(name, spec=spec, url=url, formats=fmts, cls=cls)


def list_exporters() -> List[ExporterInfo]:
    """Return all registered exporters (built-ins lazily registered), name-sorted."""
    _register_builtins()
    return [EXPORTERS[k] for k in sorted(EXPORTERS)]
