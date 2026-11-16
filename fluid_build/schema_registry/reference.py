# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One typed reference for every way a contract names a schema registry.

A contract names a registry in four places:

``builds[].properties.stream.registry``
    0.7.7 ``$defs/schemaRegistry``. The topic is the producer's, so its subject is
    a **consumed** subject: verified at plan, never written.
``builds[].properties.parameters.flink.source.schemaRegistryUrl``
    Managed Flink's older spelling. It reads as ``{kind: confluent, url}``, and an
    ``avro-confluent`` stream makes the topic's ``<topic>-value`` subject consumed.
``builds[].properties.kafka-connect.schema_registry``
    The acquisition block ``{url, auth}``. It reads as ``{kind: confluent, url,
    auth}``. The Connect converter writes those subjects at run time, so forge
    neither verifies nor registers them (role ``connector``).
``exposes[].binding.properties.registry``
    A ``$defs/schemaRegistry`` object on an expose bound to Kafka (``platform:
    kafka``, or ``platform: confluent`` with ``format: kafka_topic``). The topic is
    the product's, so its subject is **produced**: registered through OpenTofu.
    ``binding.properties`` is free-form in the schema, so :func:`shape_errors`
    checks the object against the same rules ``$defs/schemaRegistry`` states.

Resolution reads; it never changes what the kafka-connect runner and the Flink
compiler read from those blocks (``tests/schema_registry/test_reference.py`` pins it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

KINDS = ("glue", "confluent")

#: Compatibility modes each registry accepts (the schema's lists).
GLUE_MODES = (
    "NONE",
    "DISABLED",
    "BACKWARD",
    "BACKWARD_ALL",
    "FORWARD",
    "FORWARD_ALL",
    "FULL",
    "FULL_ALL",
)
CONFLUENT_MODES = (
    "NONE",
    "BACKWARD",
    "BACKWARD_TRANSITIVE",
    "FORWARD",
    "FORWARD_TRANSITIVE",
    "FULL",
    "FULL_TRANSITIVE",
)
#: A produced subject's mode when the contract names none: backward and transitive.
DEFAULT_COMPATIBILITY = {"glue": "BACKWARD_ALL", "confluent": "BACKWARD_TRANSITIVE"}
#: Glue's name for the registry it uses when none is named.
GLUE_DEFAULT_REGISTRY = "default-registry"

_URL = re.compile(r"^https?://[^/?#@\s]+(?:/[^?#\s]*)?$")
_GLUE_NAME = re.compile(r"^[A-Za-z0-9_$#.-]{1,255}$")
_SUBJECT = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
_ALLOWED = frozenset({"kind", "url", "registryName", "subject", "version", "compatibility", "auth"})

Version = Union[str, int]


class RegistryShapeError(ValueError):
    """A registry object the schema's ``$defs/schemaRegistry`` rules refuse."""


@dataclass(frozen=True)
class RegistryRef:
    """A schema registry and the subject a contract points at in it.

    ``subject`` is ``None`` only for a kafka-connect block, which names no subject.
    ``secret_ref`` is the ``auth.secretRef`` text as written; it is a reference,
    never a value.
    """

    kind: str
    origin: str
    where: str
    url: Optional[str] = None
    registry_name: Optional[str] = None
    subject: Optional[str] = None
    version: Version = "latest"
    compatibility: Optional[str] = None
    secret_ref: Optional[str] = None

    @property
    def glue_registry(self) -> str:
        """The Glue registry's name, ``default-registry`` when none is named."""
        return self.registry_name or GLUE_DEFAULT_REGISTRY

    @property
    def effective_compatibility(self) -> str:
        """The mode a produced subject is registered with: the contract's, else the default."""
        return self.compatibility or DEFAULT_COMPATIBILITY[self.kind]

    @property
    def label(self) -> str:
        """How a message names the subject: ``glue:orders/orders-value@latest``."""
        where = self.glue_registry if self.kind == "glue" else self.url
        return f"{self.kind}:{where}/{self.subject}@{self.version}"


@dataclass(frozen=True)
class Subject:
    """A subject with the topic it holds and the columns the contract declares for it.

    ``role`` is ``consumed`` (a build reads the topic) or ``produced`` (an expose
    writes it). ``format`` is the value encoding (``json``, ``avro-confluent``,
    ``avro``, ``json-confluent``...), ``columns`` the value's fields as the contract
    declares them.
    """

    ref: RegistryRef
    role: str
    topic: str
    format: str
    columns: Tuple[Mapping[str, Any], ...] = field(default=())
    owner: str = ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def shape_errors(spec: Any, where: str) -> List[str]:
    """What ``$defs/schemaRegistry`` would refuse in ``spec``, as messages.

    For ``binding.properties.registry``, which the schema leaves free-form. Messages
    name fields, never values: a credential pasted into ``url`` is not quoted back.
    """
    if not isinstance(spec, Mapping):
        return [f"{where}: expected an object"]
    errors: List[str] = []
    unknown = sorted(str(k) for k in set(spec) - _ALLOWED)
    if unknown:
        errors.append(f"{where}: unsupported field(s) {unknown}")
    kind = spec.get("kind")
    if kind not in KINDS:
        errors.append(f"{where}.kind: expected glue or confluent")
        return errors
    url = spec.get("url")
    if kind == "confluent":
        if not isinstance(url, str) or not _URL.fullmatch(url):
            errors.append(
                f"{where}.url: a confluent registry needs its http(s) REST endpoint, "
                "with no credentials, query or fragment"
            )
        if "registryName" in spec:
            errors.append(f"{where}.registryName is for kind: glue; a Confluent registry has a url")
    else:
        if "url" in spec:
            errors.append(
                f"{where}.url is for kind: confluent; name a Glue registry by registryName"
            )
        if "auth" in spec:
            errors.append(
                f"{where}.auth is for kind: confluent; a Glue registry authenticates with IAM"
            )
        name = spec.get("registryName")
        if name is not None and not (isinstance(name, str) and _GLUE_NAME.fullmatch(name)):
            errors.append(f"{where}.registryName: letters, digits and _ $ # . - (1-255)")
    subject = spec.get("subject")
    if subject is not None:
        pattern = _GLUE_NAME if kind == "glue" else _SUBJECT
        if not (isinstance(subject, str) and pattern.fullmatch(subject)):
            errors.append(f"{where}.subject: not a {kind} subject name")
    version = spec.get("version", "latest")
    if version != "latest" and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        errors.append(f"{where}.version: expected latest or a version from 1")
    mode = spec.get("compatibility")
    modes = GLUE_MODES if kind == "glue" else CONFLUENT_MODES
    if mode is not None and mode not in modes:
        errors.append(f"{where}.compatibility: {kind} accepts {', '.join(modes)}")
    auth = spec.get("auth")
    if auth is not None:
        raw = _mapping(auth).get("secretRef")
        if not isinstance(raw, str) or set(_mapping(auth)) - {"secretRef"}:
            errors.append(f"{where}.auth: expected {{secretRef: <reference>}}")
    return errors


def from_registry(spec: Mapping[str, Any], *, topic: str, origin: str, where: str) -> RegistryRef:
    """A ``$defs/schemaRegistry`` object as a reference. Raises :class:`RegistryShapeError`."""
    errors = shape_errors(spec, where)
    if errors:
        raise RegistryShapeError("; ".join(errors))
    auth = _mapping(spec.get("auth"))
    return RegistryRef(
        kind=spec["kind"],
        origin=origin,
        where=where,
        url=spec.get("url"),
        registry_name=spec.get("registryName"),
        subject=spec.get("subject") or f"{topic}-value",
        version=spec.get("version", "latest"),
        compatibility=spec.get("compatibility"),
        secret_ref=auth.get("secretRef") if isinstance(auth.get("secretRef"), str) else None,
    )


def from_kafka_connect(block: Mapping[str, Any], *, where: str) -> Optional[RegistryRef]:
    """The kafka-connect ``schema_registry`` block as ``{kind: confluent, url, auth}``.

    ``None`` without a ``url``: the runner then leaves Connect's JSON converter, and
    reads no registry either (``kafka_connect/runner.py``).
    """
    url = block.get("url")
    if not url:
        return None
    auth = _mapping(block.get("auth"))
    raw = auth.get("secretRef")
    return RegistryRef(
        kind="confluent",
        origin="kafka-connect.schema_registry",
        where=where,
        url=str(url),
        secret_ref=raw if isinstance(raw, str) else None,
    )


def from_flink_url(url: str, *, topic: str, where: str) -> RegistryRef:
    """Managed Flink's ``schemaRegistryUrl`` as ``{kind: confluent, url}`` on ``<topic>-value``."""
    return RegistryRef(
        kind="confluent",
        origin="flink.schemaRegistryUrl",
        where=where,
        url=str(url),
        subject=f"{topic}-value",
    )


def _builds(contract: Mapping[str, Any]) -> Iterator[Tuple[int, Mapping[str, Any]]]:
    for index, build in enumerate(contract.get("builds") or []):
        if isinstance(build, Mapping):
            yield index, build


def _columns(value: Any) -> Tuple[Mapping[str, Any], ...]:
    return (
        tuple(c for c in value or [] if isinstance(c, Mapping)) if isinstance(value, list) else ()
    )


def consumed_subjects(contract: Mapping[str, Any]) -> List[Subject]:
    """Every subject a build reads, in contract order.

    A stream with ``registry`` (any engine), and a managed Flink build whose
    ``avro-confluent`` stream reads through ``schemaRegistryUrl``. A malformed
    registry raises :class:`RegistryShapeError`; the schema refuses it first on a
    validated contract.
    """
    found: List[Subject] = []
    for index, build in _builds(contract):
        props = _mapping(build.get("properties"))
        stream = _mapping(props.get("stream"))
        topic = stream.get("topic")
        if not stream or not isinstance(topic, str):
            continue
        at = f"builds[{index}].properties.stream"
        owner = str(build.get("id") or index)
        fmt = str(stream.get("format") or "")
        columns = _columns(stream.get("schema"))
        if "registry" in stream:
            ref = from_registry(
                stream["registry"], topic=topic, origin="stream.registry", where=f"{at}.registry"
            )
            found.append(Subject(ref, "consumed", topic, fmt, columns, owner))
            continue
        flink = _mapping(_mapping(props.get("parameters")).get("flink"))
        url = _mapping(flink.get("source")).get("schemaRegistryUrl")
        if url and fmt == "avro-confluent":
            where = f"builds[{index}].properties.parameters.flink.source.schemaRegistryUrl"
            ref = from_flink_url(url, topic=topic, where=where)
            found.append(Subject(ref, "consumed", topic, fmt, columns, owner))
    return found


#: The spellings ``fluid validate`` and ``fluid plan`` check. Managed Flink's
#: ``schemaRegistryUrl`` resolves too, but keeps the checks its own compiler runs
#: (``streaming/compiler.py``, ``streaming/policy.py``): checking it here would put
#: every existing Flink plan online.
CHECKED_ORIGINS = frozenset({"stream.registry", "binding.properties.registry"})


def checked_subjects(contract: Mapping[str, Any]) -> List[Subject]:
    """The consumed subjects the registry checks cover: ``stream.registry`` ones."""
    return [s for s in consumed_subjects(contract) if s.ref.origin in CHECKED_ORIGINS]


def kafka_topic_binding(binding: Mapping[str, Any]) -> bool:
    """Whether an expose binding is a Kafka topic the product writes."""
    platform = str(binding.get("platform") or "").lower()
    fmt = str(binding.get("format") or "").lower()
    return platform == "kafka" or (platform == "confluent" and fmt == "kafka_topic")


def produced_subjects(contract: Mapping[str, Any]) -> List[Subject]:
    """Every subject an expose produces: a Kafka-bound expose with ``properties.registry``.

    The topic is ``location.topic``, else the ``exposeId``; the columns are the
    expose's ``contract.schema``. A malformed registry raises
    :class:`RegistryShapeError` (:func:`shape_errors` has the messages).
    """
    found: List[Subject] = []
    for index, exposure in enumerate(contract.get("exposes") or []):
        if not isinstance(exposure, Mapping):
            continue
        binding = _mapping(exposure.get("binding"))
        spec = _mapping(binding.get("properties")).get("registry")
        if spec is None or not kafka_topic_binding(binding):
            continue
        topic = _mapping(binding.get("location")).get("topic") or exposure.get("exposeId")
        if not isinstance(topic, str) or not topic:
            continue
        where = f"exposes[{index}].binding.properties.registry"
        ref = from_registry(spec, topic=topic, origin="binding.properties.registry", where=where)
        serialization = _mapping(binding.get("serialization"))
        fmt = str(serialization.get("value") or "")
        columns = _columns(_mapping(exposure.get("contract")).get("schema"))
        owner = str(exposure.get("exposeId") or index)
        found.append(Subject(ref, "produced", topic, fmt, columns, owner))
    return found


def connector_registries(contract: Mapping[str, Any]) -> List[RegistryRef]:
    """The kafka-connect ``schema_registry`` blocks that name a url."""
    found: List[RegistryRef] = []
    for index, build in _builds(contract):
        kc = _mapping(_mapping(build.get("properties")).get("kafka-connect"))
        block = kc.get("schema_registry")
        if isinstance(block, Mapping):
            where = f"builds[{index}].properties.kafka-connect.schema_registry"
            ref = from_kafka_connect(block, where=where)
            if ref is not None:
                found.append(ref)
    return found


def registry_refs(contract: Mapping[str, Any]) -> List[RegistryRef]:
    """Every registry the contract names, each spelling resolved, in a stable order."""
    refs: List[RegistryRef] = [s.ref for s in consumed_subjects(contract)]
    refs.extend(s.ref for s in produced_subjects(contract))
    refs.extend(connector_registries(contract))
    return refs


def malformed_produced(contract: Mapping[str, Any]) -> Sequence[str]:
    """Shape messages for every ``binding.properties.registry`` on a Kafka-bound expose.

    The 0.7.7 schema types the field (``$defs/schemaRegistry``), so a validated
    contract has none; this guards a contract that reached here unvalidated.
    """
    errors: List[str] = []
    for index, exposure in enumerate(contract.get("exposes") or []):
        binding = _mapping(_mapping(exposure).get("binding"))
        spec = _mapping(binding.get("properties")).get("registry")
        if spec is not None and kafka_topic_binding(binding):
            errors.extend(shape_errors(spec, f"exposes[{index}].binding.properties.registry"))
    return errors


def stray_registries(contract: Mapping[str, Any]) -> List[str]:
    """Warnings for a ``binding.properties.registry`` on a binding that is no Kafka topic.

    Nothing reads it there, so nothing is registered: said, not refused.
    """
    warnings: List[str] = []
    for index, exposure in enumerate(contract.get("exposes") or []):
        binding = _mapping(_mapping(exposure).get("binding"))
        if "registry" in _mapping(binding.get("properties")) and not kafka_topic_binding(binding):
            warnings.append(
                f"exposes[{index}].binding.properties.registry is ignored: a produced subject "
                "is registered for a Kafka topic expose (platform: kafka, or platform: "
                "confluent with format: kafka_topic)"
            )
    return warnings
