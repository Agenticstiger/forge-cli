# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Acquisition-pattern hooks into the 11-stage pipeline.

Each pipeline stage (``policy-apply``, ``verify``, ``publish``,
``schedule-sync``) calls into this module when it sees a build with
``pattern: acquisition``. Keeping the acquisition-specific logic in one
place means the four large CLI files don't need to grow per-engine
``if/elif`` chains, and a single module owns the contract → stage
mapping.

Each entry point is pure: it accepts a contract + working directory +
optional state and returns a structured result dict. The CLI is
responsible for printing / exiting / writing logs based on that result.

**Security note (Sec-Fix 9/10):** the schedule-sync stage emits Python
files (Airflow DAGs, Dagster jobs, Prefect deployments) and cron
entries that downstream schedulers execute. Contract identifiers and
the ``schedule`` cron expression flow into those artifacts via string
templates. We validate every interpolated value before render so a
malicious contract cannot inject Python code or shell into the
generated artifact, and we constrain the artifact roots so a
``contract.id`` like ``../../etc/cron.d/x`` can't escape the workdir.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# The identifier grammar + validator are hoisted into the neutral
# ``build_runners._ids`` leaf so the runtime chokepoint
# (``build_runners.base.run_builds_from_args``) can share the EXACT same
# guard without a ``build_runners`` → ``cli`` reverse edge. ``cli`` →
# ``build_runners`` is an existing allowed edge. We re-export the symbols
# under their historical names (``_validate_identifier``, ``_IDENT_RE``,
# ``IdentifierViolation``) so existing call sites and tests that import
# them from this module keep resolving the same single class identity.
from fluid_build.build_runners._ids import (
    _IDENT_RE,
    IdentifierViolation,
)
from fluid_build.build_runners._ids import (
    validate_identifier as _validate_identifier,
)

LOG = logging.getLogger("fluid.acquire.stage_ext")


# ── Cron validation (Sec-Fix 9 + 10) ─────────────────────────────────────


# ``contract.id`` and ``build.id`` show up in:
# * filesystem paths under .fluid/artifacts/<id>/ and .fluid/policies/<id>/
# * inline f-string interpolation into Airflow / Dagster / Prefect Python
# * cron entries
# They are validated via :func:`_validate_identifier` (hoisted above).
#
# Cron must be 5 (standard) or 6 (with seconds) whitespace-separated
# fields, each made of digits, ``*``, ``/``, ``,``, ``-``, ``?``, ``L``,
# ``W``, ``#``. We deliberately exclude any character that could survive
# a shell or Python-source escape.
_CRON_FIELD_RE = re.compile(r"^[0-9*/,\-?LW#]+$")


__all__ = [  # re-exported identifier symbols + this module's public surface
    "IdentifierViolation",
    "_IDENT_RE",
    "_validate_identifier",
]


def _validate_cron(value: str) -> str:
    if not isinstance(value, str):
        raise IdentifierViolation(f"cron schedule must be a string, got {type(value).__name__}")
    fields = value.strip().split()
    if len(fields) not in (5, 6):
        raise IdentifierViolation(
            f"cron schedule {value!r} must be 5 or 6 whitespace-separated fields"
        )
    for f in fields:
        if not _CRON_FIELD_RE.match(f):
            raise IdentifierViolation(
                f"cron schedule field {f!r} contains characters outside "
                "the allowed cron alphabet [0-9*/,?-LW#]"
            )
    return " ".join(fields)


def _safe_subpath(root: Path, *segments: str) -> Path:
    """Return ``root / <validated segments>`` and assert the result stays
    inside ``root`` after resolution. Defends against ``..`` and absolute
    path injection if ``segments`` ever sneak past ``_validate_identifier``.

    Note: ``resolve()`` follows symlinks. We rely on the caller never
    pre-creating attacker-controlled symlinks under ``root``; in our
    flow ``root`` is ``<workdir>/.fluid/artifacts/<id>/`` which is
    Forge-owned. If that assumption ever changes, switch to a
    realpath-of-root comparison plus a no-follow walk.
    """
    candidate = root.joinpath(*segments).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:  # pragma: no cover — defensive
        raise IdentifierViolation(
            f"path {candidate} escapes artifact root {root_resolved}"
        ) from exc
    return candidate


# ── Helpers ──────────────────────────────────────────────────────────────


def acquisition_builds(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only the builds with ``pattern: acquisition``."""
    return [b for b in contract.get("builds", []) if b.get("pattern") == "acquisition"]


def is_acquisition_contract(contract: Dict[str, Any]) -> bool:
    return any(b.get("pattern") == "acquisition" for b in contract.get("builds", []))


def latest_run_record(workdir: Path, product_id: str, build_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest run record for ``product_id/build_id`` or None."""
    runs_dir = workdir / ".fluid" / "runs" / product_id / build_id / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(runs_dir.glob("*.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ── Stage: verify ────────────────────────────────────────────────────────


@dataclass
class VerifyCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerifyResult:
    product_id: str
    build_id: str
    checks: List[VerifyCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "build_id": self.build_id,
            "all_passed": self.all_passed,
            "checks": [asdict(c) for c in self.checks],
        }


def verify_acquisition(contract: Dict[str, Any], workdir: Path) -> List[VerifyResult]:
    """Post-apply probes for each acquisition build.

    Checks (best-effort — missing data turns into a failed check, not a
    crash):

    * ``records_landed`` — last run's ``records_total`` > 0
    * ``run_state_succeeded`` — last run's state is SUCCEEDED or PARTIAL
    * ``no_unexpected_dlq_overflow`` — DLQ count <= configured ``maxRecordsBeforeAbort``
    * ``cost_within_budget`` — last run's cost facets within
      ``properties.cost.budget.monthly`` (when present)
    """
    results: List[VerifyResult] = []
    product_id = contract.get("id", "")
    for build in acquisition_builds(contract):
        bid = build.get("id", "")
        result = VerifyResult(product_id=product_id, build_id=bid)
        record = latest_run_record(workdir, product_id, bid)

        if record is None:
            result.checks.append(
                VerifyCheck(
                    name="run_record_present",
                    passed=False,
                    detail="no run record found — has the build been applied?",
                )
            )
            results.append(result)
            continue

        # Bug A4-4: runners emit lowercase state values ("succeeded", "partial")
        # via RunState.value, but this check previously required uppercase.
        # Use case-insensitive comparison so all casing variants (uppercase
        # legacy records, lowercase new records, mixed) are handled uniformly.
        state = record.get("state", "UNKNOWN")
        result.checks.append(
            VerifyCheck(
                name="run_state_succeeded",
                passed=str(state).upper() in ("SUCCEEDED", "PARTIAL"),
                detail=f"state={state}",
            )
        )

        records_total = int(record.get("records_total", 0))
        result.checks.append(
            VerifyCheck(
                name="records_landed",
                passed=records_total > 0,
                detail=f"records_total={records_total}",
            )
        )

        dlq_records = int(record.get("dlq_records", 0))
        delivery = (build.get("properties", {}).get("delivery") or {}).get("dlq", {}) or {}
        max_dlq = int(delivery.get("maxRecordsBeforeAbort", 10_000))
        result.checks.append(
            VerifyCheck(
                name="no_unexpected_dlq_overflow",
                passed=dlq_records <= max_dlq,
                detail=f"dlq_records={dlq_records} max={max_dlq}",
            )
        )

        cost_props = build.get("properties", {}).get("cost", {}) or {}
        budget = (cost_props.get("budget") or {}).get("monthly", {}) or {}
        if budget:
            facets = record.get("facets", {}) or {}
            cost_used = float(facets.get("cost_records_total", records_total))
            row_cap = budget.get("rows")
            within = row_cap is None or cost_used <= int(row_cap)
            result.checks.append(
                VerifyCheck(
                    name="cost_within_budget",
                    passed=within,
                    detail=f"records_used={cost_used} row_cap={row_cap}",
                )
            )

        results.append(result)
    return results


# ── Stage: publish ───────────────────────────────────────────────────────


@dataclass
class PublishResult:
    product_id: str
    expose_id: str
    target: str
    succeeded: bool
    urn: str = ""
    error: Optional[str] = None


def _ensure_builtin_registrars(targets: List[str]) -> None:
    """Register a built-in, env-configured registrar for any planned target
    that has none.

    Explicit registrations (a custom registrar, or a test's fake) are left
    untouched — ``get_registrar`` is checked first, so they take precedence
    over the built-ins. Targets with no environment config resolve to no
    registrar; the dispatcher then records a clear "not configured" result.
    """
    from fluid_build.build_runners import _catalog as orch
    from fluid_build.build_runners.catalog_registrars import build_registrar

    for target in targets:
        if orch.get_registrar(target) is not None:
            continue
        registrar = build_registrar(target)
        if registrar is not None:
            orch.register_registrar(target, registrar)


def publish_acquisition(contract: Dict[str, Any], workdir: Path) -> List[PublishResult]:
    """Register every acquisition build's catalog targets exactly once.

    Builds a :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
    *once per build* and threads it through every catalog target in the
    plan via :func:`register_all_payload`. That centralises the
    expensive bits (parsing the contract, rendering ODPS, rendering
    one ODCS per expose, computing lineage) so they happen exactly
    once regardless of how many catalogs the contract publishes to.

    Per-target configs are resolved from ``FluidConfig`` (env vars +
    ``catalogs.<target>`` blocks) and threaded through so backends
    declared only in ``providers/catalogs/CATALOG_PROVIDERS`` (the CLI
    ``--target`` registry) work here automatically — keeping the two
    surfaces from drifting. As a fallback for backends that haven't
    migrated to the unified config path, :func:`_ensure_builtin_registrars`
    pre-wires env-configured registrars into the legacy ``_catalog``
    dispatcher so a target declared in the contract still resolves.
    """
    from fluid_build.api.catalog_publication import CatalogPublicationPayload
    from fluid_build.build_runners import _catalog as catalog_orchestrator

    target_configs = _collect_target_configs(contract)

    results: List[PublishResult] = []
    product_id = contract.get("id", "")
    classifications = _classifications_from_run_records(contract, workdir)
    payload = CatalogPublicationPayload.from_contract(contract, classifications)

    for build in acquisition_builds(contract):
        plan = catalog_orchestrator.CatalogPlan.from_dict(
            build.get("properties", {}).get("catalog", {})
        )
        if not plan.targets:
            continue
        _ensure_builtin_registrars(plan.targets)
        outcome = catalog_orchestrator.register_all_payload(
            plan,
            payload,
            target_configs=target_configs,
        )
        # The canonical path produces one ``RegistrationResult`` per
        # target. We project back to the ``PublishResult`` shape (per
        # product+expose+target) by emitting one entry per output the
        # build declared — every entry shares the target outcome since
        # the registrar's ``register_payload`` publishes the whole
        # product atomically. ``expose_id`` is the build's declared
        # output (preserved for CLI display).
        outputs = list(build.get("outputs") or []) or [a.asset_id for a in payload.assets]
        for r in outcome.results:
            for expose_id in outputs:
                results.append(
                    PublishResult(
                        product_id=product_id,
                        expose_id=expose_id,
                        target=r.target,
                        succeeded=r.succeeded,
                        urn=r.urn,
                        error=r.error,
                    )
                )
    return results


def _collect_target_configs(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Resolve per-target catalog configs for every target named in any
    acquisition build's ``properties.catalog.register``.

    Reading via ``FluidConfig`` picks up both the YAML config blocks
    and the env-var overrides applied in ``get_catalog_config``. Best-
    effort: a failed lookup degrades to an empty dict so the registrar
    factory falls back to its defaults.
    """
    targets: List[str] = []
    for build in acquisition_builds(contract):
        for t in (build.get("properties", {}).get("catalog", {}) or {}).get("register", []) or []:
            if t not in targets:
                targets.append(t)
    if not targets:
        return {}
    try:
        from fluid_build.config_manager import FluidConfig

        cfg = FluidConfig()
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for t in targets:
        try:
            resolved = cfg.get_catalog_config(t) or {}
        except Exception:  # noqa: BLE001
            resolved = {}
        out[t] = resolved
    return out


def _classifications_from_run_records(
    contract: Dict[str, Any], workdir: Path
) -> Dict[str, List[str]]:
    """Read PII classifications from the latest run record's facets, if present."""
    classifications: Dict[str, List[str]] = {}
    pid = contract.get("id", "")
    for build in acquisition_builds(contract):
        rec = latest_run_record(workdir, pid, build.get("id", ""))
        if rec is None:
            continue
        for col, labels in (rec.get("facets", {}).get("classifications", {}) or {}).items():
            existing = classifications.setdefault(col, [])
            for lbl in labels:
                if lbl not in existing:
                    existing.append(lbl)
    return classifications


# ── Stage: schedule-sync ─────────────────────────────────────────────────


@dataclass
class ScheduleArtifact:
    product_id: str
    build_id: str
    orchestrator: str  # "airflow" | "dagster" | "prefect" | "cron"
    artifact_path: str
    cron: Optional[str] = None


def schedule_sync_acquisition(
    contract: Dict[str, Any],
    workdir: Path,
    *,
    orchestrators: Optional[List[str]] = None,
) -> List[ScheduleArtifact]:
    """Emit per-orchestrator schedule artifacts for each acquisition build.

    By default emits Airflow + cron; ``orchestrators`` overrides that.
    Artifacts land under ``.fluid/artifacts/<contract-id>/schedule/``.
    """
    chosen = orchestrators or ["airflow", "cron"]
    contract_id = _validate_identifier(contract.get("id", "unknown"), kind="contract.id")
    artifacts_parent = workdir / ".fluid" / "artifacts"
    artifacts_parent.mkdir(parents=True, exist_ok=True)
    artifacts_root = _safe_subpath(artifacts_parent, contract_id, "schedule")
    artifacts_root.mkdir(parents=True, exist_ok=True)

    out: List[ScheduleArtifact] = []
    for build in acquisition_builds(contract):
        bid = _validate_identifier(build.get("id", ""), kind="build.id")
        execution = build.get("execution", {}) or {}
        trigger = execution.get("trigger", {}) or {}
        schedule = trigger.get("schedule")
        if not schedule:
            continue
        schedule = _validate_cron(schedule)

        if "airflow" in chosen:
            dag_path = _safe_subpath(artifacts_root, f"{bid}_dag.py")
            dag_path.write_text(
                _render_airflow_dag(contract_id, bid, build, schedule), encoding="utf-8"
            )
            out.append(
                ScheduleArtifact(
                    product_id=contract_id,
                    build_id=bid,
                    orchestrator="airflow",
                    artifact_path=str(dag_path),
                    cron=schedule,
                )
            )

        if "dagster" in chosen:
            job_path = _safe_subpath(artifacts_root, f"{bid}_dagster.py")
            job_path.write_text(_render_dagster_job(contract_id, bid, schedule), encoding="utf-8")
            out.append(
                ScheduleArtifact(
                    product_id=contract_id,
                    build_id=bid,
                    orchestrator="dagster",
                    artifact_path=str(job_path),
                    cron=schedule,
                )
            )

        if "prefect" in chosen:
            depl_path = _safe_subpath(artifacts_root, f"{bid}_prefect.py")
            depl_path.write_text(
                _render_prefect_deployment(contract_id, bid, schedule), encoding="utf-8"
            )
            out.append(
                ScheduleArtifact(
                    product_id=contract_id,
                    build_id=bid,
                    orchestrator="prefect",
                    artifact_path=str(depl_path),
                    cron=schedule,
                )
            )

        if "cron" in chosen:
            cron_path = _safe_subpath(artifacts_root, f"{bid}.cron")
            cron_path.write_text(_render_cron_entry(contract_id, bid, schedule), encoding="utf-8")
            out.append(
                ScheduleArtifact(
                    product_id=contract_id,
                    build_id=bid,
                    orchestrator="cron",
                    artifact_path=str(cron_path),
                    cron=schedule,
                )
            )
    return out


def _python_safe_name(s: str) -> str:
    """Reduce a validated identifier to a Python-safe symbol.

    The caller already passed it through ``_validate_identifier`` so the
    only character class outside ``[A-Za-z0-9_]`` is dot/dash; we
    map both to ``_``.
    """
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)


def _render_airflow_dag(
    contract_id: str, build_id: str, build: Dict[str, Any], schedule: str
) -> str:
    """Render a deterministic Airflow DAG that calls ``fluid apply``.

    Caller MUST validate ``contract_id``, ``build_id``, and ``schedule``
    before calling this. The validators in
    :func:`schedule_sync_acquisition` ensure attacker-controlled
    contract metadata cannot inject Python code into the emitted DAG.
    """
    dag_id = _python_safe_name(f"fluid_{contract_id.replace('.', '_')}_{build_id}")
    # Bound retry count: a malicious contract that smuggles a giant int
    # past schema validation shouldn't be able to bloat the DAG file or
    # spawn 10**9 retries when Airflow loads it.
    raw_retries = build.get("execution", {}).get("retry", {}).get("count", 3)
    try:
        retries = max(0, min(int(raw_retries), 100))
    except (TypeError, ValueError):
        retries = 3
    contract_path = f"contracts/{contract_id}.fluid.yaml"
    return (
        '"""Auto-generated by fluid schedule-sync. Edit the contract, not this file."""\n\n'
        "from datetime import datetime, timedelta\n"
        "from airflow import DAG\n"
        "from airflow.operators.bash import BashOperator\n\n"
        "default_args = {\n"
        '    "owner": "fluid",\n'
        '    "depends_on_past": False,\n'
        f'    "retries": {retries},\n'
        '    "retry_delay": timedelta(minutes=5),\n'
        "}\n\n"
        f"with DAG(\n"
        f'    dag_id="{dag_id}",\n'
        f'    schedule="{schedule}",\n'
        "    start_date=datetime(2024, 1, 1),\n"
        "    catchup=False,\n"
        "    default_args=default_args,\n"
        f'    tags=["fluid", "acquisition", "{contract_id}"],\n'
        ") as dag:\n"
        "    apply = BashOperator(\n"
        '        task_id="apply",\n'
        f'        bash_command="fluid apply {contract_path} --build {build_id}",\n'
        "    )\n"
    )


def _render_dagster_job(contract_id: str, build_id: str, schedule: str) -> str:
    """Render a Dagster job. Inputs must be validator-clean."""
    job_name = _python_safe_name(f"fluid_{contract_id.replace('.', '_')}_{build_id}")
    return (
        '"""Auto-generated by fluid schedule-sync. Edit the contract, not this file."""\n\n'
        "import subprocess\n"
        "from dagster import job, op, ScheduleDefinition\n\n"
        "@op\n"
        "def fluid_apply(_):\n"
        "    subprocess.run(\n"
        f'        ["fluid", "apply", "contracts/{contract_id}.fluid.yaml", "--build", "{build_id}"],\n'
        "        check=True,\n"
        "    )\n\n"
        "@job\n"
        f"def {job_name}():\n"
        "    fluid_apply()\n\n"
        f'schedule = ScheduleDefinition(job={job_name}, cron_schedule="{schedule}")\n'
    )


def _render_prefect_deployment(contract_id: str, build_id: str, schedule: str) -> str:
    """Render a Prefect deployment. Inputs must be validator-clean."""
    flow_name = _python_safe_name(f"fluid_{contract_id.replace('.', '_')}_{build_id}")
    return (
        '"""Auto-generated by fluid schedule-sync. Edit the contract, not this file."""\n\n'
        "import subprocess\n"
        "from prefect import flow, task\n"
        "from prefect.client.schemas.schedules import CronSchedule\n\n"
        "@task\n"
        "def fluid_apply():\n"
        "    subprocess.run(\n"
        f'        ["fluid", "apply", "contracts/{contract_id}.fluid.yaml", "--build", "{build_id}"],\n'
        "        check=True,\n"
        "    )\n\n"
        "@flow\n"
        f"def {flow_name}():\n"
        "    fluid_apply()\n\n"
        f'deployment = {flow_name}.to_deployment(name="{flow_name}", '
        f'schedule=CronSchedule(cron="{schedule}"))\n'
    )


def _render_cron_entry(contract_id: str, build_id: str, schedule: str) -> str:
    """Render a crontab line. Inputs must be validator-clean."""
    return (
        "# Auto-generated by fluid schedule-sync. Edit the contract, not this file.\n"
        f"{schedule} fluid apply contracts/{contract_id}.fluid.yaml --build {build_id}\n"
    )


# ── Stage: policy-apply ──────────────────────────────────────────────────


@dataclass
class PolicyApplyResult:
    product_id: str
    build_id: str
    actions_applied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def policy_apply_acquisition(contract: Dict[str, Any], workdir: Path) -> List[PolicyApplyResult]:
    """Translate acquisition-level policy declarations into runtime actions.

    For each acquisition build we register:

    * **PII column masking** — when the run record carries column-level
      classifications, generate masking actions for each tagged column.
    * **Retention** — write the contract's top-level ``retention`` block
      into ``.fluid/policies/<product>/retention.json`` so the sweeper
      picks it up on its next run.
    * **DLQ alert routing** — write
      ``observability.alert.channels`` to
      ``.fluid/policies/<product>/alert_channels.json`` so the alerter
      picks it up at next run.
    * **Cost budgets** — write ``properties.cost.budget`` to
      ``.fluid/policies/<product>/cost_budget.json``.

    Returning a list of applied actions makes the result inspectable by
    the CLI for human-readable output.
    """
    out: List[PolicyApplyResult] = []
    pid = _validate_identifier(contract.get("id", ""), kind="contract.id")
    policies_parent = workdir / ".fluid" / "policies"
    policies_parent.mkdir(parents=True, exist_ok=True)
    policy_root = _safe_subpath(policies_parent, pid)
    policy_root.mkdir(parents=True, exist_ok=True)

    retention = contract.get("retention")
    if retention:
        _safe_subpath(policy_root, "retention.json").write_text(
            json.dumps(retention, indent=2), encoding="utf-8"
        )

    obs = (contract.get("observability") or {}).get("alert") or {}
    if obs:
        _safe_subpath(policy_root, "alert_channels.json").write_text(
            json.dumps(obs, indent=2), encoding="utf-8"
        )

    for build in acquisition_builds(contract):
        bid = _validate_identifier(build.get("id", ""), kind="build.id")
        applied: List[str] = []
        skipped: List[str] = []

        record = latest_run_record(workdir, pid, bid)
        classifications = (record or {}).get("facets", {}).get("classifications", {}) or {}
        if classifications:
            _safe_subpath(policy_root, f"{bid}_pii_masking.json").write_text(
                json.dumps({"masking": classifications}, indent=2), encoding="utf-8"
            )
            applied.append(f"pii_masking:{len(classifications)}_columns")
        else:
            skipped.append("pii_masking:no_classifications_yet")

        cost = build.get("properties", {}).get("cost")
        if cost:
            _safe_subpath(policy_root, f"{bid}_cost_budget.json").write_text(
                json.dumps(cost, indent=2), encoding="utf-8"
            )
            applied.append("cost_budget")

        if retention:
            applied.append("retention")
        if obs:
            applied.append("alert_channels")

        out.append(
            PolicyApplyResult(
                product_id=pid, build_id=bid, actions_applied=applied, skipped=skipped
            )
        )
    return out
