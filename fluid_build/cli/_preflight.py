# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid doctor --contract``: can this operation run here, for this contract?

A supported provider name does not say whether a product's nested columns, table
layout, build engine, governance and state backend all work together on this
machine. This report answers that for one contract and one operation, before a CI
queue starts:

* the schema revision the contract resolves to, and where it came from;
* the Forge build, the installed extras against the pins this build declares, the
  plugins installed through entry points;
* the external tools and adapters the operation runs (``tofu``, ``dbt`` and its
  adapter, ``spark-submit``) and the OpenTofu provider pins;
* a capability breakdown in four classes: ``exact``, ``lossy``, ``unsupported`` and
  ``requires-live-proof``.

It is offline. It reads the bundled schemas, the installed distributions' metadata
and the tools' ``--version``; it never fetches a schema, a provider or a tool, and
never installs one. ``--live`` adds a read-only check of the cloud identity and the
state backend the operation needs (STS ``GetCallerIdentity``, a token refresh, a
bucket read); nothing else touches the network.

Every capability comes from the code the operation itself runs: the column types
from the emitters' type tables (:func:`fluid_build.iac.providers.gcp.bigquery_column_outcome`,
:func:`fluid_build.iac.providers.aws.glue_column_outcome`), and everything else from
the contract rules ``fluid validate`` runs (layout, governance, Iceberg, Databricks,
packaging), so preflight cannot pass what ``generate iac`` refuses.

The report holds no environment value: no path, host, account, bucket, user or
variable value. Variable *names* the provider reads are listed, and every string
goes through the secret redactor before it is printed, so two reports (a laptop and
a CI runner) can be compared and shared: :func:`compare_reports` names the version
gaps between them.

Borrowed: the capability classes follow dbt-adapters' ``Support`` enum
(``dbt/adapters/capability.py``: Unknown, Unsupported, NotImplemented, Versioned,
Full), with ``requires-live-proof`` for dbt's ``Unknown``; the versioned report
header follows ``pip inspect`` (``"version": "1"`` plus the producer version); the
extras check evaluates ``Requires-Dist`` markers with ``extra`` set, as ``packaging``
documents, rather than inventing a lockfile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform as _platform
import shutil
import sys
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: The report's format. A field is only ever added under this name; a change that
#: removes or renames one bumps it, as pip's inspect report does.
REPORT_FORMAT = "fluid-preflight/1"

#: The distribution whose extras and pins the report reads.
DIST = "data-product-forge"

OPERATIONS = ("validate", "generate", "plan", "apply", "ship")
DEFAULT_OPERATION = "plan"
#: Operations that change a cloud: they need its credentials, its state backend,
#: ``tofu`` and the provider SDKs. ``plan`` here is the offline plan (no ``--tofu``).
_LIVE_OPERATIONS = frozenset({"apply", "ship"})

EXACT = "exact"
LOSSY = "lossy"
UNSUPPORTED = "unsupported"
LIVE = "requires-live-proof"
CLASSES = (EXACT, LOSSY, UNSUPPORTED, LIVE)

#: The ``fluid validate`` checks whose warnings say a declaration is not honoured in
#: full on its target (a lossy capability). Other warnings (lint, ids) are not about
#: what runs here, and are only counted.
_LOSSY_EVENTS = frozenset(
    {
        "contract_gcp_binding",
        "contract_governance",
        "contract_iceberg_binding",
        "contract_iceberg_sink",
        "contract_confluent_binding",
        "contract_databricks_binding",
        "contract_vector_binding",
        "contract_continuous_trigger",
        "contract_packaging",
        "contract_spark",
        "contract_spark_identity",
        "contract_managed_flink",
        "contract_streaming_types",
    }
)

#: Warnings that name what only the live run decides: a secret reference whose
#: resolver is not installed here, or whose host is judged only at apply.
_LIVE_EVENTS = frozenset({"contract_secret_ref"})

#: Cloud -> the extra that installs its SDKs.
_CLOUD_EXTRAS = {
    "aws": "aws",
    "gcp": "gcp",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "local": "local",
}

#: Cloud -> the dbt adapter a dbt build on it loads.
_DBT_ADAPTERS = {
    "gcp": "dbt-bigquery",
    "snowflake": "dbt-snowflake",
    "databricks": "dbt-databricks",
    "aws": "dbt-athena-community",
    "local": "dbt-duckdb",
}

#: Clouds ``--live`` has a read-only identity check for. The others stay
#: ``requires-live-proof`` with the reason.
_LIVE_IDENTITY = ("aws", "gcp")

_MAX_TEXT = 2000
_MAX_REPORT_BYTES = 16 * 1024 * 1024


class PreflightError(ValueError):
    """The request cannot be answered: an unknown operation, an unreadable report."""


@dataclass(frozen=True)
class Capability:
    """One thing the operation needs, and whether it holds here."""

    cls: str
    area: str
    target: str
    subject: str
    detail: str
    fix: str = ""

    def as_dict(self) -> Dict[str, str]:
        row = asdict(self)
        row["class"] = row.pop("cls")
        return row


class Probe:
    """What the report reads from this machine. Tests replace it; nothing here
    touches the network or installs anything."""

    def forge_version(self) -> str:
        from fluid_build import __version__

        return str(__version__)

    def editable(self) -> Optional[bool]:
        """Is Forge an editable install (a checkout) rather than a built wheel?"""
        try:
            from importlib import metadata

            records = metadata.distribution(DIST).files or []
            # PEP 610's direct_url.json, read as UTF-8 like every text file here.
            text = next(
                (
                    f.read_text(encoding="utf-8")
                    for f in records
                    if f.name == "direct_url.json" and f.parent.name.endswith(".dist-info")
                ),
                "",
            )
        except Exception:  # noqa: BLE001 - no metadata: not known
            return None
        if not text:
            return False
        try:
            return bool((json.loads(text).get("dir_info") or {}).get("editable"))
        except (ValueError, AttributeError):
            return None

    def python_version(self) -> str:
        return _platform.python_version()

    def platform(self) -> str:
        return f"{sys.platform}-{_platform.machine().lower()}"

    def dist_version(self, name: str) -> Optional[str]:
        from importlib import metadata

        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - broken metadata reads as absent
            return None

    def requires(self, name: str) -> Optional[List[str]]:
        from importlib import metadata

        try:
            return list(metadata.requires(name) or [])
        except Exception:  # noqa: BLE001 - not installed: no pins to read
            return None

    def entry_points(self, group: str) -> List[Tuple[str, str, str]]:
        from importlib import metadata

        rows = []
        try:
            eps = metadata.entry_points(group=group)
        except Exception:  # noqa: BLE001
            return rows
        for ep in eps:
            dist = getattr(ep, "dist", None)
            rows.append(
                (
                    str(ep.name),
                    str(getattr(dist, "name", "") or ""),
                    str(getattr(dist, "version", "") or ""),
                )
            )
        return rows

    def tofu(self) -> Tuple[bool, Optional[str]]:
        """``(found, version)``: ``tofu --version``, the local binary only."""
        from fluid_build.iac import runner

        if runner.tofu_path() is None:
            return False, None
        version = runner.tofu_version()
        return True, ".".join(str(p) for p in version) if version else None

    def dbt(self, detect: bool) -> Tuple[bool, Optional[str], Optional[str]]:
        """``(found, flavor, version)`` of the dbt the runner would run
        (``$DBT_EXECUTABLE``, PATH, the venv's own). Its version (``dbt --version``)
        is read only when the operation runs dbt."""
        from fluid_build.build_runners.dbt.runner import (
            _detect_dbt_engine,
            _resolve_dbt_executable,
        )

        executable = _resolve_dbt_executable()
        if executable is None:
            return False, None, None
        if not detect:
            return True, None, None
        flavor, version = _detect_dbt_engine(executable)
        return True, (None if flavor == "unknown" else flavor), version or None

    def which(self, name: str) -> bool:
        return shutil.which(name) is not None

    def environ(self) -> Mapping[str, str]:
        return os.environ


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass
class _Needs:
    clouds: List[str] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    adapters: List[str] = field(default_factory=list)


def build_report(
    contract: Mapping[str, Any],
    *,
    operation: str = DEFAULT_OPERATION,
    env: Optional[str] = None,
    contract_path: Optional[str] = None,
    live: bool = False,
    provider: Optional[str] = None,
    state_backend: Optional[str] = None,
    overlay_warnings: Sequence[str] = (),
    probe: Optional[Probe] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """The preflight report for ``operation`` on ``contract`` (already loaded, with
    ``env``'s overlay applied). ``contract_path`` is only used to resolve files the
    contract names; it is never written into the report. ``provider`` and
    ``state_backend`` are ``apply``'s ``--provider`` and ``--state-backend``."""
    if operation not in OPERATIONS:
        raise PreflightError(f"unknown operation {operation!r}: use one of {', '.join(OPERATIONS)}")
    probe = probe or Probe()
    log = logger or logging.getLogger(__name__)
    clouds = _clouds(contract)
    capabilities: List[Capability] = []
    scope = clouds
    if operation in _LIVE_OPERATIONS and [c for c in clouds if c != "local"]:
        resolved, refusal = _resolve_cloud(contract, provider)
        scope = [resolved] if resolved else [c for c in clouds if c == "local"]
        capabilities += [refusal] if refusal else []
    needs = _needs(contract, operation, scope)

    schema_revision, schema_caps = _schema_revision(contract)
    capabilities += schema_caps
    validation, rule_caps = _contract_rules(contract, env, contract_path, log, overlay_warnings)
    capabilities += rule_caps
    capabilities += _column_capabilities(contract)
    extras = _extras(probe, needs.extras)
    capabilities += _extra_capabilities(extras)
    tools = _tools(probe, needs.tools)
    capabilities += _tool_capabilities(tools)
    adapters = _adapters(probe, needs, clouds, tools)
    capabilities += _adapter_capabilities(adapters)
    backend, backend_spec = _state_backend(
        probe, contract, operation, needs.clouds, state_backend, provider
    )
    capabilities += _live_capabilities(probe, operation, needs.clouds, backend)
    live_checks = run_live_checks(needs.clouds, backend, backend_spec) if live else None
    if live_checks is not None:
        capabilities = _apply_live_checks(capabilities, live_checks)

    scrub = _path_scrubber(contract_path)
    report: Dict[str, Any] = {
        "version": REPORT_FORMAT,
        "operation": operation,
        "env": env,
        "mode": "live" if live else "offline",
        "contract": {
            "id": _text(contract.get("id")) if isinstance(contract, Mapping) else "",
            "fluidVersion": _text(contract.get("fluidVersion") or ""),
            "clouds": clouds,
        },
        "forge": {
            "version": probe.forge_version(),
            "editable": probe.editable(),
            "python": probe.python_version(),
            "platform": probe.platform(),
        },
        "schema_revision": schema_revision,
        "validation": validation,
        "needs": {
            "clouds": needs.clouds,
            "extras": needs.extras,
            "tools": needs.tools,
        },
        "extras": extras,
        "plugins": _plugins(probe),
        "tools": tools,
        "adapters": adapters,
        "state_backend": backend,
        "capabilities": [scrub(c).as_dict() for c in _sorted(capabilities)],
    }
    if live_checks is not None:
        report["live_checks"] = live_checks
    report["summary"] = _summary(report)
    return redact_report(report, contract_path=contract_path)


def _resolve_cloud(
    contract: Mapping[str, Any], provider: Optional[str]
) -> Tuple[Optional[str], Optional[Capability]]:
    """The one cloud ``apply`` provisions, chosen as ``apply`` chooses it
    (``generate_iac._resolve_provider``): ``--provider``, else the only cloud the
    contract declares. A contract on several clouds needs ``--provider``."""
    from ._common import CLIError
    from .generate_iac import _resolve_provider

    try:
        return str(_resolve_provider(dict(contract), provider or "auto")), None
    except CLIError as exc:
        reason = _text((exc.context or {}).get("error") or exc.event)
        fix = "Pass --provider with the cloud to provision, as apply takes it."
        return None, Capability(
            UNSUPPORTED, "provider", "contract", "binding.platform", reason, fix
        )


def exit_code(report: Mapping[str, Any], *, strict: bool = False) -> int:
    """0 when the operation can run here as far as this machine can tell; 1 when
    something is unsupported, a live check failed, or (``strict``) something is lossy."""
    summary = report.get("summary") or {}
    if summary.get("verdict") == "not-ready":
        return 1
    if strict and (summary.get("counts") or {}).get(LOSSY):
        return 1
    return 0


def _summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    counts = {cls: 0 for cls in CLASSES}
    for cap in report["capabilities"]:
        counts[cap["class"]] = counts.get(cap["class"], 0) + 1
    if counts[UNSUPPORTED]:
        verdict = "not-ready"
    elif counts[LIVE]:
        verdict = "needs-live-proof"
    else:
        verdict = "ready"
    return {"counts": counts, "verdict": verdict}


def _sorted(caps: Iterable[Capability]) -> List[Capability]:
    order = {cls: i for i, cls in enumerate((UNSUPPORTED, LOSSY, LIVE, EXACT))}
    unique = {(c.cls, c.area, c.target, c.subject, c.detail): c for c in caps}
    return sorted(
        unique.values(), key=lambda c: (order[c.cls], c.area, c.target, c.subject, c.detail)
    )


def _path_roots(contract_path: Optional[str]) -> List[Tuple[str, str]]:
    """``(root, placeholder)`` for the directories a message can name on this machine,
    longest first, each also in its resolved form (``/tmp`` is ``/private/tmp`` on
    macOS). The contract's own directory reads ``<contract-dir>``, the home ``~``."""
    import tempfile
    from pathlib import Path

    named: List[Tuple[Optional[str], str]] = []
    if contract_path:
        named.append((str(Path(contract_path).parent), "<contract-dir>"))
    named += [
        (os.getcwd(), "<cwd>"),
        (str(Path.home()), "~"),
        (sys.prefix, "<python>"),
        (sys.base_prefix, "<python>"),
        (tempfile.gettempdir(), "<tmp>"),
    ]
    roots: Dict[str, str] = {}
    for root, placeholder in named:
        if not root or len(root) < 2:
            continue
        for form in {root, os.path.realpath(root)}:
            if len(form) > 1:
                roots.setdefault(form.rstrip("/\\"), placeholder)
    return sorted(roots.items(), key=lambda item: -len(item[0]))


def _unrooted(text: str, roots: Sequence[Tuple[str, str]]) -> str:
    """``text`` with each root that starts a path replaced: the root must begin the
    path (not follow a word, ``/`` or ``.``) and end at a separator or the end of
    the path, so ``gs://b/opt/x`` and ``/optional`` keep their ``/opt``."""
    import re

    for root, placeholder in roots:
        pattern = r"(?<![\w/.])" + re.escape(root) + r"(?=[/\\]|$|[\s'\"(),;])"
        text = re.sub(pattern, lambda _m, p=placeholder: p, text)
    return text


def _absolute_paths() -> Any:
    """A path under one of this machine's top-level directories (``/Users/...``,
    ``/home/...``, ``/builds/...``), or a Windows drive path."""
    import re

    try:
        tops = sorted(e for e in os.listdir(os.sep) if e and not e.startswith("."))
    except OSError:
        tops = []
    alternatives = [rf"/{re.escape(t)}(?:/[^\s'\"(),;]*|(?![\w.-]))" for t in tops]
    alternatives.append(r"\b[A-Za-z]:\\[^\s'\"(),;]*")
    return re.compile(r"(?<![\w/.])(?:" + "|".join(alternatives) + r")")


def _path_scrubber(contract_path: Optional[str]) -> Any:
    """A function that takes every filesystem path out of a capability's detail and fix.
    A contract rule's message can name one (a dbt project resolved from the contract's
    directory): known roots become placeholders, any other absolute path ``<path>``."""
    roots = _path_roots(contract_path)
    pattern = _absolute_paths()

    def scrub(cap: Capability) -> Capability:
        detail = pattern.sub("<path>", _unrooted(cap.detail, roots))
        fix = pattern.sub("<path>", _unrooted(cap.fix, roots))
        return Capability(cap.cls, cap.area, cap.target, cap.subject, detail, fix)

    return scrub


def _text(value: Any) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


# ---------------------------------------------------------------------------
# What the operation needs
# ---------------------------------------------------------------------------


def _clouds(contract: Mapping[str, Any]) -> List[str]:
    from fluid_build.iac.provider_match import detect_cloud_declarations

    try:
        return sorted({cloud for cloud, _where in detect_cloud_declarations(contract)})
    except Exception:  # noqa: BLE001 - an unreadable contract declares nothing
        return []


def _runs_builds(contract: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The builds forge runs on this machine. A reference build's transformation lives
    elsewhere; a Databricks job, a Flink job and other externally run builds run on
    their platform, which ``apply`` provisions but does not run here."""
    from fluid_build.util.contract import is_databricks_job_build, is_externally_run_build

    builds = contract.get("builds") if isinstance(contract, Mapping) else None
    reference = {"hybrid-reference", "reference", "external-reference"}
    return [
        b
        for b in (builds if isinstance(builds, list) else [])
        if isinstance(b, Mapping)
        and b.get("pattern") not in reference
        and not is_databricks_job_build(b)
        and not is_externally_run_build(b)
    ]


def _dbt_adapters(build: Mapping[str, Any], clouds: Sequence[str]) -> List[str]:
    """The dbt adapter distribution a dbt build loads: the engine's suffix
    (``dbt-bigquery``) or its runtime platform, as the dbt runner reads them, else the
    adapters of the clouds the contract binds."""
    from fluid_build.build_runners.dbt.runner import _infer_dbt_adapter

    adapter = _infer_dbt_adapter(dict(build))
    if adapter:
        return [f"dbt-{adapter}"]
    return sorted({_DBT_ADAPTERS[c] for c in clouds if c in _DBT_ADAPTERS})


def _needs(contract: Mapping[str, Any], operation: str, clouds: Sequence[str]) -> _Needs:
    """What ``operation`` needs on this machine. ``validate``, ``generate`` and the
    offline ``plan`` read the contract and the bundled schemas only: no cloud, no
    credential, no tool. ``apply`` and ``ship`` change the clouds the contract binds."""
    needs = _Needs()
    if operation not in _LIVE_OPERATIONS:
        return needs
    from fluid_build.iac.cutover import default_engine

    needs.clouds = [c for c in clouds if c != "local"]
    needs.extras = sorted({_CLOUD_EXTRAS[c] for c in clouds if c in _CLOUD_EXTRAS})
    tools = set()
    if any(default_engine(c) == "opentofu" for c in needs.clouds):
        tools.add("tofu")
    from fluid_build.build_runners.base import is_dbt_build

    builds = _runs_builds(contract)
    engines = {str(b.get("engine") or "").strip().lower() for b in builds}
    dbt_builds = [b for b in builds if is_dbt_build(dict(b))]
    if dbt_builds:
        tools.add("dbt")
        needs.adapters = sorted({a for b in dbt_builds for a in _dbt_adapters(b, clouds)})
    # A Spark build on a cloud is submitted there (EMR, Databricks, Dataproc); only a
    # local one needs spark-submit here.
    if "spark" in engines and not needs.clouds:
        tools.add("spark-submit")
    needs.tools = sorted(tools)
    return needs


# ---------------------------------------------------------------------------
# Schema revision
# ---------------------------------------------------------------------------


def _schema_revision(contract: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Capability]]:
    """The schema the contract resolves to, from the bundled schemas only. A version
    that is not bundled is reported, never fetched."""
    from fluid_build.schema_manager import FluidSchemaManager

    declared = _text(contract.get("fluidVersion") or "") if isinstance(contract, Mapping) else ""
    latest = FluidSchemaManager.latest_bundled_version()
    resolved = declared or latest
    bundled = resolved in FluidSchemaManager.BUNDLED_VERSIONS
    revision: Dict[str, Any] = {
        "declared": declared or None,
        "resolved": resolved if bundled else None,
        "source": "bundled" if bundled else "missing",
        "preview": resolved in FluidSchemaManager.PREVIEW_VERSIONS,
        "latest_stable": latest,
        "sha256": None,
    }
    caps: List[Capability] = []
    if bundled:
        path = FluidSchemaManager.latest_schema_path().with_name(f"fluid-schema-{resolved}.json")
        try:
            revision["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            revision["source"] = "missing"
            revision["resolved"] = None
    if revision["source"] == "missing":
        caps.append(
            Capability(
                UNSUPPORTED,
                "schema",
                "forge",
                f"fluidVersion {declared or resolved}",
                f"this Forge build bundles no schema {resolved}, and an offline preflight "
                "fetches none",
                "Install a Forge release that bundles it, or declare a bundled fluidVersion.",
            )
        )
    return revision, caps


# ---------------------------------------------------------------------------
# Contract rules (what validate and generate iac refuse)
# ---------------------------------------------------------------------------


def _contract_rules(
    contract: Mapping[str, Any],
    env: Optional[str],
    contract_path: Optional[str],
    logger: logging.Logger,
    overlay_warnings: Sequence[str] = (),
) -> Tuple[Dict[str, Any], List[Capability]]:
    """``fluid validate``'s schema check and contract rules, offline, as capabilities.

    An error is ``unsupported``: ``validate`` fails and ``generate iac`` refuses. A
    warning from a binding check is ``lossy``. Column-type findings are left to
    :func:`_column_capabilities`, which judges each column.
    """
    import tempfile
    from pathlib import Path

    from fluid_build.cli import validate as _validate
    from fluid_build.iac.providers.aws import validate_aws_column_types
    from fluid_build.iac.providers.gcp import validate_bigquery_column_types

    # An empty schema cache: the bundled schemas only, as the report says, and nothing
    # written under the home directory.
    with tempfile.TemporaryDirectory(prefix="fluid-preflight-") as cache:
        manager = _validate.FluidSchemaManager(cache_dir=Path(cache), logger=logger)
        try:
            result = manager.validate_contract(dict(contract), offline_only=True)
        except Exception as exc:  # noqa: BLE001 - an unreadable fluidVersion, say
            from fluid_build.schema_manager import ValidationResult

            result = ValidationResult(is_valid=False)
            result.add_error(
                f"the schema check could not run ({type(exc).__name__}: {exc})",
                event="validation_error",
            )
    for warning in overlay_warnings:
        result.add_warning(warning, event="contract_overlay")
    args = SimpleNamespace(
        quiet=True, verbose=False, offline=True, strict=False, contract=contract_path, env=env
    )
    # What ``fluid validate`` runs after the schema: the contract rules, then the
    # installed extension and role validators.
    for name, call in (
        (
            "the contract rules",
            lambda: _validate._run_contract_rules(
                dict(contract), result.schema_version, result, args, logger
            ),
        ),
        (
            "the extension validators",
            lambda: _validate._run_extension_validators(dict(contract), result, logger),
        ),
        (
            "the role validators",
            lambda: _validate._run_role_validators(dict(contract), result, logger),
        ),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            result.add_error(
                f"{name} could not run ({type(exc).__name__})", event="contract_check_failed"
            )
    per_column = set()
    for check in (validate_aws_column_types, validate_bigquery_column_types):
        try:
            errors, warnings = check(contract)
        except Exception:  # noqa: BLE001 - the rules above report it
            continue
        per_column.update(errors)
        per_column.update(warnings)
    caps: List[Capability] = []
    for message in result.errors:
        if message in per_column:
            continue
        event, pointer = _meta(result, message)
        if event == "schema_not_available":
            continue  # :func:`_schema_revision` reports it, with what to do
        caps.append(
            Capability(UNSUPPORTED, _area(event), "contract", pointer or "/", _text(message))
        )
    counted_warnings = 0
    for message in result.warnings:
        counted_warnings += 1
        if message in per_column:
            continue
        event, pointer = _meta(result, message)
        if event in _LIVE_EVENTS:
            caps.append(Capability(LIVE, _area(event), "contract", pointer or "/", _text(message)))
        elif event in _LOSSY_EVENTS:
            caps.append(Capability(LOSSY, _area(event), "contract", pointer or "/", _text(message)))
    validation = {
        "valid": bool(result.is_valid),
        "schema_version": str(result.schema_version) if result.schema_version else None,
        "errors": len(result.errors),
        "warnings": counted_warnings,
    }
    return validation, caps


def _meta(result: Any, message: str) -> Tuple[str, str]:
    event, pointer, _fix = (getattr(result, "issue_meta", {}) or {}).get(message, ("", "", ""))
    return event or "", pointer or ""


def _area(event: str) -> str:
    """``contract_gcp_binding`` -> ``gcp-binding``; schema findings are ``schema``."""
    name = event[len("contract_") :] if event.startswith("contract_") else event
    if (
        not name
        or name.startswith("schema")
        or name in ("validation_error", "invalid_schema_version")
    ):
        return "schema"
    return name.replace("_", "-")


def _column_capabilities(contract: Mapping[str, Any]) -> List[Capability]:
    """Each column of each BigQuery and Glue table, classed by the emitter's type table."""
    from fluid_build.iac.providers.aws import glue_column_outcome, glue_tables
    from fluid_build.iac.providers.gcp import bigquery_column_outcome, bigquery_tables

    caps: List[Capability] = []
    for target, tables, outcome_of in (
        ("bigquery", bigquery_tables, bigquery_column_outcome),
        ("glue", glue_tables, glue_column_outcome),
    ):
        try:
            found = list(tables(contract))
        except Exception:  # noqa: BLE001 - the contract rules report an unreadable one
            continue
        for expose, columns in found:
            for column in columns:
                if isinstance(column, Mapping):
                    caps.append(_column_capability(target, expose, column, outcome_of))
    return caps


def _column_capability(
    target: str, expose: str, column: Mapping[str, Any], outcome_of: Any
) -> Capability:
    from fluid_build.iac.column_types import EXACT as _EXACT
    from fluid_build.iac.column_types import LOSSY as _LOSSY

    declared = _text(column.get("type") if column.get("type") is not None else "string")
    subject = f"exposes[{expose}].{_text(column.get('name'))}"
    try:
        outcome = outcome_of(column)
    except Exception as exc:  # noqa: BLE001 - one bad column, not the report
        why = f"which could not be read ({type(exc).__name__})"
        return Capability(
            UNSUPPORTED, "column-type", target, subject, f"declared {declared!r}, {why}"
        )
    if outcome.status == _EXACT:
        return Capability(EXACT, "column-type", target, subject, f"{declared} -> {outcome.target}")
    detail = f"declared {declared!r}, {outcome.why}"
    if outcome.status == _LOSSY:
        detail += f"; written as {outcome.target!r}"
    cls = LOSSY if outcome.status == _LOSSY else UNSUPPORTED
    return Capability(cls, "column-type", target, subject, detail, outcome.fix)


# ---------------------------------------------------------------------------
# Extras, plugins, tools, adapters
# ---------------------------------------------------------------------------


def _extras(probe: Probe, needed: Sequence[str]) -> List[Dict[str, Any]]:
    """Every extra this Forge build declares, its pins and what is installed against
    them, read from the installed metadata (``Requires-Dist`` with ``extra`` markers)."""
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement

    lines = probe.requires(DIST)
    if lines is None:
        return [
            {"name": name, "needed": True, "status": "unknown", "requirements": []}
            for name in needed
        ]
    by_extra: Dict[str, List[Dict[str, Any]]] = {}
    includes: Dict[str, set] = {}
    base_env: Dict[str, str] = dict(default_environment())
    for line in lines:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if req.marker is None or "extra" not in str(req.marker):
            continue
        for extra in _marker_extras(str(req.marker)):
            try:
                if not req.marker.evaluate({**base_env, "extra": extra}):
                    continue
            except Exception:  # noqa: BLE001 - an unevaluable marker pins nothing here
                continue
            if _canonical(req.name) == _canonical(DIST):
                # ``all = ["data-product-forge[aws,gcp]"]``: the extras it names.
                includes.setdefault(extra, set()).update(req.extras)
                by_extra.setdefault(extra, [])
                continue
            installed = probe.dist_version(req.name)
            by_extra.setdefault(extra, []).append(
                {
                    "name": req.name,
                    "specifier": str(req.specifier),
                    "installed": installed,
                    "satisfied": _satisfies(req.specifier, installed),
                }
            )
    for name in needed:
        by_extra.setdefault(name, [])
    rows = []
    for name in sorted(by_extra):
        merged = {r["name"]: r for r in _with_included(name, by_extra, includes, set())}
        reqs = sorted(merged.values(), key=lambda r: r["name"])
        ok = [r for r in reqs if r["satisfied"]]
        if not reqs:
            status = "unknown"
        elif len(ok) == len(reqs):
            status = "installed"
        elif ok:
            status = "partial"
        else:
            status = "missing"
        rows.append(
            {"name": name, "needed": name in needed, "status": status, "requirements": reqs}
        )
    return rows


def _canonical(name: str) -> str:
    from packaging.utils import canonicalize_name

    return str(canonicalize_name(name))


def _with_included(
    extra: str,
    by_extra: Mapping[str, List[Dict[str, Any]]],
    includes: Mapping[str, set],
    seen: set,
) -> List[Dict[str, Any]]:
    """An extra's requirements, with those of the extras it includes."""
    if extra in seen:
        return []
    seen.add(extra)
    rows = list(by_extra.get(extra, []))
    for included in sorted(includes.get(extra, ())):
        rows += _with_included(included, by_extra, includes, seen)
    return rows


def _marker_extras(marker: str) -> List[str]:
    """The extra names a marker string tests (``extra == "gcp"``)."""
    import re

    return sorted(set(re.findall(r"extra\s*==\s*['\"]([A-Za-z0-9._-]+)['\"]", marker)))


def _satisfies(specifier: Any, installed: Optional[str]) -> bool:
    if installed is None:
        return False
    try:
        return bool(specifier.contains(installed, prereleases=True)) if str(specifier) else True
    except Exception:  # noqa: BLE001 - an unparsable installed version
        return False


def _extra_capabilities(extras: Sequence[Mapping[str, Any]]) -> List[Capability]:
    caps = []
    for extra in extras:
        if not extra["needed"]:
            continue
        name = extra["name"]
        if extra["status"] == "installed":
            caps.append(
                Capability(EXACT, "extra", "forge", name, "installed at the pinned versions")
            )
            continue
        gaps = [
            f"{r['name']} {r['installed'] or 'not installed'} (needs {r['specifier'] or 'any'})"
            for r in extra["requirements"]
            if not r["satisfied"]
        ]
        detail = "; ".join(gaps) if gaps else "its requirements could not be read"
        caps.append(
            Capability(
                UNSUPPORTED,
                "extra",
                "forge",
                name,
                detail,
                f"pip install 'data-product-forge[{name}]'",
            )
        )
    return caps


def _plugins(probe: Probe) -> List[Dict[str, str]]:
    """Plugins installed through the entry-point groups the CLI loads: names and
    versions from metadata, nothing imported."""
    from fluid_build.plugin_manager import EXTRA_GROUPS, ROLE_GROUPS

    rows = []
    for group in sorted(set(ROLE_GROUPS.values()) | set(EXTRA_GROUPS.values())):
        for name, dist, version in probe.entry_points(group):
            rows.append({"group": group, "name": name, "dist": dist, "version": version})
    return sorted(rows, key=lambda r: (r["group"], r["name"], r["dist"]))


def _tofu_requirement() -> str:
    from fluid_build.iac.versions import REQUIRED_TOFU_VERSION

    return REQUIRED_TOFU_VERSION.replace(" ", "")


def _tools(probe: Probe, needed: Sequence[str]) -> List[Dict[str, Any]]:
    """The external tools the operation runs. Only their presence and version: never
    their path."""
    rows = []

    def row(name: str, found: bool, version: Optional[str], flavor: Optional[str], req: str):
        if name in needed or found:
            satisfied = found and (not req or version is None or _satisfies_text(req, version))
            rows.append(
                {
                    "name": name,
                    "needed": name in needed,
                    "found": found,
                    "version": version,
                    "flavor": flavor,
                    "requirement": req,
                    "satisfied": satisfied,
                }
            )

    found, version = probe.tofu()
    row("tofu", found, version, None, _tofu_requirement())
    found, flavor, version = probe.dbt(detect="dbt" in needed)
    row("dbt", found, version, flavor, "")
    row("spark-submit", probe.which("spark-submit"), None, None, "")
    return rows


def _satisfies_text(requirement: str, version: str) -> bool:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    try:
        return _satisfies(SpecifierSet(requirement), version)
    except InvalidSpecifier:
        return True


def _tool_capabilities(tools: Sequence[Mapping[str, Any]]) -> List[Capability]:
    caps = []
    fixes = {
        "tofu": "Install OpenTofu (https://opentofu.org/docs/intro/install/), or pass "
        "--ensure-opentofu to apply.",
        "dbt": "Install dbt (dbt-core and the platform's adapter, or dbt Fusion), or set "
        "DBT_EXECUTABLE.",
        "spark-submit": "Install Apache Spark and put spark-submit on PATH.",
    }
    for tool in tools:
        if not tool["needed"]:
            continue
        name = tool["name"]
        if not tool["found"]:
            caps.append(
                Capability(UNSUPPORTED, "tool", name, name, "not found", fixes.get(name, ""))
            )
        elif not tool["satisfied"]:
            caps.append(
                Capability(
                    UNSUPPORTED,
                    "tool",
                    name,
                    name,
                    f"{tool['version']} does not satisfy {tool['requirement']}",
                    fixes.get(name, ""),
                )
            )
        else:
            caps.append(Capability(EXACT, "tool", name, name, f"{tool['version'] or 'found'}"))
    return caps


def _adapters(
    probe: Probe, needs: _Needs, clouds: Sequence[str], tools: Sequence[Mapping[str, Any]] = ()
) -> Dict[str, Any]:
    """dbt and its adapters (installed version against the extra's pin), and the
    OpenTofu provider pins of the clouds the operation changes."""
    from fluid_build.iac.versions import PROVIDER_PINS

    dbt = []
    # dbt Fusion is one binary with its adapters built in: no Python distribution to check.
    fusion = any(t.get("name") == "dbt" and t.get("flavor") == "fusion" for t in tools)
    needs = _Needs(needs.clouds, needs.extras, needs.tools, [] if fusion else needs.adapters)
    if fusion:
        needs.tools = [t for t in needs.tools if t != "dbt"]
    names = sorted({"dbt-core", *needs.adapters})
    for name in names:
        installed = probe.dist_version(name)
        if (
            installed is None
            and name not in needs.adapters
            and not (name == "dbt-core" and "dbt" in needs.tools)
        ):
            continue
        dbt.append(
            {
                "name": name,
                "needed": name in needs.adapters or (name == "dbt-core" and "dbt" in needs.tools),
                "installed": installed,
            }
        )
    providers = []
    for cloud in needs.clouds:
        plugin = _iac_plugin(cloud)
        if plugin is None:
            continue
        for local_name, pin in sorted((getattr(plugin, "required_providers", {}) or {}).items()):
            pinned = PROVIDER_PINS.get(local_name, pin)
            providers.append(
                {
                    "cloud": cloud,
                    "source": _text(pinned.get("source")),
                    "pin": _text(pinned.get("version")),
                }
            )
    return {"dbt": dbt, "opentofu_providers": providers}


def _adapter_capabilities(adapters: Mapping[str, Any]) -> List[Capability]:
    caps = []
    for row in adapters["dbt"]:
        if not row["needed"]:
            continue
        if row["installed"] is None:
            caps.append(
                Capability(
                    UNSUPPORTED,
                    "adapter",
                    "dbt",
                    row["name"],
                    "not installed",
                    f"pip install {row['name']}",
                )
            )
        else:
            caps.append(Capability(EXACT, "adapter", "dbt", row["name"], row["installed"]))
    for row in adapters["opentofu_providers"]:
        caps.append(
            Capability(
                LIVE,
                "opentofu-provider",
                row["cloud"],
                row["source"],
                f"pinned {row['pin']}; tofu init downloads it unless a provider mirror "
                "holds it, and an offline preflight does not",
                "Run tofu init once with network, or configure a filesystem_mirror.",
            )
        )
    return caps


def _iac_plugin(cloud: str) -> Any:
    try:
        from fluid_build.iac.registry import get_iac_plugin

        return get_iac_plugin(cloud)
    except Exception:  # noqa: BLE001 - a plugin that cannot load is reported elsewhere
        return None


# ---------------------------------------------------------------------------
# Credentials and state backend
# ---------------------------------------------------------------------------


def _state_backend(
    probe: Probe,
    contract: Mapping[str, Any],
    operation: str,
    clouds: Sequence[str],
    flag: Optional[str],
    provider: Optional[str],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Where ``apply`` keeps OpenTofu state, resolved as ``apply`` resolves it
    (``--state-backend``, then ``FLUID_STATE_BACKEND``, then local): ``(row, spec)``. The
    row names the scheme and where it was set, never the bucket or key."""
    from fluid_build.iac.backend import (
        STATE_BACKEND_ENV,
        parse_backend,
        resolve_state_backend_spec,
    )

    spec, origin = resolve_state_backend_spec(flag, probe.environ())
    row: Dict[str, Any] = {
        "needed": operation in _LIVE_OPERATIONS and bool([c for c in clouds if c != "local"]),
        "origin": origin,
        "scheme": "local",
        "valid": True,
    }
    if spec:
        try:
            block = parse_backend(
                spec,
                contract,
                per_contract_default=origin == STATE_BACKEND_ENV,
                provider=provider or (clouds[0] if clouds else None),
            )
        except ValueError:
            row.update(scheme="unknown", valid=False)
        else:
            row["scheme"] = next(iter(block)) if block else "local"
    return row, spec


def _live_capabilities(
    probe: Probe, operation: str, clouds: Sequence[str], state_backend: Mapping[str, Any]
) -> List[Capability]:
    """What only the live cloud can prove: its credentials and the state backend.
    Listed for the operations that use them, and only those."""
    caps = []
    environ = probe.environ()
    for cloud in clouds:
        plugin = _iac_plugin(cloud)
        names = [n for n in getattr(plugin, "credential_env_vars", ()) if environ.get(n)]
        where = f"; set here: {', '.join(sorted(names))}" if names else ""
        caps.append(
            Capability(
                LIVE,
                "identity",
                cloud,
                f"{cloud} identity",
                f"{operation} calls {cloud} as the identity its provider resolves"
                f"{where}. An offline preflight cannot prove it",
                (
                    "Run fluid doctor --contract ... --live for a read-only check."
                    if cloud in _LIVE_IDENTITY
                    else f"--live has no read-only {cloud} check yet; prove it with a "
                    f"{operation} against a test target."
                ),
            )
        )
    if state_backend.get("needed"):
        if not state_backend.get("valid", True):
            caps.append(
                Capability(
                    UNSUPPORTED,
                    "state-backend",
                    "opentofu",
                    state_backend["origin"],
                    "the state backend spec does not parse",
                    "Use s3://<bucket>/<key> or gcs://<bucket>/<prefix>.",
                )
            )
        elif state_backend["scheme"] == "local":
            caps.append(
                Capability(
                    LOSSY,
                    "state-backend",
                    "opentofu",
                    "local",
                    f"{operation} keeps OpenTofu state on this machine, so another runner "
                    "cannot see it",
                    "Pass --state-backend (or set FLUID_STATE_BACKEND) to s3:// or gcs://, "
                    "as apply takes it.",
                )
            )
        else:
            caps.append(
                Capability(
                    LIVE,
                    "state-backend",
                    "opentofu",
                    state_backend["scheme"],
                    "the bucket's existence and access are only provable live",
                    "Run with --live for a read-only check.",
                )
            )
    return caps


def run_live_checks(
    clouds: Sequence[str], state_backend: Mapping[str, Any], spec: Optional[str]
) -> List[Dict[str, str]]:
    """Read-only live checks. Each says ok, failed or skipped, and why, never the
    identity or bucket it found."""
    rows = []
    for cloud in clouds:
        if cloud == "aws":
            rows.append(_check("aws-identity", _aws_identity))
        elif cloud == "gcp":
            rows.append(_check("gcp-identity", _gcp_identity))
        else:
            rows.append(
                {
                    "check": f"{cloud}-identity",
                    "status": "skipped",
                    "detail": "no read-only identity check for this cloud yet",
                }
            )
    if state_backend.get("needed") and state_backend.get("scheme") in ("s3", "gcs"):
        rows.append(_check("state-backend", lambda: _backend_reachable(spec or "")))
    return rows


def _apply_live_checks(
    capabilities: List[Capability], checks: Sequence[Mapping[str, str]]
) -> List[Capability]:
    """A live check that answered turns its ``requires-live-proof`` capability into
    ``exact``; one that failed makes it ``unsupported``; a skipped one leaves it."""
    by_name = {c["check"]: c for c in checks}
    out = []
    for cap in capabilities:
        name = f"{cap.target}-identity" if cap.area == "identity" else cap.area
        check = by_name.get(name) if cap.cls == LIVE else None
        if check and check["status"] == "ok":
            cap = Capability(EXACT, cap.area, cap.target, cap.subject, f"live: {check['detail']}")
        elif check and check["status"] == "failed":
            cap = Capability(
                UNSUPPORTED,
                cap.area,
                cap.target,
                cap.subject,
                f"live check failed ({check['detail']})",
                "Check what this runner resolves: its credentials, or the bucket.",
            )
        out.append(cap)
    return out


def _check(name: str, fn: Any) -> Dict[str, str]:
    try:
        detail = fn()
    except ModuleNotFoundError as exc:
        return {
            "check": name,
            "status": "skipped",
            "detail": f"{exc.name or 'its SDK'} is not installed",
        }
    except Exception as exc:  # noqa: BLE001 - the failure is the result
        return {"check": name, "status": "failed", "detail": _failure(exc)}
    return {"check": name, "status": "ok", "detail": detail}


def _failure(exc: BaseException) -> str:
    """The exception's class and, for a cloud API error, its status code (``403``,
    ``NoSuchBucket``): never its message, which can name the identity or the bucket.

    botocore's ``ClientError.response`` is a dict; google-api-core's
    ``GoogleAPICallError.response`` is a ``requests.Response`` and its HTTP status is
    ``.code``. Anything else names the class alone."""
    code: Any = None
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
    if code is None:
        status = getattr(exc, "code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            code = int(status)
    if code is None and isinstance(exc, LookupError):
        code = "not found"
    return f"{type(exc).__name__} {_text(code)}".strip() if code else type(exc).__name__


#: A live check answers within this, or fails: a runner with no route out must not
#: hang on SDK retries.
_LIVE_TIMEOUT_S = 10


def _aws_client(service: str) -> Any:
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=_LIVE_TIMEOUT_S,
        read_timeout=_LIVE_TIMEOUT_S,
        # One attempt in every retry mode: legacy mode reads ``max_attempts`` as
        # retries after the first try, so ``max_attempts: 1`` would send twice.
        retries={"total_max_attempts": 1},
    )
    return boto3.client(service, config=config)


def _aws_identity() -> str:
    _aws_client("sts").get_caller_identity()
    return "STS GetCallerIdentity answered"


def _gcp_identity() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default()
    transport = Request()

    def request(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _LIVE_TIMEOUT_S)
        return transport(*args, **kwargs)

    credentials.refresh(request)
    return "application default credentials refreshed"


def _backend_reachable(spec: str) -> str:
    from fluid_build.iac.backend import parse_backend

    block = parse_backend(spec) or {}
    if "s3" in block:
        _aws_client("s3").head_bucket(Bucket=block["s3"]["bucket"])
        return "state bucket readable"
    if "gcs" in block:
        from google.cloud import storage

        bucket = storage.Client().bucket(block["gcs"]["bucket"])
        # retry=None: the default Retry keeps trying a connection error for 120 s.
        if not bucket.exists(timeout=_LIVE_TIMEOUT_S, retry=None):
            raise LookupError("not found")
        return "state bucket readable"
    return "local state"


# ---------------------------------------------------------------------------
# Redaction and comparison
# ---------------------------------------------------------------------------


def redact_report(report: Any, *, contract_path: Optional[str] = None) -> Any:
    """``report`` through both redaction layers: the global secret redactor, and the
    Snowflake provider's twin, so a credential-shaped value in a message never reaches
    stdout or a saved report. Any string naming this machine's working, home, Python or
    temporary directory (or the contract's) names a placeholder instead."""
    from fluid_build.observability.secret_redactor import redact_value
    from fluid_build.providers.snowflake.util.logging import redact_dict, redact_list

    roots = _path_roots(contract_path)

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return _unrooted(value, roots)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    scrubbed = redact_value(_walk(report))
    if isinstance(scrubbed, dict):
        return redact_dict(scrubbed)
    if isinstance(scrubbed, list):
        return redact_list(scrubbed)
    return scrubbed


def load_report(path: str) -> Dict[str, Any]:
    """A saved report, refused unless it is a ``fluid-preflight/1`` report."""
    try:
        if os.path.getsize(path) > _MAX_REPORT_BYTES:
            raise PreflightError("a preflight report is never this large; refusing to read it")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, RecursionError) as exc:
        raise PreflightError(f"cannot read a preflight report ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("version") != REPORT_FORMAT:
        raise PreflightError(f"not a {REPORT_FORMAT} report (from fluid doctor --contract --json)")
    return data


#: The single values two reports are compared on: (area, name, where in the report,
#: how to read it, what to change).
_SCALAR_GAPS: Tuple[Tuple[str, str, Tuple[str, ...], Any, str], ...] = (
    ("request", "operation", ("operation",), None, "Compare reports of the same request."),
    ("request", "env", ("env",), None, "Compare reports of the same request."),
    ("request", "contract", ("contract", "id"), None, "Compare reports of the same contract."),
    ("forge", "version", ("forge", "version"), None, "Pin one Forge version."),
    ("forge", "python", ("forge", "python"), "minor", "Use the same Python minor version."),
    (
        "schema",
        "resolved",
        ("schema_revision", "resolved"),
        None,
        "Use a Forge build that bundles the same schema.",
    ),
    (
        "schema",
        "sha256",
        ("schema_revision", "sha256"),
        None,
        "The bundled schema files differ: pin one Forge build.",
    ),
)


def compare_reports(left: Mapping[str, Any], right: Mapping[str, Any]) -> List[Dict[str, str]]:
    """The differences between two reports that change whether the operation runs:
    versions, extras, tools, adapters, plugins, schema revision and capability
    classes. Each names what to change. Values are versions and class names only."""
    gaps: List[Dict[str, str]] = []

    def gap(area: str, name: str, a: Any, b: Any, action: str) -> None:
        if _norm(a) != _norm(b):
            row = {"area": area, "name": _text(name), "left": _norm(a), "right": _norm(b)}
            gaps.append({**row, "action": action})

    for area, name, path, read, action in _SCALAR_GAPS:
        a, b = _at(left, path), _at(right, path)
        if read == "minor":
            a, b = _minor(a), _minor(b)
        gap(area, name, a, b, action)
    for name, a, b in _needed(_pair(left.get("extras"), right.get("extras"), "name")):
        gap(
            "extra",
            name,
            _get(a, "status"),
            _get(b, "status"),
            f"Install data-product-forge[{name}] on both.",
        )
        for req, ra, rb in _pair(_get(a, "requirements"), _get(b, "requirements"), "name"):
            spec = _get(ra or rb, "specifier") or "any"
            gap(
                "requirement",
                f"{name}:{req}",
                _get(ra, "installed"),
                _get(rb, "installed"),
                f"Install the same {req} version on both (this build pins {spec}).",
            )
    for name, a, b in _needed(_pair(left.get("tools"), right.get("tools"), "name")):
        need = _get(a or b, "requirement")
        tail = f" (this build needs {need})." if need else "."
        gap(
            "tool",
            name,
            _tool_state(a),
            _tool_state(b),
            f"Install the same {name} version on both{tail}",
        )
    dbt = _pair(_at(left, ("adapters", "dbt")), _at(right, ("adapters", "dbt")), "name")
    for name, a, b in _needed(dbt):
        gap(
            "adapter",
            name,
            _get(a, "installed"),
            _get(b, "installed"),
            f"Install the same {name} version on both.",
        )
    for name, a, b in _pair(left.get("plugins"), right.get("plugins"), "name", "group"):
        action = "Install the same plugin distribution and version on both."
        gap("plugin", name, _plugin_state(a), _plugin_state(b), action)
    for name, a, b in _pair(
        left.get("capabilities"), right.get("capabilities"), "subject", "area", "target"
    ):
        action = "Resolve the version gaps above; the class follows the installed versions."
        gap("capability", name, _get(a, "class"), _get(b, "class"), action)
    redacted: List[Dict[str, str]] = redact_report(gaps)
    return redacted


def _needed(rows: Sequence[Tuple[str, Any, Any]]) -> List[Tuple[str, Any, Any]]:
    """Rows the operation needs on either side: an extra or tool neither run needs is
    not a gap worth a CI failure (a laptop with every extra against a lean CI image)."""
    return [(n, a, b) for n, a, b in rows if _get(a, "needed") or _get(b, "needed")]


def _at(report: Any, path: Sequence[str]) -> Any:
    for key in path:
        report = report.get(key) if isinstance(report, Mapping) else None
    return report


def _get(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, Mapping) else None


def _norm(value: Any) -> str:
    if value is None or value == "":
        return "absent"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _text(value)


def _minor(version: Any) -> Optional[str]:
    parts = str(version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else (version or None)


def _tool_state(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not row or not row.get("found"):
        return None
    return str(row.get("version") or "found")


def _plugin_state(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not row:
        return None
    return f"{row.get('dist') or '?'} {row.get('version') or ''}".strip()


def _pair(
    left: Any, right: Any, *keys: str
) -> List[Tuple[str, Optional[Mapping[str, Any]], Optional[Mapping[str, Any]]]]:
    """Rows of two lists matched on ``keys``; the name is the keys joined."""

    def index(rows: Any) -> Dict[str, Mapping[str, Any]]:
        out: Dict[str, Mapping[str, Any]] = {}
        seen: Dict[str, int] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping):
                key = ":".join(_text(row.get(k)) for k in reversed(keys))
                # Rows sharing a key (several schema errors at one pointer) are told
                # apart by their order, so one more on either side is a gap.
                seen[key] = seen.get(key, 0) + 1
                out[key if seen[key] == 1 else f"{key}#{seen[key]}"] = row
        return out

    a, b = index(left), index(right)
    return [(name, a.get(name), b.get(name)) for name in sorted(set(a) | set(b))]


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------


def render_text(report: Mapping[str, Any]) -> List[str]:
    """The report for a terminal: what blocks first, exact items counted, not listed."""
    forge = report["forge"]
    rev = report["schema_revision"]
    summary = report["summary"]
    lines = [
        f"fluid doctor --contract: {report['operation']} "
        f"({report['mode']}{', env ' + report['env'] if report.get('env') else ''})",
        f"  forge {forge['version']} · python {forge['python']} · {forge['platform']}",
        f"  schema {rev['resolved'] or rev['declared'] or '?'} ({rev['source']}"
        f"{', preview' if rev['preview'] else ''})",
    ]
    needs = report["needs"]
    if needs["clouds"] or needs["tools"] or needs["extras"]:
        lines.append(
            "  needs: "
            + "; ".join(
                f"{label} {', '.join(values)}"
                for label, values in (
                    ("clouds", needs["clouds"]),
                    ("extras", needs["extras"]),
                    ("tools", needs["tools"]),
                )
                if values
            )
        )
    else:
        lines.append("  needs: no cloud, credential or external tool")
    labels = {UNSUPPORTED: "✗ unsupported", LOSSY: "! lossy", LIVE: "? requires live proof"}
    for cls in (UNSUPPORTED, LOSSY, LIVE):
        rows = [c for c in report["capabilities"] if c["class"] == cls]
        if not rows:
            continue
        lines.append(f"{labels[cls]} ({len(rows)})")
        for cap in rows:
            lines.append(f"  [{cap['area']}] {cap['target']} {cap['subject']}: {cap['detail']}")
            if cap["fix"]:
                lines.append(f"      fix: {cap['fix']}")
    for check in report.get("live_checks") or []:
        lines.append(f"  live {check['check']}: {check['status']} ({check['detail']})")
    if report.get("mode") == "live" and not report.get("live_checks"):
        lines.append(
            f"  live: no live check applies to {report['operation']}, which needs no cloud; "
            "use --operation apply or ship"
        )
    counts = summary["counts"]
    lines.append(
        f"{summary['verdict']}: exact {counts[EXACT]} · lossy {counts[LOSSY]} · "
        f"unsupported {counts[UNSUPPORTED]} · requires live proof {counts[LIVE]}"
    )
    return lines


def render_gaps(gaps: Sequence[Mapping[str, str]]) -> List[str]:
    if not gaps:
        return ["No differences: both reports resolve the same versions and capabilities."]
    lines = [f"{len(gaps)} difference(s) (left vs right):"]
    for row in gaps:
        lines.append(f"  [{row['area']}] {row['name']}: {row['left']} vs {row['right']}")
        lines.append(f"      {row['action']}")
    return lines


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fail(reason: str, exit_code: int = 2) -> Exception:
    from ._common import CLIError

    return CLIError(exit_code, "doctor_preflight_failed", {"reason": reason})


def _run_compare(paths: Sequence[str], as_json: bool) -> int:
    from fluid_build.cli.console import cprint_literal

    from .security import validate_input_file

    try:
        left, right = (
            load_report(str(validate_input_file(path, "preflight report"))) for path in paths
        )
    except PreflightError as exc:
        raise _fail(str(exc)) from None
    gaps = compare_reports(left, right)
    if as_json:
        sys.stdout.write(dumps({"version": "fluid-preflight-compare/1", "differences": gaps}))
    else:
        for line in render_gaps(gaps):
            cprint_literal(line)
    return 1 if gaps else 0


def _load_contract(
    path: str, env: Optional[str], logger: logging.Logger
) -> Tuple[Any, List[str], str]:
    """``(contract, overlay warnings, the path its files are anchored at)``, loaded as
    ``fluid validate`` loads it. A bundle carries its resolved contract; files it names
    are anchored at the source contract it records, as validate and apply anchor them."""
    from fluid_build._contract_loader import _is_bundle_path, source_contract_path

    from ._common import load_contract_with_overlay

    if _is_bundle_path(path):
        source = source_contract_path(path)
        return load_contract_with_overlay(path, env, logger), [], str(source) if source else path
    from .validate import _load_with_overlay_warnings

    contract, warnings, _locate, _origins = _load_with_overlay_warnings(path, env, logger)
    return contract, list(warnings), path


def run_cli(args: Any, logger: logging.Logger) -> int:
    """``fluid doctor --contract`` and ``fluid doctor --compare``."""
    from fluid_build.cli.console import cprint_literal

    as_json = bool(getattr(args, "json", False))
    compare = getattr(args, "compare", None)
    if compare:
        return _run_compare(compare, as_json)
    env = getattr(args, "env", None)
    if env is True:
        raise _fail("--env needs an overlay name with --contract (e.g. --env prod)")
    if env is False:
        env = None
    from .security import validate_cli_path

    path = str(validate_cli_path(args.contract, mode="read", file_type="contract"))
    try:
        contract, overlay_warnings, rules_path = _load_contract(path, env, logger)
    except (RuntimeError, ValueError, OSError, UnicodeError) as exc:
        # The class only: the loader's message names the file's path.
        raise _fail(f"cannot load the contract ({type(exc).__name__})", 1) from None
    try:
        report = build_report(
            contract if isinstance(contract, Mapping) else {},
            operation=getattr(args, "operation", None) or DEFAULT_OPERATION,
            env=env,
            contract_path=rules_path,
            live=bool(getattr(args, "live", False)),
            provider=getattr(args, "preflight_provider", None) or None,
            state_backend=getattr(args, "state_backend", None),
            overlay_warnings=overlay_warnings,
            logger=logger,
        )
    except PreflightError as exc:
        raise _fail(str(exc)) from None
    if as_json:
        sys.stdout.write(dumps(report))
    else:
        for line in render_text(report):
            cprint_literal(line)
    return exit_code(report, strict=getattr(args, "strict", False) is True)
