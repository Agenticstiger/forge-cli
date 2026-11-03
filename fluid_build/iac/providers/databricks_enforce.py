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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from ..base import UnsupportedBindingError
from .databricks_access import (
    MASK_PREFIX,
    ROW_FILTER_PREFIX,
    Function,
    TableEnforcement,
    is_forge_function,
    quote_principal,
    quote_table,
    revoke_all_statement,
    revoke_statements,
    same_function,
    unbind_mask,
    unbind_row_filter,
)
from .databricks_access import (
    quote as quote_ident,
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


class ForeignBinding(EnforcementError):
    """The table has a row filter or mask this module did not create, where the contract asks
    for its own: setting ours would silently replace it."""


class StatementFailed(Exception):
    """One SQL statement did not succeed; the message is the server's, scrubbed."""


class FunctionMismatch(StatementFailed):
    """A function under a content-hashed name is not the one this module would create."""


@dataclass(frozen=True)
class Step:
    """One statement, and what it is for."""

    statement: str
    what: str
    #: Lower-case columns of the table the statement refers to; it waits until they exist.
    needs: Tuple[str, ...] = ()
    #: Drops a mask or filter: it widens access.
    widens: bool = False
    #: A step with no statement: read this function back and check it is the expected one
    #: (:func:`function_problem`) before anything binds it.
    function: Optional[Function] = None


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


def read_function(client: Any, full_name: str) -> Optional[Dict[str, Any]]:
    """``GET /api/2.1/unity-catalog/functions/{full_name}`` (FunctionInfo), or ``None`` if absent."""
    try:
        out = client.api_client.do(
            "GET", f"/api/2.1/unity-catalog/functions/{quote(full_name, safe='.')}"
        )
    except Exception as exc:  # noqa: BLE001 — a 404 is an answer, anything else is raised on
        if _is_not_found(exc):
            return None
        raise
    return out if isinstance(out, dict) else {}


def deploy_identity(client: Any) -> str:
    """The identity this run authenticates as: ``userName`` of ``GET /api/2.0/preview/scim/v2/Me``.

    That is who ``CREATE FUNCTION`` makes the owner (a user's email, a service principal's
    application ID), and the credential the provider and this module share, so it is the one
    owner a function under a hashed name may have.
    """
    me = client.api_client.do("GET", "/api/2.0/preview/scim/v2/Me")
    name = str(me.get("userName") or "") if isinstance(me, Mapping) else ""
    if not name:
        raise StatementFailed("the workspace did not say which identity this run is")
    return name


def _squash(text: Any) -> str:
    return " ".join(str(text or "").split())


def _type(text: Any) -> str:
    return "".join(str(text or "").split()).lower()


def owner_matches(owner: Any, owners: Collection[str]) -> bool:
    """Whether a function's ``owner`` is one of ``owners`` (case-insensitive): the one comparison
    the read-back of a function and the orphan drop share."""
    return str(owner or "").lower() in {o.lower() for o in owners}


def function_problem(
    info: Optional[Mapping[str, Any]], expected: Function, owners: Collection[str]
) -> Optional[str]:
    """Why the live function is not ``expected``, or ``None`` when it is.

    A content-hashed name can be computed from the contract, so anyone who can create a
    function in the schema can create one under it first, and ``CREATE FUNCTION IF NOT
    EXISTS`` would keep theirs. Owner, body, parameters and return type must all be the
    expected ones, whitespace-normalised (FunctionInfo: ``owner``, ``routine_definition``,
    ``input_params.parameters[]``, ``full_data_type``; databricks-sdk-go v0.190.0).
    """
    if info is None:
        return f"{expected.full_name} does not exist"
    problems: List[str] = []
    owner = str(info.get("owner") or "")
    if not owner_matches(owner, owners):
        problems.append(f"its owner is {owner or 'unknown'}, not {' or '.join(sorted(owners))}")
    body_language = str(info.get("routine_body") or "SQL").upper()
    if body_language != "SQL":
        problems.append(f"it is a {body_language} function, not SQL")
    if _squash(info.get("routine_definition")) != _squash(expected.body):
        problems.append(
            f"its body is {_squash(info.get('routine_definition'))!r}, expected "
            f"{_squash(expected.body)!r}"
        )
    live_params = [
        (str(p.get("name") or "").lower(), _type(p.get("type_text") or p.get("type_name")))
        for p in ((info.get("input_params") or {}).get("parameters") or [])
        if isinstance(p, Mapping)
    ]
    want_params = [(n.lower(), _type(t)) for n, t in expected.params]
    if live_params != want_params:
        problems.append(f"its parameters are {live_params}, expected {want_params}")
    live_returns = _type(info.get("full_data_type") or info.get("data_type"))
    if live_returns != _type(expected.returns):
        problems.append(
            f"it returns {live_returns or 'nothing'}, expected {_type(expected.returns)}"
        )
    if not problems:
        return None
    return f"{expected.full_name} is not the function this module creates: " + "; ".join(problems)


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
        self.execute(statement)

    def execute(self, statement: str) -> Mapping[str, Any]:
        """:meth:`run`, and the finished statement's document (its ``result`` and ``manifest``)."""
        do = self._client.api_client.do
        doc: Any = None
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
        except BaseException:
            # Ctrl-C or SystemExit while the statement runs: it would go on running on the
            # warehouse after this process has gone.
            if isinstance(doc, Mapping) and doc.get("statement_id"):
                self._cancel(doc)
            raise
        state = self._state(doc)
        if state != "SUCCEEDED":
            error = (doc.get("status") or {}).get("error") or {}
            raise StatementFailed(
                f"{state}: {_scrub(error.get('message') or 'no message from the server')}"
            )
        return doc

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


def _verify_step(function: Function, needs: Tuple[str, ...]) -> Step:
    return Step(
        "",
        f"check that {function.full_name} is the function this module creates (owner, body, "
        "parameters, return type)",
        needs,
        function=function,
    )


def _foreign(table: str, kind: str, live: str, column: Optional[str]) -> ForeignBinding:
    where = f"{table}.{column}" if column else table
    drop = (
        f"ALTER TABLE {quote_table(table)} ALTER COLUMN {quote_ident(column)} DROP MASK"
        if column
        else unbind_row_filter(table)
    )
    contract_field = "policy.authz.columnRestrictions" if column else "policy.authz.rowFilters"
    return ForeignBinding(
        f"{where} has a {kind}, {live}, that this module did not create (its name has no "
        "fluid_ prefix), and the contract asks for its own: binding it would replace "
        "that one silently. Nothing was changed.",
        (
            f"Remove it ({drop}) so the contract's {kind} takes its place, or move its rule into "
            f"the contract ({contract_field}) and remove it, then re-run fluid apply.",
        ),
    )


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
            if live_filter and not is_forge_function(live_filter, ROW_FILTER_PREFIX):
                raise _foreign(full_name, "row filter", live_filter, None)
            needs = tuple(c.lower() for c in spec.columns)
            plan.bind.append(
                Step(
                    spec.function.create,
                    f"create row filter function {spec.function.full_name}",
                    needs,
                )
            )
            plan.bind.append(_verify_step(spec.function, needs))
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
            live_function = str(live_mask.get("function_name") or "")
            if live_function and not is_forge_function(live_function, MASK_PREFIX):
                raise _foreign(full_name, "column mask", live_function, mask.column)
            needs = (mask.column.lower(),)
            plan.bind.append(
                Step(
                    mask.function.create,
                    f"create mask function {mask.function.full_name}",
                    needs,
                )
            )
            plan.bind.append(_verify_step(mask.function, needs))
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


def interim_steps(
    enforcement: Optional[TableEnforcement],
    plan: TablePlan,
    live: Optional[Mapping[str, Any]],
    full_name: str,
) -> List[Step]:
    """The deny-all row filter to bind while the real one waits for a column the apply adds.

    The table exists and holds rows, so until ``post_apply`` binds the real filter the
    restricted identities would read every row. The interim filter is the fail-safe's own
    (:func:`deny_all_steps`); ``post_apply`` replaces it. None when the real filter can bind now,
    or the deny-all one is already bound.
    """
    if live is None or enforcement is None:
        return []  # a table that does not exist has no rows to hide yet
    if enforcement.row_filter is None or enforcement.deny_all is None:
        return []
    real = enforcement.row_filter
    waiting = [s for s in plan.bind if s.statement == real.bind and not _ready(s, live)]
    if not waiting:
        return []
    current = ((live or {}).get("row_filter") or {}).get("function_name")
    if same_function(current, enforcement.deny_all.function.full_name):
        return []
    return deny_all_steps(enforcement, full_name)


def _ready(step: Step, live: Optional[Mapping[str, Any]]) -> bool:
    columns = _live_columns(live or {})
    return live is not None and all(c in columns for c in step.needs)


class Session:
    """The client, the warehouse runner and, read once, the identity this run is."""

    def __init__(self, client: Any, runner: SqlRunner):
        self.client = client
        self.runner = runner
        self._identity: Optional[str] = None

    @property
    def identity(self) -> str:
        if self._identity is None:
            self._identity = deploy_identity(self.client)
        return self._identity

    def run(self, step: Step) -> None:
        """Run a step: a statement on the warehouse, or the check of a function."""
        if step.function is None:
            self.runner.run(step.statement)
            return
        try:
            info = read_function(self.client, step.function.full_name)
            problem = function_problem(info, step.function, {self.identity})
        except StatementFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FunctionMismatch(
                f"could not read {step.function.full_name}: {type(exc).__name__}: {_scrub(exc)}"
            ) from exc
        if problem is not None:
            raise FunctionMismatch(
                f"{problem}. It was not bound: a function someone else made under this name "
                "would otherwise decide who reads the table"
            )


# ── the hooks ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    full_name: str
    enforcement: Optional[TableEnforcement]


def _client_for(
    targets: Sequence[Target], factory: Optional[ClientFactory], say: Say
) -> Optional[Session]:
    """A client and runner, or ``None`` (with a note) when only removals could be checked.

    A contract with a restriction needs both, and fails without them; one without can only
    be dropping what an earlier apply bound, which is access narrowing left in place, so
    an unreachable workspace is a note, not an error.
    """
    required = any(t.enforcement is not None for t in targets)
    # Without the SDK a contract that restricts nothing has no apply-time use for it, and an
    # earlier restriction left in place only narrows access: silent. Any other failure to get a
    # client is a note, since a removal could not be checked.
    try:
        client = (factory or default_client)()
        warehouse = str(os.environ.get(WAREHOUSE_ENV) or "").strip()
        if not warehouse:
            raise EnforcementError(
                f"{WAREHOUSE_ENV} is not set, and the masks and row filters run on that warehouse.",
                (f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace.",),
            )
        return Session(client, SqlRunner(client, warehouse))
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
        return None  # no restriction was ever asked for: there is nothing to say
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
    client = pair.client
    lines: List[Tuple[TablePlan, List[Tuple[str, Step]]]] = []
    for target in targets:
        live = _read(client, target)
        plan = plan_table(target.full_name, target.enforcement, live)
        steps = [
            ("before apply", s)
            for s in interim_steps(target.enforcement, plan, live, target.full_name)
        ]
        steps += [("before apply" if _ready(s, live) else "after apply", s) for s in plan.bind]
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
            if step.statement:
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
    client, runner = pair.client, pair
    # Read and plan every table first: a refusal (a binding this module did not create) must
    # come before any change, on any table.
    work: List[Tuple[Target, List[Step]]] = []
    for target in wanted:
        live = _read(client, target)
        if live is None:
            continue  # created by this apply: post_apply binds it, empty
        plan = plan_table(target.full_name, target.enforcement, live)
        steps = [s for s in plan.bind if _ready(s, live)]
        interim = interim_steps(target.enforcement, plan, live, target.full_name)
        if interim:
            steps = [*interim, *steps]
        if steps:
            work.append((target, steps))
    for target, steps in work:
        say(f"\n  {target.full_name}: binding masks and row filter before the apply")
        for step in steps:
            try:
                runner.run(step)
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
    targets: Sequence[Target],
    say: Say,
    client_factory: Optional[ClientFactory] = None,
    *,
    after_failed_apply: bool = False,
) -> None:
    """Bind what is still missing, drop what the contract no longer asks, fail safe.

    ``after_failed_apply`` is the bind-only mode for a ``tofu apply`` that failed part way: a
    table it created, its grants and the job that fills it can exist with no mask or filter, and
    a scheduled run would expose unmasked rows. It binds what exists, revokes SELECT where it
    cannot bind, skips a table that does not exist (expected after a failed apply) and drops
    nothing: dropping widens access and waits for an apply that succeeded.
    """
    if after_failed_apply:
        targets = [t for t in targets if t.enforcement is not None]
    if not targets:
        return
    pair = _client_for(targets, client_factory, say)
    if pair is None:
        return
    client, runner = pair.client, pair
    failed: List[Tuple[Target, str, FailSafe]] = []
    for target in targets:
        try:
            live = _read(client, target)
        except EnforcementError as exc:
            # A transient 429 or 503 on one table must not leave it granted and unbound, nor
            # stop the others from being tried: it is a failed bind for that table.
            if target.enforcement is None:
                say(
                    f"  note: {target.full_name} could not be read, so its earlier masks and "
                    "row filter were not checked for removal; any left in place keep restricting."
                )
                continue
            say(f"    FAILED: {exc}")
            failed.append((target, str(exc), _fail_safe(runner, target, say)))
            continue
        if live is None:
            if after_failed_apply:
                say(f"  {target.full_name}: not created by the failed apply; nothing to bind.")
            elif target.enforcement is not None:
                why = "the table does not exist after the apply"
                failed.append((target, why, _fail_safe(runner, target, say)))
            continue
        try:
            plan = plan_table(target.full_name, target.enforcement, live)
        except ForeignBinding as exc:
            say(f"    FAILED: {exc}")
            failed.append((target, str(exc), _fail_safe(runner, target, say, live=live)))
            continue
        if after_failed_apply:
            plan.drop = []
        if plan.bind or plan.drop:
            say(f"\n  {target.full_name}: masks and row filter")
        for step in [*plan.bind, *plan.drop]:
            if step.widens:
                say(f"    access widened (no data lost, not gated): {step.what}")
            try:
                runner.run(step)
            except StatementFailed as exc:
                reason = f"{step.what} failed: {exc}. The statement was: {step.statement}"
                say(f"    FAILED: {reason}")
                if target.enforcement is not None:
                    failed.append((target, reason, _fail_safe(runner, target, say, live=live)))
                break
            say(f"    done: {step.what}")
    if failed:
        names = ", ".join(sorted({t.full_name for t, _, _ in failed}))
        raise EnforcementError(
            (
                "the apply failed part way, and "
                if after_failed_apply
                else "the apply succeeded, but "
            )
            + f"the masks or row filter of {names} could not be put in place. "
            + " ".join(f"{t.full_name}: {why}. {_describe(t, fs)}" for t, why, fs in failed),
            (
                "Fix the cause, then re-run fluid apply: it binds the masks and row filter "
                "first (replacing a deny-all filter) and grants again afterwards.",
            ),
        )
    if not after_failed_apply:
        drop_orphan_functions(runner, targets, say)


def _list_named(client: Any, path: str, query: Mapping[str, str], key: str) -> List[Dict[str, Any]]:
    """Every entry of a paged Unity Catalog list (``key`` holds the page's rows)."""
    out: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(100):
        page = client.api_client.do(
            "GET", path, query={**query, **({"page_token": token} if token else {})}
        )
        out += [r for r in (page.get(key) or []) if isinstance(r, Mapping)]
        token = page.get("next_page_token") or None
        if not token:
            return out
    raise StatementFailed("the list never ended")


def _bare(name: Any) -> str:
    return str(name or "").replace("`", "").lower()


def _orphans(
    names: Sequence[str], table: str, columns: Collection[str], keep: Collection[str]
) -> List[str]:
    """The functions among ``names`` (bare, lower case) that carry this table's fluid_ name
    pattern and are not in ``keep``: ``fluid_rf_<table>_<8 hex>``, the deny-all
    ``fluid_rf_<table>_deny_all_<8 hex>`` and ``fluid_mask_<table>_<column>_<8 hex>`` for a column
    the table has."""
    t = re.escape(table.lower())
    shapes = [
        f"{ROW_FILTER_PREFIX}{t}_[0-9a-f]{{8}}",
        f"{ROW_FILTER_PREFIX}{t}_deny_all_[0-9a-f]{{8}}",
    ]
    shapes += [f"{MASK_PREFIX}{t}_{re.escape(c.lower())}_[0-9a-f]{{8}}" for c in columns]
    rx = re.compile("|".join(f"(?:{x})" for x in shapes))
    return [n for n in names if n.startswith("fluid_") and rx.fullmatch(n) and n not in keep]


def drop_orphan_functions(session: Session, targets: Sequence[Target], say: Say) -> None:
    """Drop this module's functions that a changed contract left behind, after a successful apply.

    A function is dropped only when its name is this table's fluid_ pattern
    (:func:`_orphans`), it is not one the contract expects now, and no table in its schema has it
    as a row filter or column mask (every table of the schema is read first). If the schema
    cannot be read completely, nothing is dropped in it. Each drop is printed.
    """
    schemas: Dict[Tuple[str, str], List[Target]] = {}
    for target in targets:
        parts = target.full_name.split(".")
        if len(parts) == 3:
            schemas.setdefault((parts[0], parts[1]), []).append(target)
    expected = set()
    for target in targets:
        e = target.enforcement
        if e is None:
            continue
        expected |= {_bare(m.function.full_name) for m in e.masks}
        if e.row_filter is not None:
            expected.add(_bare(e.row_filter.function.full_name))
        if e.deny_all is not None:
            expected.add(_bare(e.deny_all.function.full_name))
    client = session.client
    for (catalog, schema), members in sorted(schemas.items()):
        where = f"{catalog}.{schema}"
        try:
            query = {"catalog_name": catalog, "schema_name": schema}
            bound: set = set()
            columns: Dict[str, List[str]] = {}
            for row in _list_named(client, "/api/2.1/unity-catalog/tables", query, "tables"):
                full = str(row.get("full_name") or f"{where}.{row.get('name')}")
                live = read_table(client, full)
                if live is None:
                    continue
                bound.add(_bare((live.get("row_filter") or {}).get("function_name")))
                live_cols = _live_columns(live)
                bound |= {
                    _bare((c.get("mask") or {}).get("function_name")) for c in live_cols.values()
                }
                columns[full.rsplit(".", 1)[-1].lower()] = [
                    str(c["name"]) for c in live_cols.values()
                ]
            listed = _list_named(client, "/api/2.1/unity-catalog/functions", query, "functions")
        except Exception as exc:  # noqa: BLE001 — unknown binding: drop nothing
            say(
                f"  note: orphan functions in {where} not dropped: its tables or functions could "
                f"not be read ({type(exc).__name__}: {_scrub(exc)})."
            )
            continue
        names = [
            str(r.get("name") or str(r.get("full_name") or "").rsplit(".", 1)[-1]).lower()
            for r in listed
        ]
        mine = f"{where}.".lower()
        keep = {n[len(mine) :] for n in expected | bound if n.startswith(mine)}
        dropped: List[str] = []
        for target in members:
            table = target.full_name.split(".")[2]
            for name in _orphans(names, table, columns.get(table.lower(), ()), keep):
                if name in dropped:
                    continue
                full = f"{where}.{name}"
                try:
                    owner = str((read_function(client, full) or {}).get("owner") or "")
                    me = session.identity
                except Exception:  # noqa: BLE001 — unknown owner: leave it
                    owner, me = "", ""
                if not me or not owner_matches(owner, {me}):
                    why = (
                        f"owned by {owner}, not by the deploy identity"
                        if owner
                        else "owner unknown"
                    )
                    say(f"  left function {full}: {why}")
                    dropped.append(name)  # said once; not tried again for another table
                    continue
                try:
                    session.runner.run(f"DROP FUNCTION IF EXISTS {quote_table(full)}")
                except StatementFailed as exc:
                    say(f"  note: superseded function {full} was not dropped: {exc}")
                    continue
                dropped.append(name)
                say(f"  dropped superseded function {full}")


@dataclass
class FailSafe:
    """What the fail-safe did for one table that could not be bound, and what it could not close."""

    #: The deny-all row filter is bound: the restricted identities read no rows, by any grant path.
    locked: bool = False
    #: The contract restricts no identity on Databricks: nothing to bind or revoke.
    nothing_to_restrict: bool = False
    #: Why the deny-all filter was not bound (it failed, or it would replace a filter not made here).
    deny_all_problem: str = ""
    revoked: List[str] = field(default_factory=list)
    unrevoked: List[str] = field(default_factory=list)
    #: Grant paths a table-level REVOKE cannot close, each with what to run.
    still_open: List[str] = field(default_factory=list)
    #: Whether the inherited grants could be listed at all.
    checked: bool = False
    #: Whether the table's grants could be listed (else only the restricted identities were revoked).
    listed: bool = True
    #: Whether any effective-permission entry said where a privilege comes from
    #: (``inherited_from_*``); if none did, "nothing inherited is open" cannot be told from
    #: "the listing does not say".
    inheritance_visible: bool = False
    #: The table's owner could not be read, so the lock could not skip it (only this run's own
    #: identity was skipped).
    owner_unknown: bool = False


_READ = frozenset({"SELECT", "ALL_PRIVILEGES"})


def deny_all_steps(enforcement: TableEnforcement, full_name: str) -> List[Step]:
    """Create, check and bind the zero-column deny-all row filter for the restricted identities."""
    spec = enforcement.deny_all
    assert spec is not None
    return [
        Step(
            spec.function.create, f"create deny-all row filter function {spec.function.full_name}"
        ),
        _verify_step(spec.function, ()),
        Step(spec.bind, f"bind deny-all row filter {spec.function.full_name} on {full_name}"),
    ]


def _list_pages(client: Any, path: str) -> List[Dict[str, Any]]:
    """Every privilege assignment the permissions API lists for a securable, following pages."""
    out: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(50):
        page = client.api_client.do("GET", path, query={"page_token": token} if token else None)
        out += [a for a in (page.get("privilege_assignments") or []) if isinstance(a, Mapping)]
        token = page.get("next_page_token") or None
        if not token:
            return out
    raise StatementFailed("the permission list never ended")


def _privileges(entry: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for priv in entry.get("privileges") or []:
        detail = priv if isinstance(priv, Mapping) else {"privilege": priv}
        if detail.get("privilege"):
            out[str(detail["privilege"]).upper()] = detail
    return out


def unprotected(
    targets: Sequence[Target], client_factory: Optional[ClientFactory] = None
) -> Dict[str, str]:
    """``{table: why}`` for each table with restrictions that is not yet protected as the
    contract expects, read live; every such table when the live state cannot be read.

    Protected means the table exists, its row filter and masks are the expected functions bound
    on the expected columns, and each of those functions is the one this module creates (owner,
    body, parameters, return type). A dbt job that writes a table that is not yet protected is
    created paused (``DatabricksIacPlugin.emit``), so it cannot fill it first. Fails closed: no
    SDK, no credential or a failed read all count as not protected.
    """
    wanted = [t for t in targets if t.enforcement is not None]
    if not wanted:
        return {}
    quiet: List[str] = []
    try:
        session = _client_for(wanted, client_factory, quiet.append)
    except Exception as exc:  # noqa: BLE001 — unknown is unprotected
        return {
            t.full_name: f"the live state could not be read ({type(exc).__name__})" for t in wanted
        }
    if session is None:
        return {t.full_name: "the live state could not be read" for t in wanted}
    out: Dict[str, str] = {}
    for target in wanted:
        enforcement = target.enforcement
        assert enforcement is not None
        try:
            live = read_table(session.client, target.full_name)
            if live is None:
                out[target.full_name] = "the table does not exist yet"
                continue
            plan = plan_table(target.full_name, enforcement, live)
            if plan.bind:
                out[target.full_name] = "its masks or row filter are not bound as the contract says"
                continue
            functions = [m.function for m in enforcement.masks]
            if enforcement.row_filter is not None:
                functions.append(enforcement.row_filter.function)
            for fn in functions:
                problem = function_problem(
                    read_function(session.client, fn.full_name), fn, {session.identity}
                )
                if problem is not None:
                    out[target.full_name] = problem
                    break
        except Exception as exc:  # noqa: BLE001 — a failed read is not protection
            out[target.full_name] = f"it could not be checked ({type(exc).__name__})"
    return out


def _fail_safe(
    session: Session, target: Target, say: Say, *, live: Optional[Mapping[str, Any]] = None
) -> FailSafe:
    """Close the table to the restricted identities after a failed bind, and say exactly how.

    First a deny-all row filter bound for them (:func:`deny_all_steps`): it blocks them whatever
    grant path gives them the table, a group they are in, a schema or a catalog grant. If that
    cannot be bound (it failed, or the table has a row filter this module did not create, which
    it would replace), SELECT is revoked on the table from every grantee but its owner, listed
    through the permissions API. A table-level REVOKE cannot close a grant on the schema or the
    catalog: those are listed, with what to run.
    """
    enforcement = target.enforcement
    assert enforcement is not None
    result = FailSafe()
    table = target.full_name
    if not enforcement.restricted or enforcement.deny_all is None:
        result.nothing_to_restrict = True
        say(f"    fail-safe: {table} restricts no identity on Databricks; nothing to close.")
        return result
    if live is None:
        try:
            live = read_table(session.client, table)
        except Exception:  # noqa: BLE001 — unreadable: the filter that is there is unknown
            live = None
    current = ((live or {}).get("row_filter") or {}).get("function_name") or ""
    if live is None:
        result.deny_all_problem = "the table could not be read, so a filter on it is unknown"
    elif current and not is_forge_function(current, ROW_FILTER_PREFIX):
        result.deny_all_problem = (
            f"it would replace {current}, a row filter this module did not create"
        )
    else:
        say(
            f"    fail-safe: binding a deny-all row filter on {table} for "
            f"{', '.join(enforcement.restricted)}:"
        )
        try:
            for step in deny_all_steps(enforcement, table):
                session.run(step)
        except StatementFailed as exc:
            result.deny_all_problem = f"it failed: {exc}"
            say(f"    deny-all row filter {result.deny_all_problem}")
        else:
            result.locked = True
            say(f"      done: {enforcement.deny_all.function.full_name} is bound on {table}")
            return result
    say(f"    fail-safe: no deny-all row filter ({result.deny_all_problem}); locking {table}.")
    _lock(session, table, live, enforcement, result, say)
    return result


def _lock(
    session: Session,
    table: str,
    live: Optional[Mapping[str, Any]],
    enforcement: TableEnforcement,
    result: FailSafe,
    say: Say,
) -> None:
    """Revoke SELECT (and ALL PRIVILEGES) on the table from every grantee but its owner and the
    identity this run is: revoking from the deploy identity would lock the run out of the table
    it has to repair."""
    owner = str((live or {}).get("owner") or "")
    result.owner_unknown = not owner
    try:
        me = session.identity
    except Exception:  # noqa: BLE001 — unreadable: nothing to skip but the owner
        me = ""
    skip = {p.lower() for p in (owner, me) if p}
    grantees: Dict[str, set] = {}
    try:
        for entry in _list_pages(
            session.client, f"/api/2.1/unity-catalog/permissions/table/{quote(table, safe='.')}"
        ):
            held = set(_privileges(entry)) & _READ
            if entry.get("principal") and held:
                grantees[str(entry["principal"])] = held
    except Exception as exc:  # noqa: BLE001
        result.listed = False
        say(
            f"    could not list the grants on {table} ({type(exc).__name__}): revoking only from "
            f"{', '.join(enforcement.restricted)}"
        )
        grantees = {p: {"SELECT"} for p in enforcement.restricted}
    for principal, held in sorted(grantees.items()):
        if principal.lower() in skip:
            continue
        statements = []
        if "SELECT" in held:
            statements += revoke_statements(table, [principal])
        if "ALL_PRIVILEGES" in held:
            statements.append(revoke_all_statement(table, principal))
        for statement in statements:
            try:
                session.runner.run(statement)
            except StatementFailed as exc:
                result.unrevoked.append(statement)
                say(f"    NOT REVOKED: {statement} failed ({exc})")
            else:
                result.revoked.append(statement)
                say(f"      revoked: {statement}")
    restricted = {p.lower() for p in enforcement.restricted}
    for held_by in (owner, me):
        if held_by and held_by.lower() in restricted:
            what = "owns" if held_by == owner else "is the identity of this run, and has"
            result.still_open.append(
                f"{held_by} {what} {table} and the contract restricts it; an owner reads its "
                "table whatever is revoked, so this lock cannot close that path. Run: ALTER TABLE "
                f"{'.'.join(quote_ident(p) for p in table.split('.'))} OWNER TO "
                "`<an identity the contract does not restrict>`"
            )
    _open_paths(session, table, owner, result)


def _open_paths(session: Session, table: str, owner: str, result: FailSafe) -> None:
    """What still lets someone read the table: a grant inherited from its schema or catalog."""
    try:
        entries = _list_pages(
            session.client,
            f"/api/2.1/unity-catalog/effective-permissions/table/{quote(table, safe='.')}",
        )
    except Exception:  # noqa: BLE001
        result.checked = False
        return
    result.checked = True
    result.inheritance_visible = any(
        str(k).startswith("inherited_from")
        for entry in entries
        for detail in _privileges(entry).values()
        for k in detail
    )
    for entry in entries:
        principal = str(entry.get("principal") or "")
        if not principal or (owner and principal.lower() == owner.lower()):
            continue
        for priv, detail in _privileges(entry).items():
            source = str(detail.get("inherited_from_name") or "")
            if priv not in _READ or not source:
                continue
            kind = str(detail.get("inherited_from_type") or "SCHEMA").upper()
            target_sql = ".".join(quote_ident(part) for part in source.split("."))
            verb = "ALL PRIVILEGES" if priv == "ALL_PRIVILEGES" else priv
            result.still_open.append(
                f"{principal} holds {priv} on {kind} {source}, which a table-level REVOKE cannot "
                f"close; run: REVOKE {verb} ON {kind} {target_sql} FROM {quote_principal(principal)}"
            )


def _describe(target: Target, fs: FailSafe) -> str:
    """The fail-safe, in words: exactly what was done, and every path still open."""
    if fs.nothing_to_restrict:
        return "The contract restricts no identity on Databricks, so nothing was bound or revoked."
    if fs.locked:
        assert target.enforcement is not None and target.enforcement.deny_all is not None
        return (
            f"A deny-all row filter ({target.enforcement.deny_all.function.full_name}) is bound: "
            f"{', '.join(target.enforcement.restricted)} read no rows of it, whatever grant gives "
            "them the table. SELECT was not revoked from anyone."
        )
    done = f"The deny-all row filter could not be bound ({fs.deny_all_problem}). " + (
        "SELECT (and ALL PRIVILEGES where held) was revoked on the table from "
        + ", ".join(sorted({s.rsplit(" FROM ", 1)[1] for s in fs.revoked}))
        + " (every grantee but its owner and this run's own identity). "
        if fs.revoked
        else "Nothing was revoked. "
    )
    if fs.owner_unknown:
        done += (
            "The table's owner could not be read, so it could not be skipped: if the owner held "
            "SELECT through a grant, it was revoked too. "
        )
    if not fs.listed:
        done += "Its grants could not be listed, so only the restricted identities were revoked. "
    if fs.unrevoked:
        done += (
            "These could NOT be revoked, run them yourself, now: " + "; ".join(fs.unrevoked) + ". "
        )
    if fs.still_open:
        done += "Still open: " + " | ".join(fs.still_open) + ". "
    elif fs.checked and fs.inheritance_visible:
        done += "No grant inherited from the schema or the catalog was found open. "
    elif fs.checked:
        done += (
            "The permission listing did not say where any privilege comes from, so it could not "
            "determine whether a schema or catalog grant is open: check them. "
        )
    else:
        done += "Grants inherited from the schema or the catalog could not be checked: check them. "
    return done.rstrip()


__all__ = [
    "DATABRICKS_EXTRA_INSTALL",
    "WAREHOUSE_ENV",
    "EnforcementError",
    "ForeignBinding",
    "FailSafe",
    "FunctionMismatch",
    "Session",
    "SqlRunner",
    "StatementFailed",
    "Step",
    "Target",
    "TablePlan",
    "deny_all_steps",
    "interim_steps",
    "deploy_identity",
    "function_problem",
    "plan_table",
    "post_apply",
    "pre_apply",
    "preview",
    "read_function",
    "read_table",
    "unprotected",
]
