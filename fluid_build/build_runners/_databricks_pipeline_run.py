# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Start a Lakeflow pipeline update now and wait for it: the build phase of ``fluid apply``.

A lakeflow build (``engine: lakeflow``) is a Lakeflow Declarative Pipeline that ``fluid apply``
provisions (``iac/providers/databricks_lakeflow.py``). Under ``--mode amend-and-build`` and
``--mode replace-and-build`` the build runner hands such a build here once ``tofu apply`` has
succeeded, as it hands a Databricks dbt job to ``_databricks_job_run.py``:

1. The pipeline is the one this apply's OpenTofu state holds for the build, found by the tags
   the emitter puts on it (``fluid_product``, ``fluid_build``; :func:`pipelines_in_state`).
2. One update starts now (``POST /api/2.0/pipelines/{id}/updates``); ``replace-and-build`` asks
   for a full refresh. A pipeline that is already updating is not started twice: the build
   fails, naming the update that runs.
3. The update is polled until it is ``COMPLETED``, ``FAILED`` or ``CANCELED``
   (``GET /api/2.0/pipelines/{id}/updates/{update_id}``): a line for every state it enters,
   and a line a minute while nothing changes.
4. The update's event log (``GET /api/2.0/pipelines/{id}/events``) gives each expectation's
   passed and failed records and each dataset's dropped records (``flow_progress``
   ``data_quality``): one line per rule, as the pipeline graph's Data quality tab shows them.
   A ``FAIL UPDATE`` rule that failed records nothing (Lakeflow writes no metrics for it), so
   the update's own error says which rule stopped it.
5. A completed update sets the table's tags and its columns' tags as the contract declares
   them (the tags a table forge-cli owns gets from OpenTofu; on a pipeline's table they wait
   for the table to exist, and survive later updates). A tag on the table or a column that the
   contract does not declare is removed when the deploying identity set it (the tag API's
   ``updated_by``), so a label the contract dropped goes, whatever ran the updates since, and
   a tag a steward set stays. A tag that cannot be set or removed fails the build.

A ``FAILED`` or ``CANCELED`` update fails the build with the event log's errors, credentials
masked. The wait is capped as a job run's is (``FLUID_DATABRICKS_RUN_TIMEOUT_SECONDS``, an hour
by default); at the cap, and on Ctrl-C, SIGTERM or SIGHUP, the update is stopped
(``POST /api/2.0/pipelines/{id}/stop``) before the build fails. A continuous pipeline is
never started here (Databricks keeps it running): its tags are set, once its table exists.

The polling, the interrupt handling and the printing are the job runner's
(``_databricks_job_run.py``, after databricks-sdk-py's waiters); the update states and the
event-log fields are the Pipelines API's, read live on Databricks Free Edition (2026-11-12).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import quote as url_quote

from fluid_build._console import error as console_error
from fluid_build.util.contract import is_continuous_build, is_lakeflow_build

from ._databricks_job_run import (
    DEFAULT_TIMEOUT_SECONDS,
    HEARTBEAT_SECONDS,
    TIMEOUT_ENV,
    Say,
    _clock,
    _default_client,
    _duration,
    _failure,
    _interrupt_on_terminate,
    _say,
    _timeout_override,
    clean,
)

LOG = logging.getLogger(__name__)

_PIPELINES = "/api/2.0/pipelines"
_TABLES = "/api/2.1/unity-catalog/tables"
_TAGS = "/api/2.1/unity-catalog/entity-tag-assignments"
_REPLACE_MODES = ("replace", "replace-and-build")
#: Update states that end it (the Pipelines API's ``UpdateInfoState``).
_ENDED = ("COMPLETED", "FAILED", "CANCELED")
_READ_ATTEMPTS = 5
#: Event-log pages read for one update, newest first (250 events each).
_EVENT_PAGES = 20
_ERRORS_SHOWN = 3


@dataclass(frozen=True)
class PipelineRef:
    """A Lakeflow pipeline an apply's OpenTofu state holds for one build."""

    pipeline_id: str
    url: str = ""


def started_pipeline_builds(
    contract: Mapping[str, Any], build_id: Optional[str]
) -> List[Mapping[str, Any]]:
    """The lakeflow builds a ``-and-build`` apply of ``contract`` runs: every one
    (``build_id`` empty), or the one ``build_id`` names. A triggered one gets an update; a
    continuous one, which Databricks keeps running, only its tags."""
    return [
        build
        for build in contract.get("builds") or []
        if isinstance(build, Mapping)
        and is_lakeflow_build(build)
        and (not build_id or build.get("id") == build_id)
    ]


def pipelines_in_state(resources: Iterable[Any], product: str) -> Dict[str, List[PipelineRef]]:
    """Build id to the ``databricks_pipeline`` resources ``product`` has in an OpenTofu state,
    by the emitter's ``fluid_product`` / ``fluid_build`` tags (a list per build, so a caller
    can refuse a build with more than one)."""
    from fluid_build.iac.providers.databricks_jobs import BUILD_TAG, PRODUCT_TAG

    found: Dict[str, List[PipelineRef]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping) or resource.get("type") != "databricks_pipeline":
            continue
        if resource.get("mode", "managed") != "managed":
            continue
        values = resource.get("values") if isinstance(resource.get("values"), Mapping) else {}
        tags = values.get("tags") if isinstance(values.get("tags"), Mapping) else {}
        if tags.get(PRODUCT_TAG) != product or not tags.get(BUILD_TAG):
            continue
        pipeline_id = str(values.get("id") or "")
        if pipeline_id:
            found.setdefault(str(tags[BUILD_TAG]), []).append(
                PipelineRef(pipeline_id, str(values.get("url") or ""))
            )
    return found


def _compiled_pipeline(
    contract: Mapping[str, Any], build_id: str, contract_file: Any = None
) -> Tuple[Any, str]:
    """``(the emitter's LakeflowPipeline for build_id, "")``: its table, rules and tags; or
    ``(None, why)``. Compiled from ``contract_file`` as ``fluid apply`` compiled it
    (``iac/source.py``), so a reference check finds the workspace it was resolved in."""
    from fluid_build.iac.providers.databricks import _compile
    from fluid_build.iac.source import contract_source

    with contract_source(contract_file):
        compiled = _compile(contract, os.environ)
    for pipeline in compiled.pipelines:
        if pipeline.build_id == build_id:
            return pipeline, ""
    refusals = "; ".join(f"{r.kind}: {r}" for r in compiled.refusals[:3])
    return None, refusals or "it has no Lakeflow pipeline for this build"


def run_pipeline_build(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    pipelines: Mapping[str, Sequence[PipelineRef]],
    *,
    dry_run: bool = False,
    mode: Optional[str] = None,
    contract_file: Any = None,
    client_factory: Optional[Callable[[], Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Start an update of ``build``'s pipeline now and wait for it to end. Returns 0 or 1.

    ``pipelines`` is :func:`pipelines_in_state` of the apply that provisioned it, and
    ``contract_file`` the contract file that apply read.
    """
    from fluid_build.iac.providers.databricks_jobs import job_build_id, job_product

    product = job_product(contract)
    build_id = job_build_id(contract, build)
    name = f"fluid {product} {build_id}"
    full_refresh = (mode or "").strip().lower() in _REPLACE_MODES
    how = " (full refresh)" if full_refresh else ""
    continuous = is_continuous_build(dict(build))
    if dry_run:
        _say(
            f"\n🔍 [DRY RUN] Build '{build_id}' - would set the tags of continuous Lakeflow "
            f"pipeline '{name}' (Databricks keeps it running)."
            if continuous
            else f"\n🔍 [DRY RUN] Build '{build_id}' - would start an update{how} of Lakeflow "
            f"pipeline '{name}' and wait for it."
        )
        return 0
    refs = list(pipelines.get(build_id) or [])
    if len(refs) != 1:
        found = "no Lakeflow pipeline" if not refs else f"{len(refs)} Lakeflow pipelines"
        console_error(
            f"Build '{build_id}' - this apply's OpenTofu state holds {found} tagged "
            f"fluid_product={product}, fluid_build={build_id}, so no update was started. "
            "Either the state could not be read back after the apply (said above), or the "
            "contract was not applied to databricks; fix that, then re-run fluid apply."
        )
        return 1
    compiled, why = _compiled_pipeline(contract, build_id, contract_file)
    if compiled is None:
        console_error(
            f"Build '{build_id}' - the contract does not compile to its Lakeflow pipeline here "
            f"({clean(why)}), so no update was started. fluid validate says the same."
        )
        return 1
    try:
        client = (client_factory or _default_client)()
    except ImportError:
        console_error(
            f"Build '{build_id}' - databricks-sdk is not installed, and the update of Lakeflow "
            f"pipeline '{name}' is started through it. Install the extra: "
            "pip install 'data-product-forge[databricks]'"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - a bad credential must not become a traceback
        console_error(
            f"Build '{build_id}' - could not create the Databricks client to start pipeline "
            f"'{name}': {_failure(exc)}"
        )
        return 1
    if continuous:
        return tag_running_pipeline(client, compiled, build_id=build_id, name=name)
    return update_and_wait(
        client,
        refs[0],
        compiled,
        build_id=build_id,
        name=name,
        full_refresh=full_refresh,
        env=os.environ if env is None else env,
        sleep=sleep,
        clock=clock,
    )


def tag_running_pipeline(
    client: Any, compiled: Any, *, build_id: str, name: str, say: Optional[Say] = None
) -> int:
    """A continuous pipeline's tags: Databricks keeps it running, so no update is started;
    once its table exists, its tags are set as after an update. Returns 0 or 1."""
    say = say or _say
    api = client.api_client
    try:
        api.do("GET", f"{_TABLES}/{url_quote(compiled.table, safe='')}")
    except Exception:  # noqa: BLE001 - not there yet (or not readable): nothing to tag now
        say(
            f"\n⏭️  Build '{build_id}' - continuous Lakeflow pipeline '{name}' runs in "
            f"Databricks; {compiled.table} is not there yet, so its tags are set by the next "
            "apply after its first update."
        )
        return 0
    say(f"\n⏭️  Build '{build_id}' - continuous Lakeflow pipeline '{name}' runs in Databricks")
    problems = _sync_tags(api, compiled, say)
    if problems:
        console_error(
            f"Build '{build_id}' - the table's tags could not all be set: {'; '.join(problems)}."
        )
        return 1
    return 0


def update_and_wait(
    client: Any,
    pipeline: PipelineRef,
    compiled: Any,
    *,
    build_id: str,
    name: str,
    full_refresh: bool = False,
    env: Mapping[str, str],
    say: Optional[Say] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run one update of ``pipeline`` through ``client`` and report it. Returns 0 or 1."""
    say = say or _say
    api = client.api_client
    label = f"Lakeflow pipeline '{name}' ({pipeline.pipeline_id})"
    override, raw = _timeout_override(env)
    budget = override or DEFAULT_TIMEOUT_SECONDS
    # One guard from the start request to the end of the wait: a Ctrl-C or SIGTERM while the
    # request is in flight stops what it may have started (the pipeline is known before the
    # update is), and one while the update runs stops it.
    with _stopped_if_interrupted(api, pipeline.pipeline_id, say):
        try:
            started = (
                api.do(
                    "POST",
                    f"{_PIPELINES}/{pipeline.pipeline_id}/updates",
                    body={"full_refresh": full_refresh},
                )
                or {}
            )
            update_id = str(started["update_id"])
        except Exception as exc:  # noqa: BLE001
            console_error(
                f"Build '{build_id}' - could not start an update of {label}: {_failure(exc)}"
            )
            if pipeline.url:
                say(f"   pipeline page: {clean(pipeline.url)}")
            return 1
        LOG.debug(
            "lakeflow_update_started pipeline_id=%s update_id=%s", pipeline.pipeline_id, update_id
        )
        say(
            f"\n🚀 Build '{build_id}' - started update {update_id}"
            f"{' (full refresh)' if full_refresh else ''} of {label}"
        )
        if pipeline.url:
            say(f"   pipeline page: {clean(pipeline.url)}")
        if raw and override is None:
            say(
                f"   {TIMEOUT_ENV}={clean(raw)!r} is not a whole number of seconds above 0: "
                f"waiting up to {_duration(budget)}"
            )
        start = clock()
        state = _wait(
            api, pipeline.pipeline_id, update_id, build_id, budget, start, say, sleep, clock
        )
    if state is None:
        return 1
    events = _update_events(api, pipeline.pipeline_id, update_id)
    _report_quality(events, compiled, say)
    if state != "COMPLETED":
        console_error(f"Build '{build_id}' - update {update_id} of {label} ended {state}.")
        for message in _errors(events)[:_ERRORS_SHOWN]:
            say(f"   {message}")
        return 1
    problems = _sync_tags(api, compiled, say)
    if problems:
        console_error(
            f"Build '{build_id}' - update {update_id} completed, but the table's tags could not "
            f"all be set: {'; '.join(problems)}. Re-run fluid apply --mode amend-and-build."
        )
        return 1
    say(f"✅ Build '{build_id}' - update {update_id} completed in {_duration(clock() - start)}")
    return 0


def _wait(
    api: Any,
    pipeline_id: str,
    update_id: str,
    build_id: str,
    budget: int,
    start: float,
    say: Say,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Optional[str]:
    """Poll the update until it ends: its end state, or ``None`` once its failure is said."""
    last = ""
    noted = start
    delay = 0
    failed_reads = 0
    while True:
        try:
            got = api.do("GET", f"{_PIPELINES}/{pipeline_id}/updates/{update_id}") or {}
            state = str((got.get("update") or {}).get("state") or "")
        except Exception as exc:  # noqa: BLE001 - one failed read says nothing about the update
            failed_reads += 1
            if failed_reads == 1:
                say(
                    f"   {_clock(clock() - start)} could not read update {update_id} "
                    f"({_failure(exc)}); trying again"
                )
            if failed_reads >= _READ_ATTEMPTS:
                console_error(
                    f"Build '{build_id}' - could not read update {update_id} {failed_reads} "
                    f"times in a row: {_failure(exc)}. fluid apply leaves it as it is; it may "
                    "still be going."
                )
                return None
            sleep(5 * failed_reads)
            continue
        failed_reads = 0
        now = clock()
        if state in _ENDED:
            say(f"   {_clock(now - start)} {state}")
            return state
        if state != last:
            say(f"   {_clock(now - start)} {state or 'starting'}")
            last, noted = state, now
        elif now - noted >= HEARTBEAT_SECONDS:
            say(f"   {_clock(now - start)} still {state}")
            noted = now
        if now - start >= budget:
            stopped = _stop(api, pipeline_id, say)
            console_error(
                f"Build '{build_id}' - update {update_id} did not end within {_duration(budget)}"
                + (" and was stopped" if stopped else "")
                + f". Set {TIMEOUT_ENV} (seconds) to wait longer."
            )
            return None
        delay = min(delay + 1, 10)
        sleep(delay)


@contextmanager
def _stopped_if_interrupted(api: Any, pipeline_id: str, say: Say) -> Iterator[None]:
    """Ctrl-C, SIGTERM or SIGHUP while the update is waited on stops it, then interrupts as
    Ctrl-C always does (``_databricks_job_run._cancelled_if_interrupted``, for a pipeline)."""
    previous = _interrupt_on_terminate()
    try:
        yield
    except KeyboardInterrupt:
        _stop(api, pipeline_id, say)
        raise
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (TypeError, ValueError, OSError):
                pass  # best effort, as the job runner restores its handlers


def _stop(api: Any, pipeline_id: str, say: Say) -> bool:
    try:
        api.do("POST", f"{_PIPELINES}/{pipeline_id}/stop")
    except Exception as exc:  # noqa: BLE001
        say(f"   could not stop pipeline {pipeline_id}: {_failure(exc)}")
        return False
    say(f"   stopped pipeline {pipeline_id}")
    return True


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _update_events(api: Any, pipeline_id: str, update_id: str) -> List[Mapping[str, Any]]:
    """The update's events, oldest first: newest-first pages until the update's own start
    (``create_update``) or :data:`_EVENT_PAGES` pages. A read that fails ends the reading."""
    events: List[Mapping[str, Any]] = []
    token: Optional[str] = None
    for _page in range(_EVENT_PAGES):
        query: Dict[str, Any] = {"max_results": 250}
        if token:
            query["page_token"] = token
        try:
            got = _mapping(api.do("GET", f"{_PIPELINES}/{pipeline_id}/events", query=query))
        except Exception as exc:  # noqa: BLE001 - the report says what could not be read
            LOG.debug("lakeflow_events_unread pipeline_id=%s error=%s", pipeline_id, exc)
            break
        raw = got.get("events")
        page = [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []
        mine = [e for e in page if _mapping(e.get("origin")).get("update_id") == update_id]
        events.extend(mine)
        if any(e.get("event_type") == "create_update" for e in mine):
            break
        token = got.get("next_page_token") if isinstance(got.get("next_page_token"), str) else None
        if not token:
            break
    return list(reversed(events))


def quality(
    events: Iterable[Mapping[str, Any]],
) -> Tuple[Dict[Tuple[str, str], Tuple[int, int]], Dict[str, int]]:
    """``({(dataset, expectation): (passed, failed)}, {dataset: dropped})`` from an update's
    ``flow_progress`` events. A batch flow reports its counts once, possibly repeated; the
    largest is kept, as Databricks' own event-log queries take the MAX per flow."""
    counts: Dict[Tuple[str, str], Tuple[int, int]] = {}
    dropped: Dict[str, int] = {}
    for event in events:
        if event.get("event_type") != "flow_progress":
            continue
        progress = _mapping(_mapping(event.get("details")).get("flow_progress"))
        data_quality = progress.get("data_quality")
        if not isinstance(data_quality, Mapping):
            continue
        items = data_quality.get("expectations")
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, Mapping):
                continue
            key = (str(item.get("dataset") or ""), str(item.get("name") or ""))
            passed, failed = _count(item.get("passed_records")), _count(item.get("failed_records"))
            seen = counts.get(key, (0, 0))
            counts[key] = (max(seen[0], passed), max(seen[1], failed))
        flow = str(_mapping(event.get("origin")).get("flow_name") or "")
        if flow:
            dropped[flow] = max(dropped.get(flow, 0), _count(data_quality.get("dropped_records")))
    return counts, dropped


def _report_quality(events: Sequence[Mapping[str, Any]], compiled: Any, say: Say) -> None:
    """One line per rule: its dataset, severity and counts, or why there are none. A rule
    with failures is marked ⚠ when it only counts them (info, warn) and ✗ when it drops the
    rows or fails the update (error, critical)."""
    counts, dropped = quality(events)
    if not compiled.expectations:
        return
    say("   data quality (the pipeline graph's Data quality tab):")
    width = max(len(rule) for _dataset, rule, _severity in compiled.expectations)
    for dataset, rule, severity in compiled.expectations:
        short = dataset.rsplit(".", 1)[-1]
        got = counts.get((dataset, rule))
        if got is None:
            line, mark = "no counts (a FAIL UPDATE rule that failed, or a dataset not updated)", "?"
        else:
            passed, failed = got
            line = f"passed {passed}, failed {failed}"
            if severity == "error" and failed and short == compiled.table.rsplit(".", 1)[-1]:
                line += f" (dropped {dropped.get(dataset, failed)})"
            mark = "✓" if not failed else "⚠" if severity in ("info", "warn") else "✗"
        say(f"     {mark} {rule.ljust(width)}  {severity:<8} {short}: {line}")


def _errors(events: Sequence[Mapping[str, Any]]) -> List[str]:
    """The update's ERROR events, oldest first, cleaned for printing."""
    out = []
    for event in events:
        if str(event.get("level") or "") != "ERROR":
            continue
        text = " ".join(clean(event.get("message") or "").split())
        exceptions = _mapping(event.get("error")).get("exceptions")
        for exception in exceptions if isinstance(exceptions, list) else ():
            if isinstance(exception, Mapping) and exception.get("message"):
                text += " - " + " ".join(clean(exception["message"]).split())
                break
        if text:
            out.append(text[:600])
    return out


def _identity(api: Any) -> Optional[str]:
    """The deploying identity's user name, as the tag API's ``updated_by`` names it; ``None``
    when it cannot be read (nothing is then removed)."""
    try:
        me = _mapping(api.do("GET", "/api/2.0/preview/scim/v2/Me"))
    except Exception:  # noqa: BLE001 - said by the caller
        return None
    name = me.get("userName")
    return str(name) if name else None


def _entity(kind: str, name: str) -> str:
    return f"{_TAGS}/{kind}/{url_quote(name, safe='')}/tags"


def _tags_on(api: Any, kind: str, name: str) -> Dict[str, Tuple[str, str]]:
    """The tags set on ``name`` itself (not inherited): key to ``(value, updated_by)``."""
    got = _mapping(api.do("GET", _entity(kind, name)))
    items = got.get("tag_assignments")
    return {
        str(item.get("tag_key")): (
            str(item.get("tag_value") or ""),
            str(item.get("updated_by") or ""),
        )
        for item in (items if isinstance(items, list) else ())
        if isinstance(item, Mapping) and item.get("tag_key") and not item.get("inherited")
    }


def _set_tags(
    api: Any, kind: str, name: str, wanted: Mapping[str, str], me: Optional[str]
) -> List[str]:
    """Make ``name``'s tags hold ``wanted``, and drop the ones the deploying identity ``me``
    set that the contract no longer declares; what failed."""
    problems: List[str] = []
    try:
        current = _tags_on(api, kind, name)
    except Exception as exc:  # noqa: BLE001
        return [f"could not read the tags of {name}: {_failure(exc)}"]
    stale = sorted(k for k, (_v, by) in current.items() if k not in wanted and me and by == me)
    for key in stale:
        try:
            api.do("DELETE", f"{_entity(kind, name)}/{url_quote(key, safe='')}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{key} on {name}: {_failure(exc)}")
    for key, value in sorted(wanted.items()):
        if key in current and current[key][0] == value:
            continue
        try:
            if key in current:
                api.do("DELETE", f"{_entity(kind, name)}/{url_quote(key, safe='')}")
            api.do(
                "POST",
                _TAGS,
                body={"entity_type": kind, "entity_name": name, "tag_key": key, "tag_value": value},
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{key} on {name}: {_failure(exc)}")
    return problems


def _sync_tags(api: Any, compiled: Any, say: Say) -> List[str]:
    """Set the table's and its columns' tags as the contract declares them, and remove the
    ones the deploying identity set that it no longer declares (on the table and on every
    column the table has)."""
    me = _identity(api)
    if me is None:
        say("   could not read the deploying identity: tags no longer declared are left in place")
    wanted = dict(compiled.table_tags)
    problems = _set_tags(api, "tables", compiled.table, wanted, me)
    on_columns: Dict[str, Dict[str, str]] = {}
    for column, key, value in compiled.column_tags:
        on_columns.setdefault(column, {})[key] = value
    for column in compiled.columns or sorted(on_columns):
        problems += _set_tags(
            api, "columns", f"{compiled.table}.{column}", on_columns.get(column, {}), me
        )
    if not problems:
        say(
            f"   tags: {len(wanted)} on {compiled.table}, "
            f"{sum(len(v) for v in on_columns.values())} on its columns"
        )
    return problems


__all__ = [
    "PipelineRef",
    "pipelines_in_state",
    "quality",
    "run_pipeline_build",
    "started_pipeline_builds",
    "tag_running_pipeline",
    "update_and_wait",
]
