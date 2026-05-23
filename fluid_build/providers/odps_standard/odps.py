# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Back-compat shim for the legacy ``OdpsStandardProvider``.

The Bitol ODPS provider was split into :mod:`.provider`, :mod:`.mappers`,
:mod:`.validation`, and :mod:`.io` in the Phase 2 modular refactor. The
legacy ``OdpsStandardProvider`` class continues to work — it now subclasses
:class:`BitolOdpsProvider` and overrides the provider ``name`` so existing
plugin registrations keep functioning.

New code should import from :mod:`fluid_build.providers.odps_standard`
directly::

    from fluid_build.providers.odps_standard import BitolOdpsProvider
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, Optional, Union

from fluid_build.providers.base import ProviderError

from .provider import BitolOdpsProvider as _BitolOdpsProvider


class OdpsStandardProvider(_BitolOdpsProvider):
    """Legacy class name — kept for back-compat with the original Phase-2 surface.

    Differs from :class:`BitolOdpsProvider` only in:
      - ``name`` property returns ``"odps-standard"`` (legacy).
      - ``odps_version`` / ``odps_spec_url`` attributes (legacy names).
      - ``render`` returns the bare product dict (not the
        ``{"product": ..., "contracts": ...}`` envelope) so existing CLI
        callers and tests keep working unchanged.
    """

    def __init__(self) -> None:
        super().__init__()
        # Legacy attribute names — tests reference these directly.
        self.odps_version = self.api_version
        self.odps_spec_url = self.spec_url
        # Match upstream default; can be overridden via ODPS_DEFAULT_PORT_VERSION.
        import os as _os
        self.default_port_version = _os.getenv("ODPS_DEFAULT_PORT_VERSION", "1.0.0")
        # Legacy callers (e.g. DataMeshManagerProvider's publish path) feed
        # partially-populated contracts to this back-compat alias and expect
        # the render call to succeed even when the per-port ODCS misses
        # spec-required server fields. New code should use BitolOdpsProvider
        # directly to opt into strict validation (ODPS_STRICT=true).
        self.strict_validation = False

    @property
    def name(self) -> str:
        return "odps-standard"

    def render(  # type: ignore[override]
        self,
        src: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
        *,
        out: Optional[Union[Path, str]] = None,
        fmt: Optional[str] = "yaml",
        out_dir: Optional[Union[Path, str]] = None,
    ) -> Dict[str, Any]:
        """Render returning the bare product dict (legacy contract).

        Use :class:`BitolOdpsProvider.render` directly if you want the
        product + per-port contracts bundle.
        """
        bundle = super().render(src, out=out, fmt=fmt, out_dir=out_dir)
        return bundle["product"]


__all__ = ["OdpsStandardProvider"]
