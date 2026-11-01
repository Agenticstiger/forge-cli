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

"""Run a table's column masks and row filter on the SQL warehouse: read, diff, bind, fail safe.

The SQL itself comes from :mod:`databricks_access`. The ``databricks/databricks`` provider
(1.140.0) has no function resource and no mask or filter attribute on
``databricks_sql_table`` (terraform-provider-databricks#4550), so these statements run on the
warehouse ``DATABRICKS_WAREHOUSE_ID`` names, through the SQL Statement Execution API, outside
OpenTofu, in two hooks ``fluid apply`` calls around ``tofu apply``:

* :func:`pre_apply` binds what the contract asks on every table that ALREADY exists, before
  ``tofu apply`` runs, so a grant the apply adds lands on a table that is already protected.
  A statement that needs a column the apply has yet to add waits for :func:`post_apply`.
* :func:`post_apply` binds whatever is still missing (the tables the apply just created, empty
  at that moment), and drops what the contract no longer asks for: a mask or filter whose
  function carries this module's name prefix. A filter an admin set by hand is left alone.
  Dropping widens access, so it waits for the apply to succeed and is printed as such.

Both read the live bindings first (``GET /api/2.1/unity-catalog/tables/{name}``:
``row_filter.function_name`` / ``input_column_names`` and ``columns[].mask.function_name``,
databricks-sdk-go v0.190.0 ``catalog/model.go``) and run only what differs, so a second run
executes nothing.

Fail closed. ``pre_apply`` stops the run before ``tofu apply`` on any failure. When
``post_apply`` cannot bind a table it revokes ``SELECT`` on it from every principal a
restriction names, so a later dbt run cannot expose unmasked rows to them, says exactly what
it revoked and why, and the run exits non-zero; ``fluid apply`` again, once the cause is fixed,
binds first and grants again after.

The client is ``databricks.sdk.WorkspaceClient`` built from ``sdk_config`` (the credential
policy the provider and the doctor share), used through ``api_client.do``. The SDK is imported
lazily: it is the ``[databricks]`` extra. No message carries a credential.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from ..base import UnsupportedBindingError
from .databricks_access import (
    MASK_PREFIX,
    ROW_FILTER_PREFIX,
    TableEnforcement,
    is_forge_function,
    revoke_statements,
    same_function,
    unbind_mask,
    unbind_row_filter,
)

#: The SQL warehouse every statement runs on.
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
DATABRICKS_EXTRA_INSTALL = "pip install 'data-product-forge[databricks]'"

#: Seconds one statement may take, and how long to wait between polls of it.
STATEMENT_TIMEOUT_SECONDS = 600
POLL_SECONDS = 1.0
_MAX_MESSAGE = 400

Say = Callable[[str], None]
ClientFactory = Callable[[], Any]


class EnforcementError(UnsupportedBindingError):
    """A mask or row filter could not be put in place (``databricks-enforcement-failed``)."""

    def __init__(self, message: str, remediation: Sequence[str] = ()):
        super().__init__("databricks-enforcement-failed", message, tuple(remediation))


class StatementFailed(Exception):
    """One SQL statement did not succeed; the message is the server's, scrubbed."""


@dataclass(frozen=True)
class Step:
    """One statement, and what it is for."""

    statement: str
    what: str
    #: Lower-case columns of the table the statement refers to; it waits until they exist.
    needs: Tuple[str, ...] = ()
    #: Drops a mask or filter: it widens access.
    widens: bool = False


@dataclass
class TablePlan:
    full_name: str
    exists: bool
    bind: List[Step] = field(default_factory=list)
    drop: List[Step] = field(default_factory=list)


# ── client and statements ──────────────────────────────────────────────


def default_client() -> Any:
    """``WorkspaceClient(**sdk_config(os.environ))``; the SDK imported only here.

    ``sdk_config`` first: it needs no SDK, and a credential problem should be reported as
    that, not as a missing extra.
    """
    from .databricks_auth import sdk_config

    config = sdk_config(os.environ)
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(**config)


def _scrub(text: Any) -> str:
    """One short line with every credential value in the environment removed."""
    from ...observability.secret_redactor import redact_secret_text
    from .databricks_auth import credential_values

    line = " ".join(
        redact_secret_text(str(text), extra_secrets=credential_values(os.environ)).split()
    )
    return line if len(line) <= _MAX_MESSAGE else line[: _MAX_MESSAGE - 3] + "..."


def _is_not_found(exc: BaseException) -> bool:
    for attr in ("status_code", "status", "http_status"):
        if getattr(exc, attr, None) == 404:
            return True
    if getattr(getattr(exc, "response", None), "status_code", None) == 404:
        return True
    if type(exc).__name__ in {"NotFound", "ResourceDoesNotExist"}:
        return True
    return str(getattr(exc, "error_code", "") or "") in {
        "TABLE_DOES_NOT_EXIST",
        "RESOURCE_DOES_NOT_EXIST",
        "NOT_FOUND",
    }


def read_table(client: Any, full_name: str) -> Optional[Dict[str, Any]]:
    """The live table, or ``None`` when it does not exist."""
    try:
        out = client.api_client.do(
            "GET", f"/api/2.1/unity-catalog/tables/{quote(full_name, safe='.')}"
        )
    except Exception as exc:  # noqa: BLE001 — a 404 is an answer, anything else is raised on
        if _is_not_found(exc):
            return None
        raise
    return out if isinstance(out, dict) else {}


class SqlRunner:
    """Runs statements on a warehouse through the SQL Statement Execution API."""

    def __init__(
        self,
        client: Any,
        warehouse_id: str,
        *,
        timeout: float = STATEMENT_TIMEOUT_SECONDS,
        poll: float = POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self._warehouse = warehouse_id
        self._timeout = timeout
        self._poll = poll
        self._sleep = sleep

    def run(self, statement: str) -> None:
        """Run one statement to ``SUCCEEDED``; :class:`StatementFailed` otherwise."""
        do = self._client.api_client.do
        try:
            doc = do(
                "POST",
                "/api/2.0/sql/statements/",
                body={
                    "warehouse_id": self._warehouse,
                    "statement": statement,
                    "wait_timeout": "30s",
                    "on_wait_timeout": "CONTINUE",
                },
            )
            deadline = time.monotonic() + self._timeout
            while self._state(doc) in ("PENDING", "RUNNING"):
                if time.monotonic() > deadline:
                    self._cancel(doc)
                    raise StatementFailed(f"it did not finish in {self._timeout:.0f}s")
                self._sleep(self._poll)
                doc = do("GET", f"/api/2.0/sql/statements/{quote(str(doc.get('statement_id')))}")
        except StatementFailed:
            raise
        except Exception as exc:  # noqa: BLE001 — every transport failure is the statement's
            raise StatementFailed(f"{type(exc).__name__}: {_scrub(exc)}") from exc
        state = self._state(doc)
        if state != "SUCCEEDED":
            error = (doc.get("status") or {}).get("error") or {}
            raise StatementFailed(
                f"{state}: {_scrub(error.get('message') or 'no message from the server')}"
            )

    @staticmethod
    def _state(doc: Any) -> str:
        status = doc.get("status") if isinstance(doc, dict) else None
        return str((status or {}).get("state") or "UNKNOWN").upper()

    def _cancel(self, doc: Mapping[str, Any]) -> None:
        try:
            self._client.api_client.do(
                "POST", f"/api/2.0/sql/statements/{quote(str(doc.get('statement_id')))}/cancel"
            )
        except Exception:  # noqa: BLE001, S110 — best effort; the timeout is what is reported
            pass


# ── the diff ───────────────────────────────────────────────────────────


def _live_columns(live: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {
        str(c["name"]).lower(): c
        for c in live.get("columns") or []
        if isinstance(c, Mapping) and c.get("name")
    }


def plan_table(
    full_name: str, enforcement: Optional[TableEnforcement], live: Optional[Mapping[str, Any]]
) -> TablePlan:
    """What differs between what the table must enforce and what it enforces now.

    ``live`` is ``None`` for a table that does not exist yet: everything is to bind after
    the apply creates it. Without ``enforcement`` the only work is dropping the mask or filter
    an earlier run bound that the contract no longer asks for.
    """
    plan = TablePlan(full_name=full_name, exists=live is not None)
    live_columns = _live_columns(live or {})
    row_filter = (live or {}).get("row_filter") or {}
    live_filter = str(row_filter.get("function_name") or "") if row_filter else ""
    if enforcement is not None and enforcement.row_filter is not None:
        spec = enforcement.row_filter
        live_inputs = [str(c).lower() for c in row_filter.get("input_column_names") or []]
        if live is None or not (
            same_function(live_filter, spec.function.full_name)
            and live_inputs == [c.lower() for c in spec.columns]
        ):
            needs = tuple(c.lower() for c in spec.columns)
            plan.bind.append(
                Step(
                    spec.function.create,
                    f"create row filter function {spec.function.full_name}",
                    needs,
                )
            )
            plan.bind.append(
                Step(spec.bind, f"bind row filter {spec.function.full_name} on {full_name}", needs)
            )
    elif live_filter and is_forge_function(live_filter, ROW_FILTER_PREFIX):
        plan.drop.append(
            Step(
                unbind_row_filter(full_name),
                f"drop row filter {live_filter} from {full_name}: the contract has no row filter",
                widens=True,
            )
        )
    wanted = {m.column.lower() for m in (enforcement.masks if enforcement else ())}
    for mask in enforcement.masks if enforcement else ():
        live_mask = (live_columns.get(mask.column.lower()) or {}).get("mask") or {}
        if live is None or not same_function(
            live_mask.get("function_name"), mask.function.full_name
        ):
            needs = (mask.column.lower(),)
            plan.bind.append(
                Step(
                    mask.function.create,
                    f"create mask function {mask.function.full_name}",
                    needs,
                )
            )
            plan.bind.append(
                Step(
                    mask.bind,
                    f"bind mask {mask.function.full_name} on {full_name}.{mask.column}",
                    needs,
                )
            )
    for name, column in live_columns.items():
        function = str((column.get("mask") or {}).get("function_name") or "")
        if function and name not in wanted and is_forge_function(function, MASK_PREFIX):
            plan.drop.append(
                Step(
                    unbind_mask(full_name, str(column["name"])),
                    f"drop mask {function} from {full_name}.{column['name']}: the contract "
                    "restricts no such column",
                    widens=True,
                )
            )
    return plan


def _ready(step: Step, live: Optional[Mapping[str, Any]]) -> bool:
    columns = _live_columns(live or {})
    return live is not None and all(c in columns for c in step.needs)


# ── the hooks ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    full_name: str
    enforcement: Optional[TableEnforcement]


def _client_for(
    targets: Sequence[Target], factory: Optional[ClientFactory], say: Say
) -> Optional[Tuple[Any, SqlRunner]]:
    """A client and runner, or ``None`` (with a note) when only removals could be checked.

    A contract with a restriction needs both, and fails without them; one without can only
    be dropping what an earlier apply bound, which is access narrowing left in place, so
    an unreachable workspace is a note, not an error.
    """
    required = any(t.enforcement is not None for t in targets)
    try:
        client = (factory or default_client)()
        warehouse = str(os.environ.get(WAREHOUSE_ENV) or "").strip()
        if not warehouse:
            raise EnforcementError(
                f"{WAREHOUSE_ENV} is not set, and the masks and row filters run on that warehouse.",
                (f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace.",),
            )
        return client, SqlRunner(client, warehouse)
    except UnsupportedBindingError:
        if required:
            raise
    except ImportError:
        if required:
            raise EnforcementError(
                "databricks-sdk is not installed, and the contract's column restrictions and "
                "row filters are applied through it.",
                (f"Install the extra: {DATABRICKS_EXTRA_INSTALL}",),
            ) from None
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        if required:
            raise EnforcementError(
                f"could not create the Databricks client for the masks and row filters: "
                f"{type(exc).__name__}: {_scrub(exc)}"
            ) from exc
    say(
        "  note: masks and row filters an earlier apply bound were not checked for removal "
        "(no Databricks client); any left in place keep restricting access."
    )
    return None


def _read(client: Any, target: Target) -> Optional[Dict[str, Any]]:
    try:
        return read_table(client, target.full_name)
    except Exception as exc:  # noqa: BLE001
        raise EnforcementError(
            f"could not read {target.full_name} from Unity Catalog to see which masks and row "
            f"filters it has: {type(exc).__name__}: {_scrub(exc)}",
            ("Check DATABRICKS_HOST and the credential, then re-run fluid apply.",),
        ) from exc


def preview(
    targets: Sequence[Target], say: Say, client_factory: Optional[ClientFactory] = None
) -> None:
    """Print the statements an apply would run, in the order it runs them; run none."""
    if not targets:
        return
    pair = _client_for(targets, client_factory, say)
    if pair is None:
        return
    client, _runner = pair
    lines: List[Tuple[TablePlan, List[Tuple[str, Step]]]] = []
    for target in targets:
        live = _read(client, target)
        plan = plan_table(target.full_name, target.enforcement, live)
        steps = [("before apply" if _ready(s, live) else "after apply", s) for s in plan.bind]
        steps += [("after apply", s) for s in plan.drop]
        if steps:
            lines.append((plan, steps))
    if not lines:
        return
    say("\n  Unity Catalog masks and row filters (run on the SQL warehouse, not by OpenTofu):")
    for plan, steps in lines:
        say(f"  {plan.full_name}" + ("" if plan.exists else " (not created yet)"))
        for when, step in steps:
            note = " [widens access]" if step.widens else ""
            say(f"    {when}{note}: {step.what}")
            say(f"      {step.statement}")
    say("    dry-run: none of these statements ran.")


def pre_apply(
    targets: Sequence[Target], say: Say, client_factory: Optional[ClientFactory] = None
) -> None:
    """Bind what the contract asks on the tables that already exist, before ``tofu apply``."""
    wanted = [t for t in targets if t.enforcement is not None]
    if not wanted:
        return
    pair = _client_for(wanted, client_factory, say)
    if pair is None:  # unreachable: a restriction was declared, so _client_for raised
        raise EnforcementError("no Databricks client for the masks and row filters.")
    client, runner = pair
    for target in wanted:
        live = _read(client, target)
        if live is None:
            continue  # created by this apply: post_apply binds it, empty
        plan = plan_table(target.full_name, target.enforcement, live)
        steps = [s for s in plan.bind if _ready(s, live)]
        if not steps:
            continue
        say(f"\n  {target.full_name}: binding masks and row filter before the apply")
        for step in steps:
            try:
                runner.run(step.statement)
            except StatementFailed as exc:
                raise EnforcementError(
                    f"{target.full_name}: {step.what} failed before the apply, so the apply did "
                    f"not run and nothing was changed after it: {exc}. The statement was: "
                    f"{step.statement}",
                    (
                        "Fix the cause (the warehouse must be able to run it, and this identity "
                        "needs CREATE FUNCTION on the schema and to own or MANAGE the table), "
                        "then re-run fluid apply.",
                    ),
                ) from exc
            say(f"    done: {step.what}")


def post_apply(
    targets: Sequence[Target], say: Say, client_factory: Optional[ClientFactory] = None
) -> None:
    """Bind what is still missing, drop what the contract no longer asks, fail safe."""
    if not targets:
        return
    pair = _client_for(targets, client_factory, say)
    if pair is None:
        return
    client, runner = pair
    failed: List[Tuple[Target, str]] = []
    unrevoked: List[str] = []
    for target in targets:
        live = _read(client, target)
        if live is None:
            if target.enforcement is not None:
                failed.append((target, "the table does not exist after the apply"))
                unrevoked += _fail_safe(runner, target, say)
            continue
        plan = plan_table(target.full_name, target.enforcement, live)
        if plan.bind or plan.drop:
            say(f"\n  {target.full_name}: masks and row filter")
        for step in [*plan.bind, *plan.drop]:
            if step.widens:
                say(f"    access widened (no data lost, not gated): {step.what}")
            try:
                runner.run(step.statement)
            except StatementFailed as exc:
                reason = f"{step.what} failed: {exc}. The statement was: {step.statement}"
                say(f"    FAILED: {reason}")
                failed.append((target, reason))
                if target.enforcement is not None:
                    unrevoked += _fail_safe(runner, target, say)
                break
            say(f"    done: {step.what}")
    if failed:
        names = ", ".join(sorted({t.full_name for t, _ in failed}))
        raise EnforcementError(
            f"the apply succeeded, but the masks or row filter of {names} could not be put in "
            "place. "
            + (
                "SELECT was revoked on each from the principals the contract restricts "
                "(listed above), so none of them can read unmasked rows. "
                if not unrevoked
                else "SELECT could NOT be revoked by: " + "; ".join(unrevoked) + ". Run those "
                "yourself, now. "
            )
            + " ".join(f"{t.full_name}: {why}" for t, why in failed),
            (
                "Fix the cause, then re-run fluid apply: it binds the masks and row filter "
                "first and grants SELECT again afterwards.",
            ),
        )


def _fail_safe(runner: SqlRunner, target: Target, say: Say) -> List[str]:
    """Revoke ``SELECT`` on the table from every principal a restriction names, and say so.

    Returns the statements that failed, which the caller reports as not revoked.
    """
    enforcement = target.enforcement
    assert enforcement is not None
    statements = revoke_statements(target.full_name, enforcement.restricted)
    if not statements:
        say(f"    fail-safe: {target.full_name} restricts no identity on Databricks; none revoked.")
        return []
    say(
        f"    fail-safe: revoking SELECT on {target.full_name} from "
        f"{', '.join(enforcement.restricted)}, because their mask or row filter is not in "
        "place and a later dbt run could expose unmasked rows to them:"
    )
    unrevoked: List[str] = []
    for statement in statements:
        try:
            runner.run(statement)
        except StatementFailed as exc:
            unrevoked.append(statement)
            say(
                f"    NOT REVOKED: {statement} failed ({exc}). Run it yourself, now: until it "
                "runs, that principal can read the table without its mask or filter."
            )
        else:
            say(f"      revoked: {statement}")
    return unrevoked


__all__ = [
    "DATABRICKS_EXTRA_INSTALL",
    "WAREHOUSE_ENV",
    "EnforcementError",
    "SqlRunner",
    "StatementFailed",
    "Step",
    "Target",
    "TablePlan",
    "plan_table",
    "post_apply",
    "pre_apply",
    "preview",
    "read_table",
]
