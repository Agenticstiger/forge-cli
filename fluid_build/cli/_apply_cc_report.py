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

"""``fluid apply`` reports each run to the Command Center, best effort.

The Command Center records CLI runs at ``POST /api/v1/executions`` (a run
starts, ``status: running``) and ``PATCH /api/v1/executions/{id}`` (it ends:
``success`` / ``failed``, the Command Center sets ``completed_at`` and
``duration_seconds``). forge-cli has shipped a client for exactly that API
since the observability module landed (``observability/reporter.py``,
``CommandCenterReporter``: async queue, circuit breaker, SSRF host gate), but
nothing called it (``cli/bootstrap.py::get_reporter`` has no callers), so a
Jenkins run left no trace. This wires that client into ``fluid apply``
rather than adding another one.

**Where it reports, and as whom.** Wherever ``fluid publish`` already does:
the ``fluid-command-center`` catalog configuration (``FLUID_CC_ENDPOINT``,
``FLUID_API_KEY`` or ``FLUID_BEARER_TOKEN``, and the organization from
``FLUID_CC_ORG_ID``, ``organization_id`` or the ``organization`` slug in the
product's ``fluid.config.yaml``, resolved by the publisher's own
``resolve_organization_id``), so a pipeline configured to publish reports its
applies with no new setting. The reporter's own ``FLUID_COMMAND_CENTER_URL``
/ ``FLUID_COMMAND_CENTER_API_KEY`` are the fallback. ``FLUID_COMMAND_CENTER_ENABLED=false``
turns it off. Without an organization the run is not sent: the Command
Center would store it untagged, where no read path shows it.

**What it says.** The run's product id, contract version and ``fluidVersion``,
the contract hash the Command Center keys contract versions by, the
``--env`` it was applied for, the provider, the apply mode, the state
location, the planned and applied change counts and the address of every
resource the module declares, and the timings. A build-augmented apply
(``--mode amend-and-build`` / ``replace-and-build``) adds each build it ran:
its id, how it ended, and, from the run record the build wrote, the run id,
the table it loaded and the rows it landed (``facets.bigquery_load``,
``facets.landed``, ``records_total``). Its phase is ``build``, and a failed
build names itself in the error event (``build_failed:<build id>``). **Never a secret**: no header,
no environment value, no tofu output (its text can carry attribute values);
a failure is reported by its typed event name and exit code only.

**Best effort.** A Command Center that is down, slow, refusing or
misconfigured costs the apply a warning line and at most the reporter's
timeout, never its exit code (the OpenLineage client's posture: failures
are logged, not raised). Every step below swallows its own errors.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import platform
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

_LOG = logging.getLogger(__name__)

#: The report of the ``fluid apply`` running in this context, if any.
_CURRENT: contextvars.ContextVar[Optional["ApplyRunReport"]] = contextvars.ContextVar(
    "fluid_apply_cc_report", default=None
)

_OFF_VALUES = {"0", "false", "no", "off"}

#: Why nothing is reported when nothing is configured: silent, by design.
_NOT_CONFIGURED = "no Command Center is configured"


def current_report() -> Optional["ApplyRunReport"]:
    """The report the running ``fluid apply`` fills, or ``None``."""
    return _CURRENT.get()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runner() -> str:
    """The CC's ``runner`` tag: ``jenkins`` under Jenkins (which sets
    ``JENKINS_URL`` for every build step), else ``cli``."""
    return "jenkins" if os.environ.get("JENKINS_URL") else "cli"


class ApplyRunReport:
    """One ``fluid apply`` run, as the Command Center's executions API takes it."""

    def __init__(self, args: Any, logger: logging.Logger) -> None:
        self.logger = logger
        self.execution_id = str(uuid.uuid4())
        self.started_at = _utc_now()
        self._t0 = time.monotonic()
        self.contract_path = str(getattr(args, "contract", "") or "") or None
        self.environment: Optional[str] = getattr(args, "env", None) or None
        self.mode = str(getattr(args, "mode", "") or "") or None
        self.provider: Optional[str] = None
        self.metadata: Dict[str, Any] = {}
        self.result: Dict[str, Any] = {}
        self._reporter: Any = None
        self._disabled_reason: Optional[str] = None
        self._began = False
        self._finished = False
        #: Why the build phase failed (``build_failed:<id>``), when it did.
        self._build_event: Optional[str] = None

    # -- filled by the apply engine ------------------------------------

    def identify(
        self,
        *,
        contract: Mapping[str, Any],
        provider: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> None:
        """What the run is for, as soon as the engine knows it; registers nothing.

        A run refused before :meth:`begin` (a sovereignty refusal in the
        emitter, a provider that cannot be resolved) is then still reported
        with its product, contract version and platform.
        """
        try:
            if environment:
                self.environment = environment
            self.metadata.update(_contract_facts(contract))
            if provider:
                self.provider = provider
                self.metadata["platform"] = provider
        except Exception as exc:  # noqa: BLE001 - reporting never fails an apply
            _LOG.debug("command center report: identify failed: %s", type(exc).__name__)

    def begin(
        self,
        *,
        contract: Mapping[str, Any],
        provider: str,
        environment: Optional[str] = None,
        state: Optional[str] = None,
    ) -> None:
        """The engine knows the contract and the provider: register the run."""
        try:
            self.provider = provider
            if environment:
                self.environment = environment
            self.metadata.update(_contract_facts(contract))
            self.metadata["platform"] = provider
            if state:
                self.metadata["state"] = state
            self._register()
        except Exception as exc:  # noqa: BLE001 - reporting never fails an apply
            _LOG.debug("command center report: begin failed: %s", type(exc).__name__)

    def record_infra(
        self,
        *,
        planned: Mapping[str, Any],
        applied: Optional[Mapping[str, Any]],
        resources: List[str],
        dry_run: bool,
    ) -> None:
        """What the plan and the apply did, and which resources the module holds."""
        self.result.update(
            {
                "planned_changes": {k: int(planned.get(k, 0)) for k in ("add", "change", "remove")},
                "applied_changes": (
                    None
                    if applied is None
                    else {k: int(applied.get(k, 0)) for k in ("add", "change", "remove")}
                ),
                "resources": list(resources),
                "dry_run": bool(dry_run),
            }
        )

    def record_build(
        self,
        *,
        build_id: str,
        status: str,
        run: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """One build of a build-augmented apply: ``succeeded``, ``failed`` or ``skipped``.

        ``run`` is the run record the build wrote, if it wrote one; only its
        id, the table it loaded, where it landed and the rows are kept.
        """
        try:
            entry: Dict[str, Any] = {"build_id": str(build_id), "status": str(status)}
            entry.update(_build_facts(run))
            self.result.setdefault("builds", []).append(entry)
            if status == "failed":
                self.build_failed(f"build_failed:{build_id}")
        except Exception as exc:  # noqa: BLE001 - reporting never fails an apply
            _LOG.debug("command center report: record_build failed: %s", type(exc).__name__)

    def build_failed(self, event: str) -> None:
        """The build phase failed for ``event``; the first reason is the one reported."""
        if not self._build_event:
            self._build_event = str(event)

    # -- the run's end -------------------------------------------------

    def finish(self, status: str, *, event: Optional[str] = None, exit_code: int = 0) -> None:
        """Close the run: ``success`` or ``failed``. Safe to call once, never raises."""
        if self._finished:
            return
        self._finished = True
        try:
            if not self._began:
                if not self.metadata.get("product_id"):
                    # Refused before the engine read the contract (plan
                    # binding, a declared env with no overlay): the base
                    # document still says which product this run was for.
                    self.metadata.update(_base_contract_facts(self.contract_path))
                self._register()
            reporter = self._reporter
            if reporter is None:
                return
            finished_at = _utc_now()
            duration = round(time.monotonic() - self._t0, 3)
            timings = {
                "started_at": self.started_at,
                "finished_at": finished_at,
                "duration_seconds": duration,
            }
            result = dict(self.result, exit_code=int(exit_code), **timings)
            if not event and status != "success":
                # A build that failed returns an exit code, not an exception.
                event = self._build_event
            if event:
                result["error_event"] = str(event)
            if self.result.get("dry_run"):
                phase = "plan"
            elif self.result.get("builds") or self._build_event:
                phase = "build"
            else:
                phase = "apply"
            reporter.update_execution(
                self.execution_id,
                status=status,
                progress=100.0,
                current_phase=phase,
                error_message=(f"fluid apply failed: {event}" if event else None),
                result=result,
            )
            reporter.stop(timeout=float(reporter.config.timeout) * 2 + 1)
            self._say_outcome(reporter)
        except Exception as exc:  # noqa: BLE001 - reporting never fails an apply
            _LOG.debug("command center report: finish failed: %s", type(exc).__name__)

    # -- internals -----------------------------------------------------

    def _register(self) -> None:
        self._began = True
        reporter = self._start_reporter()
        if reporter is None:
            return
        from fluid_build import __version__

        metadata = dict(self.metadata)
        metadata.setdefault("environment", self.environment)
        metadata["mode"] = self.mode
        reporter.register_execution(
            execution_id=self.execution_id,
            command="apply",
            contract_path=self.contract_path,
            provider=self.provider,
            environment=self.environment,
            runner=_runner(),
            cli_version=str(__version__),
            python_version=platform.python_version(),
            metadata=metadata,
        )

    def _start_reporter(self) -> Any:
        if self._reporter is not None:
            return self._reporter
        config, reason = _command_center_config(self.logger)
        if config is None:
            self._disabled_reason = reason
            if reason and reason != _NOT_CONFIGURED:
                # Configured but unusable: say so once. Unconfigured is silent.
                self._say(f"  command center: run not reported ({reason})")
            return None
        from fluid_build.observability.reporter import CommandCenterReporter

        reporter = CommandCenterReporter(config)
        reporter.start()
        if not reporter.running:
            # Refused by the reporter itself: its SSRF host gate, or no
            # ``requests``. It has logged which.
            self._disabled_reason = "the Command Center reporter did not start"
            self._say(f"  command center: run not reported ({self._disabled_reason})")
            return None
        self._reporter = reporter
        return reporter

    def _say_outcome(self, reporter: Any) -> None:
        sent, failed = reporter.stats.get("sent", 0), reporter.stats.get("failed", 0)
        if failed or sent < 2:
            self._say(
                f"  command center: run {self.execution_id} not fully reported "
                f"({sent} of 2 requests accepted); the apply's result is unaffected"
            )
        else:
            self._say(f"  command center: run {self.execution_id} reported")

    @staticmethod
    def _say(line: str) -> None:
        try:
            from fluid_build.cli.console import cprint

            cprint(line)
        except Exception:  # noqa: BLE001
            pass


def _build_facts(run: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The run id, the loaded table, the landed destinations and rows of a run record.

    Nothing else from the record: its facets can carry engine output.
    """
    if not isinstance(run, Mapping):
        return {}
    facts: Dict[str, Any] = {}
    if run.get("run_id"):
        facts["run_id"] = str(run["run_id"])
    facets = run.get("facets") if isinstance(run.get("facets"), Mapping) else {}
    load = facets.get("bigquery_load")
    if isinstance(load, Mapping):
        if load.get("table"):
            facts["table"] = str(load["table"])
        if isinstance(load.get("rows"), int):
            facts["rows"] = int(load["rows"])
    landed = facets.get("landed")
    destinations = landed.get("destinations") if isinstance(landed, Mapping) else None
    if isinstance(destinations, Mapping) and destinations:
        facts["destinations"] = {str(k): str(v) for k, v in destinations.items()}
    succeeded = str(run.get("state") or "").lower() == "succeeded"
    if "rows" not in facts and succeeded and isinstance(run.get("records_total"), int):
        facts["rows"] = int(run["records_total"])
    return facts


def _contract_facts(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Identity facts only: nothing from the contract's body beyond its id and versions."""
    facts: Dict[str, Any] = {
        "product_id": contract.get("id"),
        "product_name": contract.get("name"),
        "contract_version": contract.get("version"),
        "fluid_version": contract.get("fluidVersion"),
    }
    try:
        import yaml

        from fluid_build.providers.catalogs.fluid_cc.provider import command_center_contract_hash

        facts["contract_hash"] = command_center_contract_hash(
            yaml.safe_dump(dict(contract), sort_keys=False)
        )
    except Exception:  # noqa: BLE001 - a hash is a join key, not required
        pass
    return facts


def _base_contract_facts(contract_path: Optional[str]) -> Dict[str, Any]:
    """Identity facts from the base contract (or a plan's embedded one), no overlay.

    For a run that failed before the engine loaded the contract, possibly
    because its overlay could not be applied. The product id and versions
    are the base's (an overlay rebinds, it does not rename). No contract
    hash: the Command Center keys versions by the hash of the compiled
    contract, which this run never produced.
    """
    if not contract_path:
        return {}
    try:
        if contract_path.endswith(".json"):
            import json

            with open(contract_path, encoding="utf-8") as handle:
                plan = json.load(handle)
            contract = plan.get("contract") if isinstance(plan, dict) else None
        else:
            from fluid_build.loader import load_contract

            contract = load_contract(contract_path)
    except Exception:  # noqa: BLE001 - an unreadable contract is simply not described
        return {}
    if not isinstance(contract, Mapping):
        return {}
    facts = _contract_facts(contract)
    facts.pop("contract_hash", None)
    return {k: v for k, v in facts.items() if v is not None}


def _command_center_config(logger: logging.Logger):
    """``(CommandCenterConfig, None)`` to report with, or ``(None, reason)``."""
    from fluid_build.observability.config import CommandCenterConfig

    enabled = os.environ.get("FLUID_COMMAND_CENTER_ENABLED")
    if enabled is not None and enabled.strip().lower() in _OFF_VALUES:
        return None, "FLUID_COMMAND_CENTER_ENABLED is off"

    published = _publish_path_config(logger)
    if published is not None:
        return published
    env_config = CommandCenterConfig.from_environment()
    if env_config.is_configured():
        org = (os.environ.get("FLUID_CC_ORG_ID") or "").strip()
        if org and _header_safe(org):
            env_config.headers = {**env_config.headers, "X-Organization-Id": org}
        return env_config, None
    return None, _NOT_CONFIGURED


def _publish_path_config(logger: logging.Logger):
    """The ``fluid publish`` target, with its credential and its organization.

    ``None`` when the catalog configuration names no Command Center at all;
    ``(None, reason)`` when it names one this run cannot report to.
    """
    from fluid_build.config_manager import COMMAND_CENTER_CANONICAL_NAME, FluidConfig
    from fluid_build.observability.config import CommandCenterConfig

    try:
        catalog = FluidConfig().get_catalog_config(COMMAND_CENTER_CANONICAL_NAME) or {}
    except Exception:  # noqa: BLE001 - an unreadable config is "not configured"
        return None
    endpoint = str(catalog.get("endpoint") or "").strip()
    if not endpoint or not catalog.get("enabled", True):
        return None
    # The built-in defaults name ``http://localhost:8000`` with no key, so an
    # endpoint alone is not a configured Command Center: a credential is.
    from fluid_build.providers.common import get_auth_headers

    probe = get_auth_headers(endpoint, catalog.get("auth"))
    if not (probe.get("X-API-Key") or probe.get("Authorization")):
        return None
    # The reporter's SSRF gate, before the organization lookup below sends the
    # credential anywhere: loopback or an allow-listed host, never a private
    # or cloud-metadata address (observability/reporter.py).
    from fluid_build.observability.reporter import _command_center_host_allowed

    if not _command_center_host_allowed(endpoint):
        return None, (
            "its host resolves to a private or metadata address; allow it with "
            "FLUID_COMMAND_CENTER_HOST_ALLOWLIST"
        )
    try:
        from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

        provider = FluidCommandCenterProvider(catalog)
        org_id = _run_coroutine(provider.resolve_organization_id())
        headers = dict(provider._headers())
    except Exception as exc:  # noqa: BLE001 - typed CC errors say what is missing
        return None, f"the Command Center organization could not be settled ({type(exc).__name__})"
    api_key = headers.pop("X-API-Key", None)
    extra = {k: v for k, v in headers.items() if k in ("Authorization", "X-Organization-Id")}
    if org_id and _header_safe(org_id):
        extra["X-Organization-Id"] = org_id
    timeout = _timeout(catalog.get("timeout"))
    config = CommandCenterConfig(
        url=provider.endpoint, api_key=api_key, timeout=timeout, headers=extra
    )
    if not config.is_configured():
        return None, "the Command Center catalog configuration has no credential"
    return config, None


def _timeout(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(seconds, 10))


def _header_safe(value: str) -> bool:
    from fluid_build.providers.catalogs.fluid_cc.provider import _is_valid_org_id

    return bool(_is_valid_org_id(value))


def _run_coroutine(coro: Any) -> Any:
    """Run ``coro`` to completion from sync code, even under a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def reports_apply_run(fn: Callable[..., int]) -> Callable[..., int]:
    """Decorate ``fluid apply``'s ``run``: one Command Center run per invocation."""

    @functools.wraps(fn)
    def wrapper(args: Any, logger: logging.Logger) -> int:
        from ._common import CLIError

        report = ApplyRunReport(args, logger)
        token = _CURRENT.set(report)
        try:
            rc = fn(args, logger)
        except CLIError as exc:
            report.finish("failed", event=exc.event, exit_code=exc.exit_code)
            raise
        except BaseException as exc:
            report.finish("failed", event=type(exc).__name__, exit_code=1)
            raise
        else:
            report.finish("success" if rc == 0 else "failed", exit_code=rc)
            return rc
        finally:
            _CURRENT.reset(token)

    return wrapper
