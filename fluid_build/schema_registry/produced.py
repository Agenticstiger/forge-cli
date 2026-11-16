# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What OpenTofu registers for a produced subject: its format and schema text.

Shared by the Glue emitter (``iac/providers/aws_schema_registry.py``) and the
Confluent one (``iac/providers/confluent.py``) so both register the same bytes for
the same columns. The text is :func:`.schemas.schema_text` of the derived schema:
sorted keys and no whitespace, so the emitted module, and the plan digest over it,
change only when the columns do.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from .reference import Subject

#: The value formats a produced subject may declare, by registry schema type.
_FORMATS = {
    "avro": "AVRO",
    "avro-confluent": "AVRO",
    "json": "JSON",
    "json-confluent": "JSON",
}


class ProducedSubjectError(ValueError):
    """A produced subject OpenTofu cannot register as declared."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def data_format(subject: Subject) -> str:
    """``AVRO`` or ``JSON`` from the binding's ``serialization.value`` (Avro when unset)."""
    if not subject.format:
        return "AVRO"
    fmt = _FORMATS.get(subject.format)
    if fmt is None:
        raise ProducedSubjectError(
            "schema-registry-format",
            f"{subject.ref.where}: forge registers Avro and JSON Schema subjects; "
            f"serialization.value {subject.format!r} is not one",
        )
    return fmt


def schema_definition(subject: Subject, namespace: str) -> Tuple[str, str]:
    """``(data format, schema text)`` for ``subject``: the expose's columns, derived."""
    from .schemas import avro_name, derive, schema_text

    if not subject.columns:
        raise ProducedSubjectError(
            "schema-registry-no-columns",
            f"{subject.ref.where}: a produced subject is registered from the expose's "
            "contract.schema, and it declares no columns",
        )
    fmt = data_format(subject)
    try:
        document = derive(
            subject.columns,
            fmt,
            name=avro_name(subject.topic),
            namespace=".".join(avro_name(part) for part in namespace.split(".")),
        )
    except ValueError as exc:
        raise ProducedSubjectError(
            "schema-registry-columns", f"{subject.ref.where}: {exc}"
        ) from exc
    return fmt, schema_text(document)


def owns_registry(contract: Mapping[str, Any], expose: Mapping[str, Any]) -> bool:
    """Whether this product's state owns the Glue registry: ``schemaRegistry: isolated``.

    Explicit only, like the LF-tag vocabulary (``aws.py::_owns_lf_tag_vocabulary``):
    the blanket ``mode: isolated`` most products declare must not hand each one the
    domain's registry. The expose's ``binding.packaging.containers`` wins over the
    contract's.
    """

    def declared(node: Any) -> Any:
        packaging = node.get("packaging") if isinstance(node, Mapping) else None
        containers = packaging.get("containers") if isinstance(packaging, Mapping) else None
        return containers.get("schemaRegistry") if isinstance(containers, Mapping) else None

    binding = expose.get("binding") if isinstance(expose, Mapping) else None
    choice = declared(binding)
    if choice is None:
        choice = declared(contract)
    return choice == "isolated"


def classification_metadata(contract: Mapping[str, Any], expose: Mapping[str, Any]) -> dict:
    """Confluent ``metadata`` for a produced subject: classification and PII field tags.

    ``properties`` carries the expose's classification (else the contract's); ``tags``
    maps each personal-data column, as ``<record>.<field>``, to ``PII`` (Confluent data
    contracts' field-tag shape). Empty parts are left out.
    """
    from fluid_build.api.changelog_rules import _is_pii

    from .schemas import avro_name

    policy = expose.get("policy") if isinstance(expose.get("policy"), Mapping) else {}
    metadata_block = (
        contract.get("metadata") if isinstance(contract.get("metadata"), Mapping) else {}
    )
    classification = policy.get("classification") or metadata_block.get("classification")
    binding = expose.get("binding") or {}
    topic = (binding.get("location") or {}).get("topic") or expose.get("exposeId") or ""
    record = avro_name(str(topic))
    schema = ((expose.get("contract") or {}).get("schema")) or []
    pii = sorted(
        str(c.get("name"))
        for c in schema
        if isinstance(c, Mapping) and c.get("name") and _is_pii(c)
    )
    out: dict = {}
    if classification:
        out["properties"] = {"fluid_classification": str(classification)}
    if pii:
        out["tags"] = [{"key": f"{record}.{name}", "value": ["PII"]} for name in pii]
    return out
