# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tables shared by Bitol ODPS mappers (status mapping, provider → port type)."""

from __future__ import annotations

from typing import Dict

# The FLUID-side vocabulary is ``lifecycle.state``
# (preview/active/deprecated/retired). ``preview`` is the state Bitol calls
# ``draft``; the legacy hand-written spellings below are still accepted.
_FLUID_TO_BITOL_STATUS: Dict[str, str] = {
    "preview": "draft",
    "draft": "draft",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
    "development": "draft",
    "proposed": "proposed",
}

_BITOL_TO_FLUID_STATUS: Dict[str, str] = {
    "proposed": "draft",
    "draft": "draft",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
}


def fluid_to_bitol_status(status: str) -> str:
    return _FLUID_TO_BITOL_STATUS.get(str(status).lower(), "draft")


def bitol_to_fluid_status(status: str) -> str:
    return _BITOL_TO_FLUID_STATUS.get(str(status).lower(), "draft")


# FLUID provider/platform → ODPS output port type. Bitol ODPS doesn't constrain
# this enum strictly, so we keep the same vocabulary the existing exporter used.
_PROVIDER_TO_PORT_TYPE: Dict[str, str] = {
    "gcp": "bigquery",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "aws": "s3",
    "s3": "s3",
    "azure": "azure",
    "databricks": "databricks",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "kafka": "kafka",
    "local": "local",
}


def provider_to_port_type(provider: str) -> str:
    return _PROVIDER_TO_PORT_TYPE.get(provider.lower(), "custom")
