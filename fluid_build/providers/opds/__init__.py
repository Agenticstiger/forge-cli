# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# OPDS = the Linux Foundation / ODPI **Open Data Product Specification** v4.1
# (upstream acronym: ODPS; fluid uses OPDS to disambiguate it from Bitol's Open
# Data Product *Standard*, which lives in ``providers/odps_standard/``). It is a
# data-product SPEC / serialization format — NOT a cloud/infrastructure provider
# like aws/gcp/snowflake/local. ``OdpsProvider`` (name retains the upstream
# acronym) exports a contract to the OPDS spec via ``render()``; its ``apply()``
# is a no-op. It is therefore intentionally NOT registered in the provider
# registry and NOT advertised as a ``fluid_build.providers`` entry-point, so it
# never appears in ``fluid providers`` / ``fluid plugins`` / ``--provider`` as a
# deployment target.
#
# The class stays importable for the spec-export commands, which construct it
# DIRECTLY:  from fluid_build.providers.opds.opds import OdpsProvider
# (see cli/odps.py, cli/export_odps.py, cli/generate_standard.py). This package
# __init__ deliberately exposes no BaseProvider subclass, so the provider
# auto-discovery scan cannot re-register it — and we ALSO set the explicit
# opt-out below (consistent with the odcs / odps_standard sibling packages) so a
# future refactor that re-exposed the class here can't silently re-register it.
__fluid_no_autoregister__ = True
