# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Bitol Open Data Product Standard v1.0.0 provider.

Public surface:
  - :class:`BitolOdpsProvider` — the new modular implementation.
  - :class:`OdpsStandardProvider` — back-compat alias (same class).

Specification: https://github.com/bitol-io/open-data-product-standard
"""

from fluid_build.providers import register_provider

from .odps import OdpsStandardProvider
from .provider import BitolOdpsProvider

# Register under both names — the canonical name is ``odps_bitol`` per the
# Phase 5 disambiguation plan; the ``odps-standard`` legacy name still works.
register_provider("odps_bitol", BitolOdpsProvider)
register_provider("odps-standard", OdpsStandardProvider)

__all__ = ["BitolOdpsProvider", "OdpsStandardProvider"]
