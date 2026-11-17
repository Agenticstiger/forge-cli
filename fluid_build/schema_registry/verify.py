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

from .kinds import all_kinds, get_kind, names
from .reference import RegistryShapeError, Subject

LOG = logging.getLogger(__name__)

#: The ``plan.json`` key the checks are recorded under.
PLAN_KEY = "schemaRegistry"
#: The compatibility modes under which the registry's own check is plan's question.
_READER_MODES = frozenset({"BACKWARD", "BACKWARD_TRANSITIVE"})


def _say(text: str) -> None:
    """Print contract-controlled text literally: subject names may hold ``[...]``."""
    from fluid_build.cli.console import cprint_literal

    cprint_literal(text)


def _say_warning(text: str) -> None:
    from rich.markup import escape

    from fluid_build.cli.console import warning

    warning(escape(text))


def _writer(sv: Any) -> Tuple[List[Dict[str, Any]], str, Dict[str, str], Dict[str, str]]:
    from .clients import parse_schema
    from .schemas import writer_columns

    return writer_columns(parse_schema(sv.schema), sv.data_format)


def _check(
    subject: Subject,
    *,
    open_client: Callable[[Any], Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read and compare one subject. ``(record, error or None)``."""
    from .clients import RegistryError
    from .compare import describe, reader_changes
    from .schemas import UnsupportedSchema, column_paths, derive, registry_format, schema_text

    ref = subject.ref
    kind = get_kind(ref.kind)
    expected = registry_format(subject.format)
    record: Dict[str, Any] = {
        "build": subject.owner,
        "topic": subject.topic,
        "kind": ref.kind,
        "registry": kind.location(ref),
        "subject": ref.subject,
        "requested": ref.version,
    }
    at = f"{ref.where} ({ref.label})"
    if expected is None:
        return record, (
            f"{at}: the stream's format {subject.format!r} is not one the registry check "
            "reads (json or avro-confluent); nothing was read or compared"
        )
    # One client per kind: each kind reads its own way (kind.read).
    client = open_client(ref)
    try:
        sv = kind.read(client, ref)
        record["version"] = sv.version
        if sv.schema_id is not None:
            record["schemaId"] = sv.schema_id
        if sv.data_format != expected:
            return record, (
                f"{at}: version {sv.version} holds a {sv.data_format} schema, and the stream's "
                f"format {subject.format!r} reads {expected}"
            )
        writer, full, names, skipped = _writer(sv)
        read = set(column_paths(subject.columns)) & set(skipped)
        if read:
            path = sorted(read)[0]
            return record, (
                f"{at}: stream.schema reads {path!r}, and version {sv.version} gives it no "
                f"column type ({skipped[path]})"
            )
        if kind.checks_compatibility:
            name = full.rpartition(".")[2] or subject.topic
            reader = derive(
                subject.columns,
                expected,
                name=name,
                namespace=full.rpartition(".")[0] or None,
                names=names,
            )
            try:
                record["mode"] = kind.mode(client, ref)
            except RegistryError as exc:
                # The mode is context only: without it the registry's verdict is not
                # decisive (see _READER_MODES) and forge's comparison decides.
                LOG.debug("schema_registry_mode_unreadable subject=%s: %s", ref.subject, exc)
            ok, messages = kind.verdict(client, ref, sv.version, schema_text(reader), expected)
            # The registry judges the declared schema as the subject's next version under
            # the subject's mode. Only a BACKWARD mode asks what plan asks (can this
            # reader read version N's records); FORWARD and FULL also ask whether the
            # producer's schema reads ours, which a consumer reading a subset fails. So
            # the registry's verdict decides only under BACKWARD; the comparison below
            # always runs.
            if record.get("mode") not in _READER_MODES:
                record["registryVerdict"] = "compatible" if ok else "not-compatible"
                ok = True
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
    except (UnsupportedSchema, ValueError, KeyError, TypeError, AttributeError) as exc:
        return record, f"{at}: the subject's schema could not be read ({type(exc).__name__}: {exc})"
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


def _factories(clients: Mapping[str, Any], kinds: Optional[List[str]] = None) -> Mapping[str, Any]:
    """The client factories, keyed by kind name (``kinds``: the ones that may be read
    here, every kind by default). Any other keyword is a caller's typo (``enviorn=``),
    which ``**clients`` would otherwise swallow: refused."""
    accepted = list(names()) if kinds is None else kinds
    unknown = sorted(set(clients) - set(accepted))
    if unknown:
        raise TypeError(
            f"unexpected keyword argument(s) {unknown}: client factories are keyed by "
            f"registry kind ({', '.join(accepted)})"
        )
    return clients


def _opener(
    clients: Mapping[str, Any],
    *,
    contract: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    credential: Optional[str] = None,
) -> Callable[[Any], Any]:
    """``ref -> client``: each kind opens its own client, or the factory a caller passed
    under that kind's name."""

    def open_client(ref: Any) -> Any:
        return get_kind(ref.kind).client(
            ref,
            contract=contract,
            environ=environ,
            credential=credential,
            factory=clients.get(ref.kind),
        )

    return open_client


def _for_provider(produced: List[Subject], provider: Optional[str]) -> List[Subject]:
    """Only the subjects ``provider``'s plugin registers, when the run names one: a
    multi-cloud contract planned or applied one provider at a time is not refused over
    the other cloud's registry."""
    if provider is None:
        return produced
    from fluid_build.iac.provider_match import canonical_cloud

    # The flag as the provider gate reads it (aliases, case, spaces): an unreadable
    # spelling filters nothing out, so it can never skip a check.
    cloud = canonical_cloud(str(provider).strip().lower())
    if cloud not in {k.registering_cloud for k in all_kinds()}:
        return produced
    return [s for s in produced if get_kind(s.ref.kind).registering_cloud == cloud]


def _produced_checks(
    contract: Mapping[str, Any],
    produced: List[Subject],
    stage: str,
    environ: Optional[Mapping[str, str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """Egress, the derivable schema, and the operator's endpoint, for each produced subject."""
    from .policy import subject_findings
    from .produced import produced_findings, produced_set_errors

    errors.extend(produced_set_errors(contract, produced))
    for subject in produced:
        errors.extend(m for _, m in subject_findings(subject, stage=stage, environ=environ))
        more_errors, more_warnings = produced_findings(contract, subject)
        errors.extend(more_errors)
        warnings.extend(more_warnings)
        kind_errors, kind_warnings = get_kind(subject.ref.kind).produced_checks(
            subject, stage=stage, environ=environ
        )
        errors.extend(kind_errors)
        warnings.extend(kind_warnings)


def verify_consumed(
    contract: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    provider: Optional[str] = None,
    **clients: Any,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Check every consumed subject and the egress of every produced one.

    Returns ``(records, errors, warnings)``. ``clients`` are factories keyed by kind
    name (``confluent=``, ``glue=``) that replace the real client of that kind (tests
    pass fakes); each is called as its kind's ``client`` method calls it.
    """
    from .policy import subject_findings
    from .reference import checked_subjects, malformed_produced, produced_subjects

    clients = _factories(clients)

    errors: List[str] = list(malformed_produced(contract))
    warnings: List[str] = []
    records: List[Dict[str, Any]] = []
    try:
        consumed = checked_subjects(contract)
        produced = [] if errors else produced_subjects(contract)
    except RegistryShapeError as exc:
        return records, [*errors, str(exc)], warnings
    _produced_checks(contract, _for_provider(produced, provider), "plan", environ, errors, warnings)

    open_client = _opener(clients, contract=contract, environ=environ)
    for subject in consumed:
        refused = [m for s, m in subject_findings(subject, stage="plan", environ=environ)]
        if refused:
            errors.extend(refused)
            continue
        if subject.ref.secret_ref:
            warnings.append(
                f"{subject.ref.where}: subject {subject.ref.subject!r} is verified at apply, "
                "where the credential is resolved (plan never fetches a contract secret)"
            )
            records.append(
                {
                    "build": subject.owner,
                    "topic": subject.topic,
                    "kind": subject.ref.kind,
                    "registry": get_kind(subject.ref.kind).location(subject.ref),
                    "subject": subject.ref.subject,
                    "requested": subject.ref.version,
                    "result": "verified-at-apply",
                }
            )
            continue
        record, error = _check(subject, open_client=open_client)
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
    provider: Optional[str] = None,
    **clients: Any,
) -> Dict[str, Any]:
    """Run :func:`verify_consumed`; refuse the plan on an error, else record the checks.

    A contract that names no registry returns ``plan`` untouched: no new key, no
    digest change.

    Raises:
        CLIError: ``plan_schema_registry_failed`` with every error.
    """
    from .reference import connector_registries

    records, errors, warnings = verify_consumed(
        contract, environ=environ, provider=provider, **clients
    )
    if errors:
        from fluid_build.cli._common import CLIError

        # The messages reach the user through the CLIError; the log carries the count
        # only, so nothing read near a resolved credential is written to a log.
        LOG.error("schema_registry_check_failed count=%d", len(errors))
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
    for message in warnings:
        _say_warning(message)
    if not records:
        return plan
    for record in records:
        if record.get("result") == "compatible":
            _say(
                f"Schema registry: {record['kind']} subject {record['subject']!r} "
                f"version {record['version']} matches the stream.schema of build {record['build']!r}"
            )
    plan[PLAN_KEY] = {"checked": records}
    # The kafka-connect blocks are the converter's at run time: neither checked nor
    # registered, said once so nobody reads the silence as a check.
    if connector_registries(contract):
        LOG.info("schema_registry_connector_blocks_not_checked")
    return plan


def verify_at_apply(
    contract: Mapping[str, Any],
    *,
    dry_run: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    provider: Optional[str] = None,
    **clients: Any,
) -> List[Dict[str, Any]]:
    """``fluid apply``'s check of each consumed subject whose registry needs a credential.

    Plan never fetches a contract secret, so it leaves these to apply (founder
    decision, 2026-11-16). Before any ``tofu apply``, for each such subject: the
    egress rules at their run severity, then ``resolve_secret`` for that one host
    (the secrets policy: scheme, prefix, the ``apply`` stage, the ``NAME@host``
    grant), then the same read and comparison as plan, with the credential as Basic
    auth over https. A dry run fetches no secret and reads no registry. Every
    produced subject gets plan's checks again, with an unset
    ``SCHEMA_REGISTRY_REST_ENDPOINT`` an error here. Returns the records.

    Raises:
        CLIError: ``apply_schema_registry_failed`` with every error.
    """
    from urllib.parse import urlsplit

    from .policy import subject_findings
    from .reference import RegistryShapeError, checked_subjects, produced_subjects

    # Apply reads only subjects with a credential: kinds that take auth.
    clients = _factories(clients, [k.name for k in all_kinds() if "auth" in k.fields])

    errors: List[str] = []
    warnings: List[str] = []
    try:
        produced = produced_subjects(contract)
    except RegistryShapeError as exc:
        produced, errors = [], [str(exc)]
    # Produced subjects: what OpenTofu registers, and where, re-checked here, where the
    # environment tofu runs with is final (a plan made elsewhere did not see it).
    _produced_checks(
        contract, _for_provider(produced, provider), "apply", environ, errors, warnings
    )
    try:
        consumed = [] if dry_run else checked_subjects(contract)
    except RegistryShapeError as exc:
        consumed, errors = [], [*errors, str(exc)]
    subjects = [s for s in consumed if s.ref.secret_ref]
    records: List[Dict[str, Any]] = []
    for subject in subjects:
        ref = subject.ref
        refused = [m for _, m in subject_findings(subject, stage="apply", environ=environ)]
        if refused:
            errors.extend(refused)
            continue
        from fluid_build.secrets.errors import SecretError
        from fluid_build.secrets.resolve import resolve_secret

        try:
            secret = resolve_secret(
                ref.secret_ref, host=urlsplit(str(ref.url)).hostname, environ=environ
            )
        except SecretError as exc:
            errors.append(f"{ref.where}.auth.secretRef: {exc}")
            continue
        credential = (
            secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        )

        open_client = _opener(clients, environ=environ, credential=credential)
        record, error = _check(subject, open_client=open_client)
        record["result"] = "incompatible" if error else record.get("result", "compatible")
        if error:
            errors.append(error)
        records.append(record)
    if errors:
        from fluid_build.cli._common import CLIError

        # The messages reach the user through the CLIError; the log carries the count
        # only, so nothing read near a resolved credential is written to a log.
        LOG.error("schema_registry_check_failed count=%d", len(errors))
        raise CLIError(
            1,
            "apply_schema_registry_failed",
            {
                "errors": errors,
                "message": (
                    f"{len(errors)} schema registry check(s) failed before tofu apply; "
                    "nothing was applied. Fix the stream schema or the registry reference, "
                    "or ask the producer about the subject."
                ),
            },
        )
    for message in warnings:
        _say_warning(message)
    for record in records:
        _say(
            f"Schema registry: {record['kind']} subject {record['subject']!r} "
            f"version {record['version']} matches the stream.schema of build {record['build']!r}"
        )
    return records
