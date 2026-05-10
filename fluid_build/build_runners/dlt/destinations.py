# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Destination introspection for the dlt runner.

Replaces ~200 lines of hardcoded per-destination factories with one ~50-line
introspector that walks dlt's OWN destination spec to discover what fields
each destination's ``credentials_class`` declares — then populates the
matching ``DESTINATION__<PLATFORM>__CREDENTIALS__<FIELD>`` env vars from
FLUID's resolved credentials. dlt itself picks the auth flow (password vs
keypair vs OAuth) based on which fields end up populated.

Adding support for a new dlt destination = ZERO code here. The dlt SDK's
destination class is read at runtime; new destinations work as soon as
they ship in dlt.

Per /borrow-before-build receipts:
- dlt's own credential resolution chain handles env→TOML→vault → SDK auth
  flow detection. We just bridge FLUID-canonical naming to dlt's namespace.
  Docs: https://dlthub.com/docs/general-usage/credentials/setup
- Pydantic-settings (in fluid_build.build_runners._credentials) handles
  the operator-side layered config (init→env→.env→secrets-dir→defaults).
  Docs: https://docs.pydantic.dev/latest/concepts/pydantic_settings/

Forge-cli-owned surface: the per-platform field-name aliases below
(FLUID-canonical → dlt-SDK-canonical, when they differ).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from .._credentials import register_engine_introspector

LOG = logging.getLogger("fluid.acquire.dlt.destinations")


# FLUID-canonical → dlt-SDK-canonical field-name aliases. Only listed when
# the names differ; most fields are 1:1 (username, password, database,
# warehouse, role, project_id, …). One entry per platform — small and
# easy to extend when a new dlt destination has a quirky field name.
_FLUID_TO_DLT_FIELD: Dict[str, Dict[str, str]] = {
    "snowflake": {
        # FLUID's SNOWFLAKE_ACCOUNT → dlt's snowflake spec calls it 'host'.
        "account": "host",
        # FLUID's user → dlt's snowflake spec uses 'username'.
        "user": "username",
    },
    "redshift": {
        "user": "username",
    },
    "postgres": {
        "user": "username",
    },
    "bigquery": {
        # google_application_credentials path → dlt expects parsed JSON
        # private_key + client_email; expanded by _expand_bigquery_sa() below
        # rather than a direct field rename.
    },
    # Add more as new dlt destinations are demoed:
    # "databricks": {...},
    # "mssql": {...},
}


def _value_for_dlt_field(platform: str, dlt_field: str, fluid_credentials: Dict[str, Any]) -> Any:
    """Return the FLUID-resolved value to plug into a given dlt SDK field.

    Looks up the reverse alias (dlt field → FLUID field) — most fields
    are 1:1 so the dlt name IS the FLUID name. For platform-specific
    quirks the alias table above maps explicitly.
    """
    aliases = _FLUID_TO_DLT_FIELD.get(platform.lower(), {})
    # Reverse lookup: which FLUID field maps to this dlt field?
    for fluid_name, dlt_name in aliases.items():
        if dlt_name == dlt_field and fluid_name in fluid_credentials:
            return fluid_credentials[fluid_name]
    # Fallback: same name in both
    return fluid_credentials.get(dlt_field)


def _expand_bigquery_service_account(fluid_credentials: Dict[str, Any]) -> None:
    """If GOOGLE_APPLICATION_CREDENTIALS is a SA JSON path, expand it into
    the ``private_key`` + ``client_email`` fields dlt's bigquery destination
    expects, in-place on the credentials dict.

    google.auth itself does this transparently for ADC; dlt's destination
    config layer wants the parsed values. This is the one place where
    a path needs unwrapping into individual fields.
    """
    sa_path = fluid_credentials.get("google_application_credentials")
    if not sa_path:
        return
    try:
        import json

        sa_text = Path(sa_path).expanduser().read_text()
        sa = json.loads(sa_text)
    except (OSError, json.JSONDecodeError) as exc:
        LOG.debug("BigQuery SA expansion skipped (will let google.auth handle it): %s", exc)
        return
    fluid_credentials.setdefault("private_key", sa.get("private_key"))
    fluid_credentials.setdefault("client_email", sa.get("client_email"))
    if not fluid_credentials.get("project_id"):
        fluid_credentials["project_id"] = sa.get("project_id")


@register_engine_introspector("dlt")
def _dlt_introspect(
    *,
    platform: str,
    credentials: Dict[str, Any],
    binding: Dict[str, Any],
    contract: Dict[str, Any],
    product_id: str,
) -> None:
    """Bridge FLUID-resolved credentials → ``DESTINATION__<X>__CREDENTIALS__*``.

    Walks dlt's OWN ``destination().spec().credentials_class`` to discover
    the field names dlt expects, then sets each as an env var (where the
    operator hasn't already exported one — operator overrides always win).

    Returns ``None``; dlt reads the env vars itself when ``pipeline.run``
    constructs the destination client. dlt picks the auth flow (password,
    keypair, OAuth) based on which fields end up populated.
    """
    try:
        import dlt
    except ImportError:
        LOG.warning("dlt not installed; cannot introspect destination credentials")
        return

    # BigQuery quirk: SA JSON path needs unwrapping.
    if platform.lower() == "bigquery":
        _expand_bigquery_service_account(credentials)

    # Discover the destination's required fields via dlt's own spec.
    # ``destination.spec()`` returns a ``DestinationClientConfiguration``;
    # ``.credentials_type()`` is a CLASSMETHOD (not an attribute) that
    # returns the credentials class — confirmed via dlt source. Older API
    # used ``credentials_class`` attribute; we try both.
    try:
        dest_factory = getattr(dlt.destinations, platform.lower(), None)
        if dest_factory is None:
            LOG.warning(
                "dlt has no destination named %r; skipping credential bridge",
                platform,
            )
            return
        spec = dest_factory().spec()
        cred_class = None
        # Newer dlt (1.x): spec.credentials_type() is a classmethod.
        ct = getattr(spec, "credentials_type", None)
        if callable(ct):
            try:
                cred_class = ct()
            except TypeError:
                # Some dlt versions need it called on the class
                cred_class = ct
        # Older dlt: spec.credentials_class attribute.
        if cred_class is None or not isinstance(cred_class, type):
            cred_class = getattr(spec, "credentials_class", None)
        if cred_class is None:
            LOG.debug("dlt destination %r has no credentials class; skipping", platform)
            return
        # Field-name discovery — works for dataclasses (current dlt) and
        # pydantic models (future dlt). Filter out dunder/internal fields.
        if hasattr(cred_class, "__dataclass_fields__"):
            sdk_fields = [f for f in cred_class.__dataclass_fields__ if not f.startswith("_")]
        elif hasattr(cred_class, "model_fields"):
            sdk_fields = [f for f in cred_class.model_fields if not f.startswith("_")]
        elif hasattr(cred_class, "__fields__"):
            sdk_fields = [f for f in cred_class.__fields__ if not f.startswith("_")]
        else:
            LOG.debug("Could not introspect fields for %r; skipping", platform)
            return
    except Exception as exc:  # noqa: BLE001 — defensive across dlt versions
        LOG.debug("dlt destination introspection failed for %r: %s", platform, exc)
        return

    # Set DESTINATION__<PLATFORM>__CREDENTIALS__<FIELD> for each discovered field.
    # setdefault semantics: operator-set env vars always win.
    platform_upper = platform.upper()
    for sdk_field in sdk_fields:
        value = _value_for_dlt_field(platform, sdk_field, credentials)
        if value is None or value == "":
            continue
        env_var = f"DESTINATION__{platform_upper}__CREDENTIALS__{sdk_field.upper()}"
        if not os.environ.get(env_var):
            os.environ[env_var] = str(value)
