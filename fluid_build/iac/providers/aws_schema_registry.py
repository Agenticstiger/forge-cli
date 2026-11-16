# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Glue Schema Registry: the subjects an AWS product's Kafka exposes produce.

A topic an expose writes is the product's, so its schema is registered through
OpenTofu (founder decision D7, 2026-11-16). For each Kafka-bound expose whose
``binding.properties.registry`` is ``kind: glue``
(``schema_registry/reference.py::produced_subjects``):

* ``aws_glue_registry`` when the product owns the registry: an explicit
  ``packaging.containers.schemaRegistry: isolated`` on the expose's binding or the
  contract (``schema_registry/produced.py::owns_registry``). Otherwise the registry
  is the domain's (AWS streaming plan §3b, layer L1) and is looked up with
  ``data.aws_glue_registry``; with no ``registryName`` it is Glue's default registry,
  and ``registry_arn`` is left out (``glue:CreateSchema`` with no registry uses
  ``default-registry``).
* ``aws_glue_schema``: the subject (``registry.subject``, else ``<topic>-value``),
  ``data_format`` from ``binding.serialization.value`` (Avro unless JSON),
  ``compatibility`` from the contract (default ``BACKWARD_ALL``), and the schema
  derived from the expose's ``contract.schema``, as canonical text so the module and
  its plan digest are stable. A changed definition is a new schema version, and Glue
  refuses one its mode does not allow, at apply.

A ``kind: confluent`` registry on a ``platform: kafka`` expose has no route through
this plugin (the confluentinc provider registers it) and is refused, never skipped.

Called from :class:`~.aws.AwsIacPlugin` at one site each in ``emit`` and ``emit_data``.
Borrowed: the resource and data source arguments are hashicorp/aws's (MPL-2.0,
``website/docs/r/glue_schema.html.markdown``, ``r/glue_registry``, ``d/glue_registry``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Mapping, Tuple

from ...schema_registry.produced import ProducedSubjectError, owns_registry, schema_definition
from ...schema_registry.reference import Subject, produced_subjects
from ..base import UnsupportedBindingError
from ..naming import safe_ident, tofu_ref


def _exposes_by_id(contract: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for index, exposure in enumerate(contract.get("exposes") or []):
        if isinstance(exposure, Mapping):
            out[str(exposure.get("exposeId") or index)] = exposure
    return out


def _glue_subjects(contract: Mapping[str, Any]) -> Iterator[Tuple[Subject, Mapping[str, Any]]]:
    """``(subject, expose)`` for every produced Glue subject; a stray Confluent one refused."""
    exposes = _exposes_by_id(contract)
    for subject in produced_subjects(contract):
        expose = exposes.get(subject.owner, {})
        platform = str((expose.get("binding") or {}).get("platform") or "").lower()
        if subject.ref.kind == "glue":
            yield subject, expose
        elif platform == "kafka":
            raise UnsupportedBindingError(
                "schema-registry-route",
                f"{subject.ref.where}: a Confluent subject is registered by the confluent "
                "OpenTofu plugin, and this contract is emitted by the aws one",
                (
                    "Bind the expose with platform: confluent and format: kafka_topic, or",
                    "use kind: glue for a topic on MSK.",
                ),
            )


def _registry_key(cid: str, name: str) -> str:
    return safe_ident(f"{cid}_{name}")


def _owned(contract: Mapping[str, Any], subject: Subject, expose: Mapping[str, Any]) -> bool:
    owned = owns_registry(contract, expose)
    if owned and not subject.ref.registry_name:
        raise UnsupportedBindingError(
            "schema-registry-unnamed",
            f"{subject.ref.where}: packaging.containers.schemaRegistry is isolated, so this "
            "product creates its registry, and the registry has no registryName",
            ("Name the registry with registryName.",),
        )
    return owned


def emit_schema_registry(
    resources: Dict[str, Any],
    contract: Mapping[str, Any],
    cid: str,
    tags: Mapping[str, str],
) -> None:
    """Add the registries this product owns and every produced subject's schema."""
    for subject, expose in _glue_subjects(contract):
        ref = subject.ref
        try:
            data_format, definition = schema_definition(subject, f"fluid.{cid}")
        except ProducedSubjectError as exc:
            raise UnsupportedBindingError(exc.kind, str(exc)) from exc
        schema: Dict[str, Any] = {
            "schema_name": ref.subject,
            "data_format": data_format,
            "compatibility": ref.effective_compatibility,
            "schema_definition": definition,
            "description": f"Value schema of Kafka topic {subject.topic}, from FLUID {cid}",
            "tags": dict(tags),
        }
        if ref.registry_name:
            key = _registry_key(cid, ref.registry_name)
            if _owned(contract, subject, expose):
                resources.setdefault("aws_glue_registry", {}).setdefault(
                    key,
                    {
                        "registry_name": ref.registry_name,
                        "description": f"Schema registry owned by FLUID {cid}",
                        "tags": dict(tags),
                    },
                )
                schema["registry_arn"] = tofu_ref(f"aws_glue_registry.{key}.arn")
            else:
                schema["registry_arn"] = tofu_ref(f"data.aws_glue_registry.{key}.arn")
        else:
            _owned(contract, subject, expose)
        name = safe_ident(f"{cid}_{ref.glue_registry}_{ref.subject}")
        table = resources.setdefault("aws_glue_schema", {})
        if name in table:
            raise UnsupportedBindingError(
                "schema-registry-duplicate-subject",
                f"{ref.where}: subject {ref.subject!r} in {ref.glue_registry} is produced by "
                "two exposes, or by two that name it differently only in punctuation",
            )
        table[name] = schema


def emit_schema_registry_data(data: Dict[str, Any], contract: Mapping[str, Any], cid: str) -> None:
    """Look up each named Glue registry this product references but does not own."""
    for subject, expose in _glue_subjects(contract):
        name = subject.ref.registry_name
        if name and not owns_registry(contract, expose):
            data.setdefault("aws_glue_registry", {})[_registry_key(cid, name)] = {"name": name}
