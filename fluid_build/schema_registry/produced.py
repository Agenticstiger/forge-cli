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

from typing import Any, Mapping, Sequence, Tuple

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


def classification_metadata(
    contract: Mapping[str, Any], expose: Mapping[str, Any], namespace: str = ""
) -> dict:
    """Confluent ``metadata`` for a produced subject: classification and PII field tags.

    ``properties`` carries the expose's classification (else the contract's); ``tags``
    maps each personal-data column, by its field's full name (``<namespace>.<record>.
    <field>``, the names :func:`schema_definition` registers), to ``PII`` (Confluent data
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
    record = ".".join(
        [*(avro_name(part) for part in namespace.split(".") if part), avro_name(str(topic))]
    )
    schema = ((expose.get("contract") or {}).get("schema")) or []
    pii = sorted(
        str(c.get("name"))
        for c in schema
        if isinstance(c, Mapping) and c.get("name") and _is_pii(c)
    )
    out: dict = {}
    if classification:
        out["properties"] = {"fluid_classification": str(classification)}
    # Field tags name Avro record fields; a JSON Schema subject's paths are not those
    # names, so it gets the classification only.
    avro = str(((binding.get("serialization") or {}).get("value")) or "avro") in (
        "avro",
        "avro-confluent",
    )
    if pii and avro:
        out["tags"] = [{"key": f"{record}.{name}", "value": ["PII"]} for name in pii]
    return out


#: The cloud whose OpenTofu plugin registers a subject of each registry kind.
REGISTERING_CLOUD = {"glue": "aws", "confluent": "confluent"}


def cloud_bound(contract: Mapping[str, Any], kind: str) -> bool:
    """Whether an expose binds the cloud that registers a ``kind`` subject.

    ``fluid generate iac`` and ``fluid apply`` pick the OpenTofu plugin from the
    exposes' clouds, and ``platform: kafka`` is none: a Glue subject is registered by
    the aws plugin, so the contract needs an AWS-bound expose; a Confluent subject by
    the confluent plugin, which a ``platform: confluent`` expose selects.
    """
    from fluid_build.iac.provider_match import is_cloud

    cloud = REGISTERING_CLOUD[kind]
    return any(
        is_cloud((e.get("binding") or {}), cloud)
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping)
    )


def produced_findings(contract: Mapping[str, Any], subject: Subject) -> Tuple[list, list]:
    """``(errors, warnings)`` that ``fluid validate`` and ``fluid plan`` report early.

    The schema text OpenTofu would register, derived now, so a column type no registry
    format carries fails here and not at apply; and a subject no bound cloud's plugin
    would register (an error, as the emitter refuses it).
    """
    errors: list = []
    warnings: list = []
    try:
        from fluid_build.iac.naming import safe_ident

        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        schema_definition(subject, f"fluid.{cid}")
    except ProducedSubjectError as exc:
        errors.append(str(exc))
    if not cloud_bound(contract, subject.ref.kind):
        # An error, like the emitter's refusal: no OpenTofu run of this contract would
        # register it, and a produced topic with no subject is a silent gap.
        cloud = REGISTERING_CLOUD[subject.ref.kind]
        fix = (
            "bind an AWS expose (the Glue registry's product)"
            if cloud == "aws"
            else "bind the topic with platform: confluent and format: kafka_topic"
        )
        errors.append(
            f"{subject.ref.where}: no expose binds {cloud}, so no OpenTofu module registers "
            f"subject {subject.ref.subject!r}; {fix}"
        )
    return errors, warnings


def produced_set_errors(contract: Mapping[str, Any], produced: Sequence[Subject]) -> list:
    """What the emitters refuse across subjects, said at validate and plan instead.

    An owned Glue registry needs a name (``schema-registry-unnamed``), and one subject
    produced twice in one registry is refused (``schema-registry-duplicate-subject``).
    """
    errors: list = []
    exposes = {
        str(e.get("exposeId") or i): e
        for i, e in enumerate(contract.get("exposes") or [])
        if isinstance(e, Mapping)
    }
    seen: dict = {}
    for subject in produced:
        ref = subject.ref
        if (
            ref.kind == "glue"
            and not ref.registry_name
            and owns_registry(contract, exposes.get(subject.owner, {}))
        ):
            errors.append(
                f"{ref.where}: packaging.containers.schemaRegistry is isolated, so this "
                "product creates its registry, and the registry has no registryName"
            )
        where = ref.glue_registry if ref.kind == "glue" else ref.url
        key = (ref.kind, where, ref.subject)
        if key in seen:
            errors.append(
                f"{ref.where}: subject {ref.subject!r} is also produced by {seen[key]}; "
                "one subject has one producer"
            )
        seen.setdefault(key, ref.where)
    return errors
