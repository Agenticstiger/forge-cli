# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid plan``'s registry check: every consumed subject read, its schema compared.

``fluid validate`` stays offline and checks the registry's shape and egress only
(:func:`.policy.validation_findings`). ``fluid plan`` is online, and for each subject a
build reads through ``stream.registry`` (:func:`.reference.checked_subjects`; managed
Flink's ``schemaRegistryUrl`` keeps its own compiler's checks):

Confluent
    ``GET /subjects/{subject}/versions/{version}`` pins the version, then
    ``POST /compatibility/subjects/{subject}/versions/{that version}?verbose=true``
    with the Avro or JSON Schema forge derives from ``stream.schema``, named after
    the producer's records so Avro resolution matches them. An incompatible answer
    is a plan error that quotes the registry's messages. The registry judges under
    the subject's own mode (a ``NONE`` subject accepts anything), so the comparison
    below runs as well.
Glue Schema Registry
    There is no dry-run API, so ``glue:GetSchemaVersion`` (latest or N), then the
    comparison below.
Both
    :func:`.compare.reader_changes`: the subject's columns against the declared ones,
    with ``fluid diff``'s stream rules. A breaking change is a plan error. Each
    subject version checked is reported and recorded in ``plan.json`` under
    ``schemaRegistry``, covered by ``planDigest``: an approved plan names the
    versions it was checked against.

A subject whose registry needs a credential is not read: plan never fetches a
contract secret (``secrets/policy.py``), so it is reported as not verified.
Produced subjects are registered by OpenTofu and only get the egress check here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .reference import RegistryShapeError, Subject

LOG = logging.getLogger(__name__)

#: The ``plan.json`` key the checks are recorded under.
PLAN_KEY = "schemaRegistry"


def _writer(sv: Any) -> Tuple[List[Dict[str, Any]], str, Dict[str, str]]:
    from .clients import parse_schema
    from .schemas import avro_columns, json_columns

    schema = parse_schema(sv.schema)
    if sv.data_format == "JSON":
        return json_columns(schema), "", {}
    return avro_columns(schema)


def _check(
    subject: Subject,
    *,
    confluent: Callable[[str], Any],
    glue: Callable[[], Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read and compare one subject. ``(record, error or None)``."""
    from .clients import RegistryError
    from .compare import describe, reader_changes
    from .schemas import UnsupportedSchema, derive, registry_format, schema_text

    ref = subject.ref
    expected = registry_format(subject.format)
    record: Dict[str, Any] = {
        "build": subject.owner,
        "topic": subject.topic,
        "kind": ref.kind,
        "registry": ref.url if ref.kind == "confluent" else ref.glue_registry,
        "subject": ref.subject,
        "requested": ref.version,
    }
    at = f"{ref.where} ({ref.label})"
    client = confluent(ref.url) if ref.kind == "confluent" else glue()
    try:
        if ref.kind == "confluent":
            sv = client.get_version(ref.subject, ref.version)
        else:
            sv = client.get_version(ref.glue_registry, ref.subject, ref.version)
        record["version"] = sv.version
        if sv.schema_id is not None:
            record["schemaId"] = sv.schema_id
        if sv.data_format != expected:
            return record, (
                f"{at}: version {sv.version} holds a {sv.data_format} schema, and the stream's "
                f"format {subject.format!r} reads {expected}"
            )
        writer, full, names = _writer(sv)
        if ref.kind == "confluent":
            name = full.rpartition(".")[2] or subject.topic
            reader = derive(
                subject.columns,
                expected,
                name=name,
                namespace=full.rpartition(".")[0] or None,
                names=names,
            )
            ok, messages = client.check_compatibility(
                ref.subject, sv.version, schema_text(reader), expected
            )
            try:
                record["mode"] = client.compatibility_level(ref.subject)
            except RegistryError:
                pass
            if not ok:
                said = "; ".join(messages) or "no reason given"
                return record, (
                    f"{at}: the registry says the declared stream.schema is not compatible "
                    f"with version {sv.version}: {said}"
                )
        breaking = reader_changes(
            subject.columns, writer, data_format=expected, topic=subject.topic
        )
    except RegistryError as exc:
        return record, f"{at}: the subject could not be read: {exc}"
    except (UnsupportedSchema, ValueError) as exc:
        return record, f"{at}: {exc}"
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    if breaking:
        return record, (
            f"{at}: stream.schema cannot read version {sv.version} of the subject: "
            f"{describe(breaking)}"
        )
    record["result"] = "compatible"
    return record, None


def _same_endpoint(a: str, b: str) -> bool:
    from urllib.parse import urlsplit

    pa, pb = urlsplit(a.strip()), urlsplit(b.strip())
    return (pa.scheme.lower(), (pa.hostname or "").lower(), pa.port, pa.path.rstrip("/")) == (
        pb.scheme.lower(),
        (pb.hostname or "").lower(),
        pb.port,
        pb.path.rstrip("/"),
    )


def _operator_endpoint(
    subject: Subject,
    environ: Optional[Mapping[str, str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """A produced Confluent subject lands in the operator's ``SCHEMA_REGISTRY_REST_ENDPOINT``.

    OpenTofu registers it in the provider-level registry, so a contract naming one
    registry and an environment naming another would register the schema somewhere
    the contract never said: refused. Unset is a warning: apply needs it.
    """
    import os

    from .policy import TOFU_REGISTRY_ENDPOINT_ENV

    env = os.environ if environ is None else environ
    endpoint = env.get(TOFU_REGISTRY_ENDPOINT_ENV)
    ref = subject.ref
    if not endpoint:
        warnings.append(
            f"{ref.where}: OpenTofu registers subject {ref.subject!r} in the registry "
            f"{TOFU_REGISTRY_ENDPOINT_ENV} names, and it is unset; fluid apply needs it set "
            f"to {ref.url} with SCHEMA_REGISTRY_ID, SCHEMA_REGISTRY_API_KEY and "
            "SCHEMA_REGISTRY_API_SECRET"
        )
    elif not _same_endpoint(endpoint, str(ref.url)):
        errors.append(
            f"{ref.where}: the contract registers subject {ref.subject!r} in {ref.url}, and "
            f"{TOFU_REGISTRY_ENDPOINT_ENV} points OpenTofu at another registry"
        )


def verify_consumed(
    contract: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    confluent: Optional[Callable[[str], Any]] = None,
    glue: Optional[Callable[[], Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Check every consumed subject and the egress of every produced one.

    Returns ``(records, errors, warnings)``. ``confluent`` and ``glue`` build the
    clients (tests pass fakes).
    """
    from .policy import subject_findings
    from .reference import checked_subjects, malformed_produced, produced_subjects

    errors: List[str] = list(malformed_produced(contract))
    warnings: List[str] = []
    records: List[Dict[str, Any]] = []
    try:
        consumed = checked_subjects(contract)
        produced = [] if errors else produced_subjects(contract)
    except RegistryShapeError as exc:
        return records, [*errors, str(exc)], warnings
    for subject in produced:
        errors.extend(m for s, m in subject_findings(subject, stage="plan", environ=environ))
        if subject.ref.kind == "confluent":
            _operator_endpoint(subject, environ, errors, warnings)
    if confluent is None:
        from .clients import ConfluentRegistry

        confluent = ConfluentRegistry
    if glue is None:
        from .clients import GlueRegistry

        glue = GlueRegistry
    for subject in consumed:
        refused = [m for s, m in subject_findings(subject, stage="plan", environ=environ)]
        if refused:
            errors.extend(refused)
            continue
        if subject.ref.secret_ref:
            warnings.append(
                f"{subject.ref.where} ({subject.ref.label}): not verified at plan. The registry "
                "needs a credential, and plan never fetches a contract secret; fluid apply "
                "is the stage that does"
            )
            records.append(
                {
                    "build": subject.owner,
                    "topic": subject.topic,
                    "kind": subject.ref.kind,
                    "registry": subject.ref.url,
                    "subject": subject.ref.subject,
                    "requested": subject.ref.version,
                    "result": "not-verified",
                }
            )
            continue
        record, error = _check(subject, confluent=confluent, glue=glue)
        if error:
            record["result"] = "incompatible"
            errors.append(error)
        records.append(record)
    return records, errors, warnings


def attach_registry_checks(
    plan: Dict[str, Any],
    contract: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    **clients: Any,
) -> Dict[str, Any]:
    """Run :func:`verify_consumed`; refuse the plan on an error, else record the checks.

    A contract that names no registry returns ``plan`` untouched: no new key, no
    digest change.

    Raises:
        CLIError: ``plan_schema_registry_failed`` with every error.
    """
    from .reference import connector_registries

    records, errors, warnings = verify_consumed(contract, environ=environ, **clients)
    if errors:
        from fluid_build.cli._common import CLIError

        for message in errors:
            LOG.error("schema_registry_check_failed: %s", message)
        raise CLIError(
            1,
            "plan_schema_registry_failed",
            {
                "errors": errors,
                "message": (
                    f"{len(errors)} schema registry check(s) failed; no plan was written. "
                    "Fix the stream schema or the registry reference, or ask the producer "
                    "about the subject."
                ),
            },
        )
    if not records:
        return plan
    from fluid_build.cli.console import cprint, warning

    for message in warnings:
        warning(message)
    for record in records:
        if record.get("result") == "compatible":
            cprint(
                f"Schema registry: {record['kind']} subject {record['subject']!r} "
                f"version {record['version']} matches build {record['build']!r}'s stream.schema"
            )
    plan[PLAN_KEY] = {"checked": records}
    # The kafka-connect blocks are the converter's at run time: neither checked nor
    # registered, said once so nobody reads the silence as a check.
    if connector_registries(contract):
        LOG.info("schema_registry_connector_blocks_not_checked")
    return plan
