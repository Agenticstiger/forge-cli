# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema-registry subject -> FLUID consumer contract importer.

``fluid import schema-registry <subject> --registry confluent --url <https URL>`` or
``... --registry glue [--registry-name NAME]`` reads one subject and writes a contract (on
the schema :func:`contract_version` names) with one build whose ``properties.stream`` is what
:func:`fluid_build.schema_registry.importer.stream_from_subject` returns, with
``registry.version`` always set to the version actually read (also for a latest import).
A Glue import takes its region from the AWS SDK's own resolution (``AWS_REGION``, then
``AWS_DEFAULT_REGION``, then the profile). The import never resolves a secret: a registry
that needs credentials answers 401/403 and that is surfaced.

Nothing from :mod:`fluid_build.schema_registry` is imported at module level (``fluid --help``
must stay light); the registry package loads when an import runs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import Importer, ImportReport

TOOL = "schema-registry"
DOC = "https://agenticstiger.github.io/forge_docs/cli/import.html"

# argparse dest -> flag, for every option that belongs to this tool alone.
REGISTRY_FLAGS = {
    "registry": "--registry",
    "url": "--url",
    "registry_name": "--registry-name",
    "registry_version": "--version",
}

#: The importers that read a registry for the columns of the topics they find.
TOPIC_TOOLS = ("airflow", "pyspark")
_KINDS = ("confluent", "glue")
# The subject's schema types that become a consumed stream format.
_CONSUMABLE = ("AVRO", "JSON")


def _refusal(what: str, why: str, fix: str, **extras: Any) -> Exception:
    from fluid_build._errors import SchemaValidationError

    return SchemaValidationError(what=what, why=why, fix=fix, doc=DOC, extras=extras)


def check_flags(args: Any, tool: Optional[str]) -> None:
    """Refuse registry flags that do not apply, or are missing, for ``tool``.

    Raises a typed ``SchemaValidationError`` (exit code 1 through the CLI).
    """
    given = {
        dest: getattr(args, dest, None)
        for dest in REGISTRY_FLAGS
        if getattr(args, dest, None) is not None
    }
    if tool in TOPIC_TOOLS:
        _check_topic_tool(given, tool)
        return
    if tool != TOOL:
        if given:
            flags = ", ".join(REGISTRY_FLAGS[d] for d in given)
            raise _refusal(
                f"{flags} only applies to `fluid import {TOOL}`",
                f"The importer for {tool or 'a directory scan'} does not read a schema registry.",
                f"Remove {flags}, or run `fluid import {TOOL} <subject> --registry confluent|glue`.",
                flags=sorted(given),
            )
        return

    if not getattr(args, "source", None):
        raise _refusal(
            f"`fluid import {TOOL}` requires the subject name",
            "The importer reads one subject (a Confluent subject, or a Glue schema name).",
            f"`fluid import {TOOL} <subject> --registry confluent --url <https URL>` or "
            f"`fluid import {TOOL} <schema-name> --registry glue`.",
        )
    kind = given.get("registry")
    if kind is None:
        raise _refusal(
            f"`fluid import {TOOL}` requires --registry",
            "A subject is read from one registry kind.",
            "Pass --registry confluent (with --url) or --registry glue.",
        )
    _check_pairing(
        kind, given, f"`fluid import {TOOL} <subject> --registry confluent --url https://<host>`"
    )


def _check_pairing(kind: str, given: Dict[str, Any], example: str) -> None:
    """``--url`` belongs to a Confluent registry and ``--registry-name`` to a Glue one."""
    wrong: Dict[str, str] = {}
    if kind == "confluent":
        if not given.get("url"):
            raise _refusal(
                "--registry confluent requires --url",
                "A Confluent registry is named by its https REST endpoint.",
                f"{example}.",
            )
        wrong = {"registry_name": "--registry confluent"} if "registry_name" in given else {}
    elif kind == "glue":
        wrong = {d: "--registry glue" for d in ("url",) if d in given}
    for dest, owner in wrong.items():
        other = "glue" if owner.endswith("confluent") else "confluent"
        raise _refusal(
            f"{REGISTRY_FLAGS[dest]} does not apply with {owner}",
            f"{REGISTRY_FLAGS[dest]} is a --registry {other} option.",
            f"Remove {REGISTRY_FLAGS[dest]}.",
            flag=REGISTRY_FLAGS[dest],
        )


def _check_topic_tool(given: Dict[str, Any], tool: str) -> None:
    """The registry flags of ``fluid import airflow|pyspark``: optional, and they give the columns
    of the Kafka topics the jobs write (the subject is ``<topic>-value``, then ``<topic>``)."""
    if "registry_version" in given:
        raise _refusal(
            "--version does not apply to `fluid import " + tool + "`",
            "--version pins one subject's version; this import reads one subject per topic.",
            "Remove --version.",
            flag="--version",
        )
    kind = given.get("registry")
    if kind is None:
        if given:
            flags = ", ".join(REGISTRY_FLAGS[d] for d in given)
            raise _refusal(
                f"{flags} requires --registry",
                "A registry endpoint or name is only used with the kind of registry it belongs to.",
                f"Pass --registry confluent (with --url) or --registry glue, or remove {flags}.",
                flags=sorted(given),
            )
        return
    _check_pairing(
        kind, given, f"`fluid import {tool} <dir> --registry confluent --url https://<host>`"
    )


class _ProtobufSubject(Exception):
    """The subject is Protobuf, which 0.7.7 cannot consume as a stream format."""


class _Reader:
    """Builds the registry client on first use and records the version read.

    ``stream_from_subject`` runs its egress check before it first asks for a version, so
    no client exists before a refused host is refused. A Protobuf subject stops here,
    before its (non-JSON) schema text reaches the parser.
    """

    def __init__(self, ref: Any) -> None:
        self.ref = ref
        self.read: Any = None
        self._client: Any = None

    def get_version(self, *args: Any) -> Any:
        from fluid_build.schema_registry.kinds import get_kind

        if self._client is None:
            self._client = get_kind(self.ref.kind).client(self.ref)
        kind = get_kind(self.ref.kind)
        sv = kind.read(self._client, self.ref)
        self.read = sv
        if str(sv.data_format).upper() not in _CONSUMABLE:
            raise _ProtobufSubject(str(sv.data_format))
        return sv

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _glue_region(reader: Any) -> Optional[str]:
    """The region a Glue read used: the environment's, else the boto3 client's, else the
    session's. ``None`` when boto3 cannot tell."""
    from fluid_build.schema_registry.kinds.glue import env_region

    region = env_region()
    if region:
        return region
    inner = getattr(getattr(reader, "_client", None), "_client", None)
    name = getattr(getattr(inner, "meta", None), "region_name", None)
    if isinstance(name, str) and name:
        return name
    try:
        import boto3

        found = boto3.session.Session().region_name
    except Exception:  # noqa: BLE001 - no boto3, or a broken profile: say nothing
        return None
    return found if isinstance(found, str) and found else None


def contract_version() -> str:
    """The ``fluidVersion`` an imported contract declares.

    The newest stable schema (``FluidSchemaManager.latest_bundled_version``) once it carries
    the registry block, and until then the first schema that does
    (:data:`fluid_build.schema_registry.FIRST_VERSION`, a preview today). Neither is typed
    here, so a promotion moves the import with it.
    """
    from fluid_build.schema_manager import FluidSchemaManager, SchemaVersion
    from fluid_build.schema_registry import FIRST_VERSION

    stable = FluidSchemaManager.latest_bundled_version()
    if SchemaVersion.parse(stable) >= SchemaVersion.parse(FIRST_VERSION):
        return stable
    return FIRST_VERSION


def _check_importable(
    contract: Dict[str, Any], stream: Dict[str, Any], source: str, version: int
) -> None:
    """Refuse, before anything is written, a subject that would not become a usable contract."""
    from fluid_build.schema_manager import FluidSchemaManager
    from fluid_build.streaming.types import parse_fields

    at = f"subject {source!r} version {version}"
    columns = stream["schema"]
    if not columns:
        raise _refusal(
            f"{at} declares no fields",
            "A consumed stream needs at least one column.",
            "Import a subject that declares fields, or declare the stream by hand.",
            subject=source,
            version=version,
        )
    try:
        parse_fields(columns)
    except ValueError as exc:
        raise _refusal(
            f"{at} has a field FLUID cannot use as a column",
            str(exc),
            "fluid plan reads every column as a SQL identifier (letters, digits and _, not "
            "starting with a digit): rename the field in the registry, or declare the stream "
            "by hand.",
            subject=source,
            version=version,
        ) from exc
    result = FluidSchemaManager().validate_contract(
        contract, schema_version=contract["fluidVersion"], offline_only=True
    )
    if result.is_valid:
        return
    errors = "; ".join(result.errors)
    topic_bad = any("stream.topic" in e for e in result.errors)
    what = f"{at} does not give a valid contract"
    why = errors
    fix = "Fix what the validator names in the registry, or declare the stream by hand."
    if topic_bad:
        what = f"{at} does not give a valid Kafka topic name ({stream['topic']!r})"
        why = (
            "The topic comes from the subject: <topic>-value, else the subject itself. "
            f"Validator: {errors}"
        )
        fix = "Use a subject whose topic is 1-249 characters of letters, digits, '.', '_' and '-'."
    raise _refusal(what, why, fix, subject=source, version=version)


def _build_contract(
    topic: str, stream: Dict[str, Any], kind: str, subject: str, version: str
) -> Dict[str, Any]:
    from fluid_build.util.contract import slugify_identifier

    cid = slugify_identifier(topic, fallback="imported-stream")
    expose_id = cid.replace("-", "_")
    columns = stream["schema"]
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": cid,
        "name": f"{topic} (imported from a {kind} schema registry)",
        "metadata": {"owner": {}},
        "builds": [
            {
                "id": f"consume-{cid}",
                "description": (
                    f"TODO: choose the engine and the sink. Imported from subject {subject!r}: "
                    f"this build reads the Kafka topic {topic!r}. Set `engine`, replace "
                    "`properties.sql` and point the exposes entry at a real binding."
                ),
                "pattern": "embedded-logic",
                "outputs": [expose_id],
                "properties": {
                    "sql": "-- TODO: transform the stream into the output",
                    "stream": stream,
                },
            }
        ],
        "exposes": [
            {
                "exposeId": expose_id,
                "kind": "table",
                "binding": {"platform": "other", "format": "other", "location": {}},
                "contract": {"schema": copy.deepcopy(columns)},
            }
        ],
    }


@dataclass
class SchemaRegistryImporter(Importer):
    name: str = TOOL

    def can_import(self, source: str) -> bool:
        return bool(source)

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        opts = options or {}
        kind = opts.get("registry")
        if kind not in _KINDS:
            raise _refusal(
                "--registry confluent or --registry glue is required",
                f"got {kind!r}.",
                "Pass --registry confluent (with --url) or --registry glue.",
            )
        from fluid_build.schema_registry.clients import RegistryError
        from fluid_build.schema_registry.importer import ref_for, stream_from_subject
        from fluid_build.schema_registry.reference import RegistryShapeError
        from fluid_build.schema_registry.schemas import UnsupportedSchema

        wanted = opts.get("registry_version")
        try:
            ref = ref_for(
                kind,
                source,
                url=opts.get("url"),
                registry_name=opts.get("registry_name"),
                version="latest" if wanted is None else wanted,
            )
        except (RegistryShapeError, ValueError) as exc:
            raise _refusal(
                f"the registry reference for subject {source!r} is not valid",
                str(exc),
                "Check --url (an https endpoint with no credentials) and --registry-name.",
                subject=source,
            ) from exc

        reader = _Reader(ref)
        shown = "latest" if wanted is None else f"version {wanted}"
        try:
            stream = stream_from_subject(ref, client=reader)
        except _ProtobufSubject as exc:
            read = reader.read
            raise _refusal(
                f"subject {source!r} (version {read.version}) is {exc}, not Avro or JSON Schema",
                f"FLUID {contract_version()} has no consumed stream format for Protobuf, so no "
                "stream block can carry it.",
                "Import an Avro or JSON Schema subject, or declare the stream by hand.",
                subject=source,
                version=read.version,
            ) from None
        except UnsupportedSchema as exc:
            read = reader.read
            at = f"version {read.version}" if read is not None else shown
            raise _refusal(
                f"subject {source!r} ({at}) has a schema FLUID has no column type for",
                str(exc),
                "Simplify the schema (a union other than [null, T] and untyped properties have "
                "no column type), or declare the stream columns by hand.",
                subject=source,
            ) from exc
        except RegistryError as exc:
            fix = (
                "Check the subject, the registry endpoint and your network. The import sends "
                "no credential: a registry that needs one has to be read another way."
            )
            if kind == "glue":
                region = _glue_region(reader) or "the AWS profile's region"
                fix = (
                    f"Check the subject and the registry name. Glue was read in {region}: set "
                    "AWS_REGION (or AWS_DEFAULT_REGION) to the registry's region if it is "
                    "another."
                )
            raise _refusal(
                f"could not read subject {source!r} ({shown}) from the {kind} registry",
                str(exc),
                fix,
                subject=source,
            ) from exc
        finally:
            reader.close()

        read = reader.read
        # Pinned to the version read, also when the read was "latest".
        stream["registry"]["version"] = read.version
        columns = stream["schema"]
        contract = _build_contract(stream["topic"], stream, kind, source, contract_version())
        _check_importable(contract, stream, source, read.version)
        report = ImportReport()
        report.mapped_one_to_one += [
            f"topic {stream['topic']}",
            f"schema version {read.version} read ({len(columns)} columns, "
            f"format {stream['format']})",
        ]
        report.required_defaults.append(
            "exposes[0] binds platform: other (no sink chosen); builds[0] sets no engine"
        )
        report.notes.append(
            "Choose the engine and the sink, replace `properties.sql`, then run `fluid validate`."
        )
        return contract, report
