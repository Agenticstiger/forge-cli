# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Seed a build's ``properties.stream`` from a registry subject.

The subject's latest (or Nth) schema becomes ``stream.schema``, its type the
``format`` (``AVRO`` -> ``avro-confluent``, ``JSON`` -> ``json``), and the registry the
``stream.registry`` reference: unpinned (``latest``) when read at latest, so
``fluid plan`` checks each new producer version, and pinned to N when read at N. The topic is the subject less a TopicNameStrategy
``-value`` suffix. The market connectors (``cli/market_catalogs/confluent_schema_reg.py``
and ``glue_schema_reg.py``) call :func:`stream_from_subject`.

A Confluent registry is read with no credential, through the same host policy as
``fluid plan`` (:mod:`.policy`); a Glue registry with the caller's IAM identity.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Union

from .reference import RegistryRef


def _topic(subject: str) -> str:
    return subject[: -len("-value")] if subject.endswith("-value") else subject


def stream_from_subject(
    ref: RegistryRef,
    *,
    environ: Optional[Mapping[str, str]] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """A ``properties.stream`` block for ``ref``'s subject, read from the registry.

    Raises:
        RegistryError: the subject could not be read, or the host is refused.
        UnsupportedSchema: the schema has a construct no column type carries.
    """
    from .clients import RegistryError, parse_schema
    from .kinds import get_kind
    from .policy import subject_findings
    from .reference import Subject
    from .schemas import columns_from_registry

    if ref.subject is None:
        raise RegistryError("name the subject to import")
    topic = _topic(ref.subject)
    probe = Subject(ref, "consumed", topic, "avro-confluent")
    refused = [m for s, m in subject_findings(probe, stage="plan", environ=environ) if s == "error"]
    if refused:
        raise RegistryError("; ".join(refused))
    kind = get_kind(ref.kind)
    reader = client if client is not None else kind.client(ref)
    try:
        sv = kind.read(reader, ref)
    finally:
        # Only a client built here is closed (a Glue client has nothing to close).
        close = getattr(reader, "close", None)
        if client is None and callable(close):
            close()
    columns = columns_from_registry(parse_schema(sv.schema), sv.data_format)
    registry: Dict[str, Any] = {"kind": ref.kind, **kind.registry_block(ref)}
    if ref.subject != f"{topic}-value":
        registry["subject"] = ref.subject
    if ref.version != "latest":
        registry["version"] = sv.version  # the columns came from this version: pin it
    return {
        "kind": "kafka",
        "topic": topic,
        "format": "json" if sv.data_format == "JSON" else "avro-confluent",
        "schema": columns,
        "registry": registry,
    }


def ref_for(
    kind: str,
    subject: str,
    *,
    url: Optional[str] = None,
    registry_name: Optional[str] = None,
    version: Union[str, int] = "latest",
) -> RegistryRef:
    """A :class:`RegistryRef` built from command-line style arguments."""
    from .reference import from_registry

    spec: Dict[str, Any] = {"kind": kind, "subject": subject, "version": version}
    if url:
        spec["url"] = url
    if registry_name:
        spec["registryName"] = registry_name
    return from_registry(spec, topic=_topic(subject), origin="import", where="import")
