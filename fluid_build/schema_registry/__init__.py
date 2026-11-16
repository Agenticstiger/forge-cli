# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema registries, one typed reference for every spelling a contract can use.

Who owns a subject decides what forge does with it (founder decision D7, 2026-11-16):

* a topic a build reads belongs to its producer, so ``fluid plan`` reads the subject and
  checks the declared schema against it, and never writes it (:mod:`.verify`);
* a topic an expose produces is registered through OpenTofu: ``aws_glue_schema`` on the
  Glue Schema Registry (``iac/providers/aws_schema_registry.py``), ``confluent_schema``
  and ``confluent_subject_config`` on a Confluent registry (``iac/providers/confluent.py``).

The three spellings (``streamSource.registry``, the kafka-connect ``schema_registry``
block and managed Flink's ``schemaRegistryUrl``) all resolve to
:class:`~.reference.RegistryRef` (:mod:`.reference`).

Nothing here is imported on the ``fluid --help`` path: names resolve on first access
(PEP 562), and the HTTP and AWS clients import ``httpx`` and ``boto3`` inside the call.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "RegistryRef": "reference",
    "Subject": "reference",
    "RegistryShapeError": "reference",
    "consumed_subjects": "reference",
    "produced_subjects": "reference",
    "connector_registries": "reference",
    "registry_refs": "reference",
    "avro_schema": "schemas",
    "json_schema": "schemas",
    "schema_text": "schemas",
    "columns_from_registry": "schemas",
    "reader_changes": "compare",
    "verify_consumed": "verify",
    "attach_registry_checks": "verify",
    "validation_findings": "policy",
    "stream_from_subject": "importer",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
