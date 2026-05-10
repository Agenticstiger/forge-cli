# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Destination introspection for the meltano (Singer-target) runner.

Each Singer target binary owns its own JSON-schema config. The introspector
below builds that config dict from FLUID-resolved credentials + the
contract's binding location, returning a plain dict. The runner writes the
dict to a temp ``--config`` file and pipes Singer messages into the target
on stdin.

Per /borrow-before-build receipts:
- meltanolabs-target-snowflake's config schema (queried at runtime via
  ``target-snowflake --about --format json``) is the source of truth for
  field names. They happen to match FLUID-canonical exactly (account,
  user, password, database, warehouse, role, schema), so no per-platform
  alias table is needed for snowflake. Add an alias entry only when a
  future target diverges (e.g. ``target-bigquery`` may use ``project_id``
  → ``project``).
- pydantic-settings (in fluid_build.build_runners._credentials) handles
  the operator-side layered config for the credentials themselves.

Forge-cli-owned surface: the per-platform ``schema_field`` mapping below
(some targets call it ``default_target_schema``, others call it
``schema`` — we normalise via the contract binding's schema).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .._credentials import register_engine_introspector

LOG = logging.getLogger("fluid.acquire.meltano.destinations")


# Per-platform name of the "target schema" config key. Singer targets
# disagree: target-snowflake uses ``default_target_schema``, target-bigquery
# uses ``default_target_dataset``, etc.
_TARGET_SCHEMA_KEY: Dict[str, str] = {
    "snowflake": "default_target_schema",
    "bigquery": "default_target_dataset",
    "redshift": "default_target_schema",
    "postgres": "default_target_schema",
}


@register_engine_introspector("meltano")
def _meltano_introspect(
    *,
    platform: str,
    credentials: Dict[str, Any],
    binding: Dict[str, Any],
    contract: Dict[str, Any],
    product_id: str,
) -> Dict[str, Any]:
    """Build a Singer-target config dict for ``platform``.

    Returns a plain dict ready to be JSON-serialised into ``--config``.
    The runner is responsible for writing the temp file and invoking the
    target binary; this function only translates FLUID's resolved
    credentials + binding location into the target's config shape.

    Returns ``{}`` (not ``None``) when the platform is unknown — the
    runner falls back to its DuckDB destination with a clear log line.
    """
    if platform.lower() not in _TARGET_SCHEMA_KEY:
        LOG.debug(
            "no meltano destination introspector for platform %r; runner will fall back to DuckDB",
            platform,
        )
        return {}

    config: Dict[str, Any] = {}
    # Pass-through FLUID-canonical credentials (target-snowflake's schema
    # already matches: account, user, password, database, warehouse, role).
    for key, value in credentials.items():
        if value is None or value == "":
            continue
        config[key] = value

    # Override with binding.location values (contract author's pin wins
    # over env-resolved defaults — same precedence as the dlt/airbyte
    # introspectors).
    binding_loc = (binding or {}).get("location") or {}
    for key in ("account", "database", "schema", "warehouse", "role"):
        v = binding_loc.get(key)
        if v is not None and v != "":
            config[key] = v

    # Translate the contract's per-binding ``schema`` into whichever
    # ``default_target_*`` key this target's config schema uses. Streams
    # become tables under this schema/dataset.
    schema_key = _TARGET_SCHEMA_KEY[platform.lower()]
    if "schema" in config and schema_key not in config:
        config[schema_key] = config["schema"]

    # ``add_record_metadata`` (target-snowflake default) prepends columns
    # like _SDC_BATCHED_AT / _SDC_RECEIVED_AT / _SDC_EXTRACTED_AT to every
    # row. Useful for production lineage; noisy for the FLUID demo where
    # the contract's exposed schema declares only the business columns.
    # Operators can override by setting ``add_record_metadata: true`` in
    # binding.location.
    config.setdefault("add_record_metadata", False)

    return config
