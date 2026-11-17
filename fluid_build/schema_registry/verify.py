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
    confluent: Callable[[str], Any],
    glue: Callable[[], Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read and compare one subject. ``(record, error or None)``."""
    from .clients import RegistryError
    from .compare import describe, reader_changes
    from .schemas import UnsupportedSchema, column_paths, derive, registry_format, schema_text

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
        writer, full, names, skipped = _writer(sv)
        read = set(column_paths(subject.columns)) & set(skipped)
        if read:
            path = sorted(read)[0]
            return record, (
                f"{at}: stream.schema reads {path!r}, and version {sv.version} gives it no "
                f"column type ({skipped[path]})"
            )
        if ref.kind == "confluent":
            name = full.rpartition(".")[2] or subject.topic
            reader = derive(
                subject.columns,
                expected,
                name=name,
                namespace=full.rpartition(".")[0] or None,
                names=names,
            )
            try:
                record["mode"] = client.compatibility_level(ref.subject)
            except RegistryError:
                pass
            ok, messages = client.check_compatibility(
                ref.subject, sv.version, schema_text(reader), expected
            )
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


def _aws_region(contract: Mapping[str, Any], environ: Optional[Mapping[str, str]]) -> Optional[str]:
    """The region a Glue registry is read in: where ``fluid apply`` puts the product.

    The AWS emitter's own region (every AWS binding's ``location.region``), else the
    environment in the AWS SDK order (``AWS_REGION``, then ``AWS_DEFAULT_REGION``):
    boto3 1.x reads only the second (the same rule as ``cli/_diff_live.py``).
    """
    import os

    from fluid_build.iac.providers.aws import AwsIacPlugin

    try:
        region = AwsIacPlugin().provider_block_for(contract).get("region")
    except Exception:  # noqa: BLE001 - an unemittable contract: fall back to the env
        region = None
    if region:
        return str(region)
    env = os.environ if environ is None else environ
    return next((env[k] for k in ("AWS_REGION", "AWS_DEFAULT_REGION") if env.get(k)), None)


def _same_endpoint(a: str, b: str) -> bool:
    """Whether two registry URLs are one endpoint: scheme, host, port (default filled
    in) and path, ignoring case and a trailing slash. An unreadable URL is no match."""
    from urllib.parse import urlsplit

    def key(url: str) -> Any:
        try:
            parts = urlsplit(url.strip())
            scheme = parts.scheme.lower()
            port = parts.port or {"https": 443, "http": 80}.get(scheme)
        except ValueError:  # an unreadable port or IPv6 literal
            return None
        return scheme, (parts.hostname or "").lower(), port, parts.path.rstrip("/")

    first = key(a)
    return first is not None and first == key(b)


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


def _for_provider(produced: List[Subject], provider: Optional[str]) -> List[Subject]:
    """Only the subjects ``provider``'s plugin registers, when the run names one: a
    multi-cloud contract planned or applied one provider at a time is not refused over
    the other cloud's registry."""
    if provider is None:
        return produced
    from fluid_build.iac.provider_match import canonical_cloud

    from .produced import REGISTERING_CLOUD

    # The flag as the provider gate reads it (aliases, case, spaces): an unreadable
    # spelling filters nothing out, so it can never skip a check.
    cloud = canonical_cloud(str(provider).strip().lower())
    if cloud not in REGISTERING_CLOUD.values():
        return produced
    return [s for s in produced if REGISTERING_CLOUD.get(s.ref.kind) == cloud]


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
        if subject.ref.kind == "confluent":
            found: List[str] = []
            _operator_endpoint(subject, environ, errors, found)
            # Apply is where OpenTofu registers it: an unset endpoint cannot register.
            (errors if stage == "apply" else warnings).extend(found)


def verify_consumed(
    contract: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
    confluent: Optional[Callable[[str], Any]] = None,
    glue: Optional[Callable[[], Any]] = None,
    provider: Optional[str] = None,
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
    _produced_checks(contract, _for_provider(produced, provider), "plan", environ, errors, warnings)
    if confluent is None:
        from .clients import ConfluentRegistry

        confluent = ConfluentRegistry
    if glue is None:
        from .clients import GlueRegistry

        region = _aws_region(contract, environ)
        glue = lambda: GlueRegistry(region=region)  # noqa: E731
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
                    "registry": subject.ref.url,
                    "subject": subject.ref.subject,
                    "requested": subject.ref.version,
                    "result": "verified-at-apply",
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
    confluent: Optional[Callable[..., Any]] = None,
    provider: Optional[str] = None,
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
    if confluent is None:
        from .clients import ConfluentRegistry

        confluent = ConfluentRegistry
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

        def client(url: str, _credential: str = credential) -> Any:
            return confluent(url, credential=_credential)

        record, error = _check(subject, confluent=client, glue=lambda: None)
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
