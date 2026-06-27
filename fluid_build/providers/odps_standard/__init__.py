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

from .odps import OdpsStandardProvider
from .provider import BitolOdpsProvider

# Bitol/ODPS-standard is a data-product SPEC / export format, not a cloud
# provider — so these exporters are intentionally NOT registered in the
# provider registry and not advertised as `fluid_build.providers` entry-points
# (they never appear in `fluid providers` / `fluid plugins` / `--provider`).
# The classes stay importable for the spec-export commands, which construct
# them DIRECTLY:  from fluid_build.providers.odps_standard import (
#     BitolOdpsProvider, OdpsStandardProvider)
# (see cli/odps.py, cli/odps_standard.py, cli/generate_standard.py).
#
# Set the opt-out EXPLICITLY (like the odps/odcs sibling packages) rather than
# relying on the accident that this module exposes two BaseProvider subclasses
# and so escapes the single-subclass auto-register fallback. Otherwise a future
# refactor that left a single exporter class here would silently re-register it.
__fluid_no_autoregister__ = True

__all__ = ["BitolOdpsProvider", "OdpsStandardProvider"]
