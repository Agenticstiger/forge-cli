# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A data product's Databricks AI/BI Genie space: compiled from its contract, kept in step by
``fluid apply``.

A contract opts in with ``extensions.databricksGenie`` (``{}``, or ``{title: ..., parentPath:
...}``). The product gets one space holding its Databricks tables and any metric view over them,
where people ask the product's own example questions in natural language.

What the space holds (``serialized_space`` version 2, the body ``POST /api/2.0/genie/spaces``
takes; field rules from databricks/databricks-agent-skills ``databricks-genie-agents``
``references/create-genie-agent.md`` and the Genie API reference, checked against a live
workspace on 2026-11-12):

* ``config.sample_questions``: each expose's ``semantics.labels.ai-example-questions``, split on
  ``|``.
* ``data_sources.tables``: one per Databricks expose, with its description and, per column, the
  description, business definition, allowed values (an ``enum`` validation rule), range, example
  and synonyms (``businessName``, ``labels.synonyms``). Genie learns values from the contract,
  not by sampling the data.
* every metric view whose ``source`` is one of those tables, read from Unity Catalog at apply
  time, so one is used as soon as it exists, whoever made it. It goes among
  ``data_sources.tables``: the service keeps it there, and moves one sent under
  ``data_sources.metric_views`` there.
* ``instructions.text_instructions`` (Genie allows one): ``ai-instructions``, ``ai-synonyms``, the
  grain, the key, the time dimension and snapshot rule, each metric as the product defines it,
  the products it is built from (``consumes``), the expose's ``agentPolicy`` (allowed and denied
  use cases, purpose limitation, ``canStore``, ``canReason``), its classification, and how access
  works.
* ``instructions.sql_snippets.measures``: each simple metric as SQL, in a one-table space (an
  expression over two tables' columns would need qualifying, which this module does not do). The
  SQL is rendered as the governed MCP query path renders it
  (``output_ports/mcp/query_compiler.py``), so both answer the same number for one contract.

A space never widens access. Genie runs each query as the person asking, under that person's own
Unity Catalog grants, so column masks and row filters apply ("Genie agents: data access",
Databricks docs). What could still show data past those grants is not done: prompt matching
(format assistance and entity matching) samples column values with the author's permissions and
shows them to everyone in the space, so it is never turned on (a space the API creates has it
off, and ``agentPolicy.canStore: false`` forbids the cache besides); and the space is shared,
with ``CAN_RUN``, only with the principals that hold ``SELECT`` on one of its tables. A space
for a product whose ``agentPolicy`` rules out question answering is refused.

Identity, without state. The databricks/databricks provider has no Genie space resource (v1.142.0
ships only the Private Preview ``databricks_genie_agent``, a typed definition of another shape),
so the space lives outside OpenTofu. Its description ends with ``[fluid-genie:<product id>]``:
each apply lists the spaces, adopts the one carrying the marker in the deploying identity's own
folder (where the API puts a space it creates), updates it (``PATCH`` with the space's ``etag``)
only when what the contract asks differs from what it holds, and trashes it once the contract
drops the block. A space carrying the marker anywhere else is left alone with a note, so a copy
someone plants is never filled, shared to the readers or trashed. No local record, so a fresh CI
runner finds the same space; two spaces carrying one product's marker stop the apply.

Deployed from more than one identity (a laptop and CI), each would make its own space in its
own folder, so ``parentPath`` names one workspace folder for the space instead. A space inherits
its folder's permissions (measured), so the folder's permissions become the trust boundary:
whoever can edit or manage the folder can edit the space and make one there carrying the marker.
So the apply adopts only a space directly in that folder, and refuses a folder in which the
``users`` group (every workspace user) holds ``CAN_EDIT`` or ``CAN_MANAGE``: all of ``/Shared``,
which grants it ``CAN_MANAGE``, refused offline. A space an earlier apply made in the identity's
own folder is moved into the named one (``PATCH parent_path``, which keeps its conversations), and
a copy left in it once the product's space is there is trashed.

Measured on a live workspace (2026-11-12) and handled: the service keeps a space as sent except
that it leaves out a ``false`` flag (never sent, so a second apply compares equal); a new space
shows in the list some seconds after it is made (a create waits for it, or an apply straight
after would make a second); and a title another space in the folder already has comes back
suffixed with a timestamp (kept, since a ``PATCH`` to a taken title is 409).

The client is the ``databricks.sdk.WorkspaceClient`` the masks and row filters use
(``databricks_enforce.default_client``), through ``api_client.do``. The paths are the SDK's own
(databricks-sdk-py ``service/dashboards.py`` and ``service/iam.py``). No message carries a
credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import quote

from ...providers._sql_safety import validate_ident, validate_sql_expression_allowlist
from ..base import UnsupportedBindingError
from . import databricks_enforce as _enforce

#: The contract's opt-in: ``extensions.databricksGenie``.
EXTENSION_KEY = "databricksGenie"
_BLOCK_KEYS = frozenset({"title", "parentPath"})

SPACES_PATH = "/api/2.0/genie/spaces"
PERMISSIONS_PATH = "/api/2.0/permissions/genie/{}"
WORKSPACE_STATUS_PATH = "/api/2.0/workspace/get-status"
FOLDER_PERMISSIONS_PATH = "/api/2.0/permissions/directories/{}"
#: The group every workspace user is in, as the Permissions API names it.
ALL_USERS = "users"
#: Every group that holds every user: the workspace's own, and the account's (granted on a
#: workspace object under identity federation).
ALL_USERS_GROUPS = (ALL_USERS, "account users")
#: The folder that grants :data:`ALL_USERS` ``CAN_MANAGE`` directly; every folder and space under
#: it inherits that (measured on a live workspace, 2026-11-12), and a workspace permission
#: cannot be taken away below the folder that grants it ("objects in a folder inherit all
#: permissions settings of that folder", Databricks access control docs).
SHARED_FOLDER = "/Shared"
TABLES_PATH = "/api/2.1/unity-catalog/tables"
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
DATABRICKS_EXTRA_INSTALL = "pip install 'data-product-forge[databricks]'"

#: "Up to 50 tables, views or metric views" per space ("Set up a Genie agent", Databricks docs).
MAX_TABLES = 50
#: Each string in ``serialized_space`` holds at most 25,000 characters (Genie API reference).
MAX_STRING = 25_000
#: What a reader holds on the space: ask questions. Genie's levels (``GET
#: /api/2.0/permissions/genie/{id}/permissionLevels``) are CAN_READ, CAN_RUN, CAN_EDIT, CAN_MANAGE.
READER_LEVEL = "CAN_RUN"
#: Direct levels an apply never takes away: whoever edits or manages the space keeps doing so.
_KEPT_LEVELS = frozenset({"CAN_EDIT", "CAN_MANAGE"})
_LEVEL_RANK = {"CAN_READ": 1, "CAN_RUN": 2, "CAN_EDIT": 3, "CAN_MANAGE": 4}
_PAGE_SIZE = 100
#: The use cases a Genie space is: answering questions about the data, by analysing it. One of
#: them allowed is enough; either one denied refuses the space.
_GENIE_USE_CASES = ("qa", "analysis")
#: What reads a table: ``SELECT``, or ``ALL_PRIVILEGES`` (a contract's ``admin`` or ``manage``),
#: as ``databricks_enforce`` reads access.
_READ_PRIVILEGES = frozenset({"SELECT", "ALL_PRIVILEGES"})
_NOT_SENSITIVE = frozenset({"", "none", "public", "internal"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
#: What the service appends to a title another space in the folder already has (it keeps titles
#: unique there: ``Site 360 2026-11-12 20:35:27``), and a PATCH to a taken title is 409
#: ALREADY_EXISTS (both measured 2026-11-12).
_TITLE_SUFFIX = re.compile(r" \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
#: A new space shows in the list some seconds after it is made (9 s measured, 2026-11-12), so
#: an apply waits for it, at most this long: one run straight after would make a second space.
LIST_WAIT_SECONDS = 60.0
_POLL_SECONDS = 2.0

Say = Callable[[str], None]
ClientFactory = Callable[[], Any]


class GenieError(UnsupportedBindingError):
    """The Genie space could not be put in step with the contract (``databricks-genie-failed``)."""

    #: The engine's event for it, from whichever hook it is raised in.
    event = "opentofu_output_port_failed"

    def __init__(self, message: str, remediation: Sequence[str] = ()):
        super().__init__("databricks-genie-failed", message, tuple(remediation))


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-genie-{kind}", message, tuple(remediation))


def marker(product_id: str) -> str:
    """The token a space's description carries to say which product it belongs to."""
    return f"[fluid-genie:{product_id}]"


@dataclass(frozen=True)
class Space:
    """What the contract asks the product's Genie space to be."""

    product_id: str
    title: str
    description: str
    warehouse_id: str
    #: The full names of its tables, sorted.
    tables: Tuple[str, ...]
    #: The principals given ``CAN_RUN``: whoever holds ``SELECT`` on one of its tables.
    readers: Tuple[str, ...]
    #: ``serialized_space``, without the metric views (those are read at apply time).
    serialized: Dict[str, Any]
    #: The workspace folder the space lives in (``parentPath``, as :func:`workspace_folder`
    #: writes it), or ``""``: the deploying identity's own folder, where the API puts a space
    #: when the create names none.
    parent_path: str = ""

    @property
    def marker(self) -> str:
        return marker(self.product_id)

    @property
    def folder(self) -> str:
        """Where the space lives, for a message."""
        return self.parent_path or "this identity's folder"


@dataclass
class Compiled:
    """The space a contract asks for (``None`` without the block), and every refusal."""

    space: Optional[Space] = None
    refusals: List[UnsupportedBindingError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: The contract's id: what a space it asked for earlier is found by, block or no block.
    product_id: str = ""


# ---------------------------------------------------------------------- compile (pure)


def requested(contract: Mapping[str, Any]) -> bool:
    """Whether the contract carries ``extensions.databricksGenie`` at all."""
    extensions = contract.get("extensions")
    return isinstance(extensions, Mapping) and EXTENSION_KEY in extensions


_WHERE = f"extensions.{EXTENSION_KEY}"


def compile_space(
    contract: Mapping[str, Any],
    tables: Sequence[Any],
    grants: Iterable[Tuple[str, str, str, Sequence[str]]],
    env: Mapping[str, str],
) -> Compiled:
    """The space for ``contract``, from the Databricks tables and grants the emitter resolved.

    ``tables`` are the emitter's resolved exposes (``full_name``, ``name``, ``columns``,
    ``exposure``, ``where``); ``grants`` its ``(securable type, full name, principal,
    privileges)``. Credential-free and offline: ``fluid validate`` runs it.
    """
    out = Compiled(product_id=_text(contract.get("id")))
    if not requested(contract):
        return out
    title = _block_title(contract, out)
    parent_path = _block_parent_path(contract, out)
    out.refusals += _table_refusals(tables)
    if out.refusals:
        return out
    ordered = sorted(tables, key=lambda t: t.full_name)
    serialized = _serialized_space(contract, out.product_id, ordered, out.warnings)
    too_long = next((text for text in _strings_in(serialized) if len(text) > MAX_STRING), None)
    if too_long is not None:
        out.refusals.append(
            _refusal(
                "limit",
                f"{_WHERE}: the space would hold a text of {len(too_long)} characters, and Genie "
                f"holds at most {MAX_STRING:,} in one: {too_long[:80]!r}...",
                "Shorten that text in the contract.",
            )
        )
        return out
    out.space = Space(
        product_id=out.product_id,
        title=title,
        description=_description(contract, out.product_id),
        warehouse_id=str(env.get(WAREHOUSE_ENV) or "").strip(),
        tables=tuple(t.full_name for t in ordered),
        readers=_readers(tables, grants),
        serialized=serialized,
        parent_path=parent_path,
    )
    return out


def _block_title(contract: Mapping[str, Any], out: Compiled) -> str:
    """The space's title from the block (or the product's name); what is wrong with the block
    goes into ``out.refusals``."""
    block = contract["extensions"][EXTENSION_KEY]
    if block is None:  # ``databricksGenie:`` with nothing under it
        block = {}
    if not isinstance(block, Mapping):
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} is {block!r}, not a mapping: write {{}} for a space with the "
                "product's name, or {title: ...}.",
                f"Write {_WHERE}: {{}} to create the space, or remove {_WHERE} for none.",
            )
        )
        return ""
    unknown = sorted(str(key) for key in block if key not in _BLOCK_KEYS)
    if unknown:
        near = sorted({m for k in unknown for m in get_close_matches(k, _BLOCK_KEYS, n=1)})
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} has {', '.join(repr(k) for k in unknown)}, which it does not read "
                f"(it reads: {', '.join(sorted(_BLOCK_KEYS))})"
                + (f"; did you mean {', '.join(repr(m) for m in near)}?" if near else "."),
                f"Remove {', '.join(unknown)} from {_WHERE}.",
            )
        )
    if not out.product_id:
        out.refusals.append(
            _refusal(
                "invalid",
                f"{_WHERE} needs the contract's id: the space's description carries it, which is "
                "how the next apply finds the space again.",
                "Give the contract an id.",
            )
        )
    title = block.get("title", contract.get("name") or out.product_id)
    if isinstance(title, str) and title.strip():
        return title.strip()
    out.refusals.append(
        _refusal(
            "invalid",
            f"{_WHERE}.title is {title!r}, and the space needs a title.",
            f"Set {_WHERE}.title to a name, or leave it out to use the product's name.",
        )
    )
    return ""


def workspace_folder(path: str) -> str:
    """``path`` as the Genie API reads a space's ``parent_path`` back: without the
    ``/Workspace`` prefix a create accepts and without a trailing ``/`` (both measured,
    2026-11-12). terraform-provider-databricks compares a dashboard's ``parent_path`` the same
    way (``common.WorkspacePathPrefixDiffSuppress``); here the prefix counts only as a whole
    segment. Workspace paths are case-sensitive (``/shared/x`` is not ``/Shared/x``: 404,
    measured), so nothing is folded, and nothing is trimmed: a folder the API names ``"/x "``
    is not ``/x``. (The contract's own value is trimmed first, by :func:`_block_parent_path`.)"""
    path = str(path or "")
    if path == "/Workspace" or path.startswith("/Workspace/"):
        path = path[len("/Workspace") :]
    return path.rstrip("/")


def _block_parent_path(contract: Mapping[str, Any], out: Compiled) -> str:
    """The folder ``parentPath`` names, as :func:`workspace_folder` writes it, or ``""`` without
    one; what is wrong with it goes into ``out.refusals``."""
    block = contract["extensions"][EXTENSION_KEY]
    if not isinstance(block, Mapping) or "parentPath" not in block:
        return ""
    raw = block["parentPath"]
    where = f"{_WHERE}.parentPath"
    folder = workspace_folder(raw.strip()) if isinstance(raw, str) else ""
    if not isinstance(raw, str) or not raw.strip():
        problem = "not a folder path"
    elif any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        problem = "a path with a control character in it"
    elif not raw.strip().startswith("/"):
        problem = "not absolute, and Genie takes a folder from the workspace root"
    elif not folder:
        problem = "the workspace root, and the space goes in a folder"
    elif any(part in ("", ".", "..") for part in folder.split("/")[1:]):
        problem = "a path with an empty, '.' or '..' part"
    else:
        problem = ""
    if problem:
        out.refusals.append(
            _refusal(
                "invalid",
                f"{where} is {raw!r}: {problem}.",
                f"Set {where} to a workspace folder such as /genie/<domain>, or leave it out for "
                "the deploying identity's own folder.",
            )
        )
        return ""
    if folder == SHARED_FOLDER or folder.startswith(SHARED_FOLDER + "/"):
        out.refusals.append(
            _refusal(
                "folder",
                f"{where} is {raw!r}, in {SHARED_FOLDER}, where every workspace user holds "
                f"CAN_MANAGE (the {ALL_USERS} group, granted on {SHARED_FOLDER} itself and "
                "inherited by every folder in it). Each of them could then edit the Genie space's "
                "instructions, read every reader's conversations, and make a space there that the "
                "apply takes for the product's.",
                f"Use a folder outside {SHARED_FOLDER} that a workspace admin creates (/genie/<domain> "
                "at the workspace root, say, which only admins can write) and gives CAN_MANAGE only "
                "to the identities that deploy the product.",
            )
        )
        return ""
    return folder


def _table_refusals(tables: Sequence[Any]) -> List[UnsupportedBindingError]:
    """A space needs one to :data:`MAX_TABLES` tables, each one an ``agentPolicy`` allows."""
    if not tables:
        return [
            _refusal(
                "no-table",
                f"{_WHERE} asks for a Genie space, and the contract has no Databricks table "
                "expose for it to hold.",
                "Bind an expose to databricks (binding.platform: databricks), or remove "
                f"{_WHERE}.",
            )
        ]
    out: List[UnsupportedBindingError] = []
    if len(tables) > MAX_TABLES:
        out.append(
            _refusal(
                "limit",
                f"{_WHERE}: a Genie space holds at most {MAX_TABLES} tables, and the contract "
                f"has {len(tables)} Databricks exposes.",
                "Split the product, or remove the block.",
            )
        )
    for table in tables:
        out += _policy_refusals(table)
    return out


def _readers(
    tables: Sequence[Any], grants: Iterable[Tuple[str, str, str, Sequence[str]]]
) -> Tuple[str, ...]:
    """Every principal that holds ``SELECT`` on one of the space's tables, sorted."""
    names = {t.full_name for t in tables}
    return tuple(
        sorted(
            {
                principal
                for kind, full_name, principal, privileges in grants
                if kind == "table" and full_name in names and _READ_PRIVILEGES & set(privileges)
            }
        )
    )


def _policy_refusals(table: Any) -> List[UnsupportedBindingError]:
    """What in the expose's ``agentPolicy`` a question-answering space would break."""
    policy = _agent_policy(table.exposure)
    if not policy:
        return []
    out: List[UnsupportedBindingError] = []
    where = f"{table.where} policy.agentPolicy"
    allowed = _string_list(policy.get("allowedUseCases"))
    denied = _string_list(policy.get("deniedUseCases"))
    if isinstance(policy.get("allowedModels"), list) and not policy["allowedModels"]:
        out.append(
            _refusal(
                "policy",
                f"{where}.allowedModels is [], which means no AI access, and a Genie space answers "
                "questions with a model.",
                f"Remove extensions.{EXTENSION_KEY}, or allow AI access in agentPolicy.",
            )
        )
    refused = [case for case in _GENIE_USE_CASES if case in denied]
    if refused or (allowed and not set(_GENIE_USE_CASES) & set(allowed)):
        out.append(
            _refusal(
                "policy",
                f"{where} "
                + (
                    f"denies {' and '.join(repr(c) for c in refused)}"
                    if refused
                    else "allows neither 'qa' nor 'analysis'"
                )
                + " (question answering over the data), which is what a Genie space is.",
                f"Remove extensions.{EXTENSION_KEY}, or allow 'qa' in agentPolicy.allowedUseCases "
                "(and take 'qa' and 'analysis' out of deniedUseCases).",
            )
        )
    return out


#: The plan op of a Genie space: its words name no kind the engines' shadow comparison counts
#: (``iac/shadow.py`` matches table, view, schema, ...), since no module holds the space.
PLAN_OP = "databricks.genie_space.converge"


def space_plan_action(space: Space) -> Dict[str, Any]:
    """The space as a plan action: ``fluid plan`` lists it (``plan.py``, through the plugin's
    ``planned_output_ports``) and ``fluid diff`` counts it among the resources the contract asks
    for (``databricks.py::planned_objects``).

    No OpenTofu resource: ``fluid apply`` makes it through the Genie API after ``tofu apply``,
    so no module or state holds it, and ``fluid diff``'s verdict does not read it (the live
    check reads the tables, the state check the module's resources). Identified as the space
    itself is, by the product's id, with no space id: none is known before an apply finds or
    makes it. ``op`` and ``action_type`` both, as every plan action carries them.
    """
    return {
        "action_id": f"genie_space_{space.product_id}",
        "op": PLAN_OP,
        "action_type": PLAN_OP,
        "provider": "databricks",
        "resource_type": "databricks_genie_space",
        "resource_id": space.product_id,
        "description": f"Genie space {space.title!r}{_in(space)}: fluid apply creates or "
        "updates it through the Genie API, after tofu apply",
        "title": space.title,
        "parent_path": space.parent_path or None,
        "tables": list(space.tables),
        "readers": list(space.readers),
    }


def module_notice(contract: Mapping[str, Any]) -> Optional[str]:
    """What ``fluid generate iac`` says about a contract that asks for a space: the module it
    writes holds none, and a plain ``tofu apply`` of it makes none."""
    if not requested(contract):
        return None
    return (
        f"{_WHERE} asks for a Genie space, which this module does not hold: fluid apply "
        "creates, updates or trashes it through the Genie API after tofu apply, so a plain "
        "tofu apply of this module makes no space."
    )


def _description(contract: Mapping[str, Any], product_id: str) -> str:
    about = _text(contract.get("description"))
    footer = (
        f"Managed by fluid apply from the data product contract {product_id}: changes made "
        f"here are replaced on the next apply. {marker(product_id)}"
    )
    return f"{about}\n\n{footer}" if about else footer


def _serialized_space(
    contract: Mapping[str, Any], product_id: str, tables: Sequence[Any], warnings: List[str]
) -> Dict[str, Any]:
    questions: Dict[str, str] = {}
    for table in tables:
        for question in _split(
            _labels(_semantics(table.exposure).get("labels")).get("ai-example-questions"), "|"
        ):
            questions.setdefault(question.casefold(), question)
    if not questions:
        warnings.append(
            f"extensions.{EXTENSION_KEY}: no expose has semantics.labels.ai-example-questions, "
            "so the Genie space shows no sample questions."
        )
    space: Dict[str, Any] = {"version": 2}
    if questions:
        space["config"] = {
            "sample_questions": _by_id(
                {"id": _id(product_id, "question", q), "question": [q]} for q in questions.values()
            )
        }
    space["data_sources"] = {"tables": [_table_source(t) for t in tables]}
    instructions: Dict[str, Any] = {
        "text_instructions": [
            {
                "id": _id(product_id, "instructions"),
                "content": [line + "\n" for line in _instruction_lines(contract, tables, warnings)],
            }
        ]
    }
    if len(tables) == 1:
        measures = _measure_snippets(product_id, tables[0])
        if measures:
            instructions["sql_snippets"] = {"measures": measures}
    space["instructions"] = instructions
    return space


def _table_source(table: Any) -> Dict[str, Any]:
    exposure = table.exposure
    source: Dict[str, Any] = {"identifier": table.full_name}
    about = _sentence(
        _text(exposure.get("description")) or _text(_semantics(exposure).get("description"))
    )
    if about:
        source["description"] = [about]
    configs = _column_configs(table)
    if configs:
        source["column_configs"] = configs
    return source


def _column_configs(table: Any) -> List[Dict[str, Any]]:
    """What the contract says of each column the table has, sorted by name (Genie's rule)."""
    by_name = {
        _text(column.get("name")).lower(): column
        for column in (table.exposure.get("contract") or {}).get("schema") or ()
        if isinstance(column, Mapping)
    }
    configs = []
    for emitted in table.columns:
        name = str(emitted.get("name") or "")
        column = by_name.get(name)
        if not name or column is None:
            continue
        config: Dict[str, Any] = {"column_name": name}
        about = _column_description(column)
        if about:
            config["description"] = [about]
        synonyms = _column_synonyms(column, name)
        if synonyms:
            config["synonyms"] = synonyms
        if len(config) > 1:
            configs.append(config)
    return sorted(configs, key=lambda c: c["column_name"])


def _column_description(column: Mapping[str, Any]) -> str:
    parts = [_sentence(_text(column.get("description")))]
    definition = _text(column.get("businessDefinition"))
    if definition:
        parts.append(_sentence(f"Business definition: {definition}"))
    parts += _rule_parts(column.get("validationRules"))
    sensitivity = _text(column.get("sensitivity"))
    if sensitivity.lower() in _NOT_SENSITIVE:
        example = _text(_labels(column.get("labels")).get("example"))
        if example:
            parts.append(f"Example: {example}.")
    else:
        parts.append(f"Sensitivity: {sensitivity}.")
    return " ".join(part for part in parts if part)


def _rule_parts(rules: Any) -> List[str]:
    """The values an ``enum`` rule allows and the bounds of a ``range`` one, as sentences."""
    parts = []
    for rule in rules or ():
        if not isinstance(rule, Mapping):
            continue
        kind = _text(rule.get("type")).lower()
        constraint = rule.get("constraint")
        values = _split(constraint, "|", ",") if kind == "enum" else []
        if values:
            parts.append(f"Allowed values: {', '.join(values)}.")
        elif kind == "range" and _text(constraint):
            parts.append(f"Range: {_text(constraint)}.")
    return parts


def _column_synonyms(column: Mapping[str, Any], name: str) -> List[str]:
    candidates = [_text(column.get("businessName"))]
    candidates += _split(_labels(column.get("labels")).get("synonyms"), ";", ",", "|")
    seen = {_folded(name)}
    out: List[str] = []
    for candidate in candidates:
        if candidate and _folded(candidate) not in seen:
            seen.add(_folded(candidate))
            out.append(candidate)
    return out


def _instruction_lines(
    contract: Mapping[str, Any], tables: Sequence[Any], warnings: List[str]
) -> List[str]:
    lines: List[str] = []
    for table in tables:
        section = _table_section(contract, table, warnings)
        if section and len(tables) > 1:
            title = _text(table.exposure.get("title")) or _text(table.exposure.get("exposeId"))
            lines.append(f"About {table.full_name}" + (f" ({title}):" if title else ":"))
            lines += [f"  {line}" for line in section]
        else:
            lines += section
    lines += _lineage_lines(contract)
    lines.append(
        "How access works: Genie runs each query as the person asking, under that person's own "
        "Unity Catalog grants, so column masks and row filters apply. This space grants no "
        "access to data: a table the person cannot SELECT returns no rows."
    )
    return lines


def _table_section(contract: Mapping[str, Any], table: Any, warnings: List[str]) -> List[str]:
    """What the space is told about one table: its semantics, metrics and ``agentPolicy``."""
    exposure = table.exposure
    semantics = _semantics(exposure)
    labels = _labels(semantics.get("labels"))
    section: List[str] = []
    if labels.get("ai-instructions"):
        section.append(_text(labels["ai-instructions"]))
    synonyms = _split(labels.get("ai-synonyms"), ";", "|")
    if synonyms:
        section.append(f"Also called: {'; '.join(synonyms)}.")
    grain = (
        labels.get("grain")
        or _labels(exposure.get("labels")).get("grain")
        or _labels(contract.get("labels")).get("grain")
    )
    if grain:
        section.append(_sentence(f"Grain: {_text(grain)}"))
    keys = [
        _text(entity.get("name"))
        for entity in semantics.get("entities") or ()
        if isinstance(entity, Mapping) and _text(entity.get("type")).lower() == "primary"
    ]
    if any(keys):
        section.append(f"Primary key: {', '.join(k for k in keys if k)}.")
    time = _text(semantics.get("defaultAggTimeDimension"))
    if time:
        section.append(f"Default time dimension: {time}.")
    return (
        section
        + _snapshot_lines(semantics)
        + _metric_lines(table, semantics, warnings)
        + _policy_lines(exposure)
    )


def _snapshot_lines(semantics: Mapping[str, Any]) -> List[str]:
    """The non-additive rule of the measures (``nonAdditiveDimension``), once per dimension."""
    rules: Dict[Tuple[str, str], List[str]] = {}
    for measure in semantics.get("measures") or ():
        if not isinstance(measure, Mapping):
            continue
        rule = measure.get("nonAdditiveDimension")
        if not isinstance(rule, Mapping) or not _text(rule.get("name")):
            continue
        choice = _text(rule.get("windowChoice")).lower() or "max"
        rules.setdefault((_text(rule["name"]), choice), []).append(_text(measure.get("name")))
    out = []
    for (dimension, choice), measures in sorted(rules.items()):
        which = "earliest" if choice == "min" else "latest"
        out.append(
            f"{', '.join(m for m in measures if m)} hold a state as of {dimension}: aggregate "
            f"them at the {which} {dimension} ({choice}) unless the question asks about history."
        )
    return out


def _exposed_metrics(semantics: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """``metrics[]``, then each measure with ``createMetric: true`` (a simple metric of itself,
    as MetricFlow reads it)."""
    metrics: List[Mapping[str, Any]] = []
    named: set = set()
    for metric in semantics.get("metrics") or ():
        name = _text(metric.get("name")) if isinstance(metric, Mapping) else ""
        if name and name not in named:  # the first of two with one name: ids are per name
            named.add(name)
            metrics.append(metric)
    for measure in semantics.get("measures") or ():
        if (
            isinstance(measure, Mapping)
            and measure.get("createMetric") is True
            and _text(measure.get("name")) not in named
        ):
            metrics.append(
                {
                    "name": measure.get("name"),
                    "description": measure.get("description"),
                    "type": "simple",
                    "measure": measure.get("name"),
                }
            )
    return metrics


def _metric_lines(table: Any, semantics: Mapping[str, Any], warnings: List[str]) -> List[str]:
    out = []
    for metric in _exposed_metrics(semantics):
        name = _text(metric.get("name"))
        if not name:
            continue
        line = f"- {name}"
        about = _text(metric.get("description"))
        if about:
            line += f": {_sentence(about)}"
        sql, problem = metric_sql(metric, semantics)
        if sql:
            line += f" SQL: {sql}"
        elif problem:
            warnings.append(f"{table.where} metric {name!r}: {problem}")
        out.append(line)
    return ["Metrics, as the product defines them:"] + out if out else []


def _policy_lines(exposure: Mapping[str, Any]) -> List[str]:
    policy = _agent_policy(exposure)
    out = []
    allowed = _string_list(policy.get("allowedUseCases"))
    denied = _string_list(policy.get("deniedUseCases"))
    if allowed:
        out.append(f"Allowed uses of this data: {', '.join(allowed)}.")
    if denied:
        out.append(
            f"Not allowed: {', '.join(denied)}. Decline a request for a use that is not allowed."
        )
    purpose = _text(policy.get("purposeLimitation"))
    if purpose:
        out.append(_sentence(f"Purpose limitation: {purpose}"))
    if policy.get("canStore") is False:
        out.append("AI systems may not cache or store this data (agentPolicy canStore: false).")
    if policy.get("canReason") is False:
        out.append(
            "Answer by direct retrieval, without multi-step reasoning over this data "
            "(agentPolicy canReason: false)."
        )
    classification = _text((exposure.get("policy") or {}).get("classification"))
    if classification:
        out.append(f"Classification: {classification}.")
    return out


def _lineage_lines(contract: Mapping[str, Any]) -> List[str]:
    out = []
    for ref in contract.get("consumes") or ():
        if not isinstance(ref, Mapping):
            continue
        product = _text(ref.get("productId") or ref.get("contractId"))
        if not product:
            continue
        expose = _text(ref.get("exposeId"))
        line = f"- {product}" + (f" ({expose})" if expose else "")
        purpose = _text(ref.get("purpose"))
        out.append(line + (f": {_sentence(purpose)}" if purpose else ""))
    if not out:
        return []
    return ["Built from these data products, where a value's detail is checked or disputed:"] + out


# ---------------------------------------------------------------------- metric SQL

#: The aggregates of ``semantics.measures[].agg``, rendered exactly as the governed MCP query path
#: renders them (``output_ports/mcp/query_compiler.py::_render_measure_expression``; a test pins the
#: two together). Not imported: that package loads the whole MCP output port.
_AGGREGATES = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "count_distinct": "COUNT",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "percentile": "PERCENTILE_CONT",
}
DEFAULT_PERCENTILE = 0.5


def aggregate_sql(name: str, measure: Mapping[str, Any]) -> str:
    """``AGG(expr)`` for one measure; raises ``ValueError`` for one it cannot render safely."""
    agg = measure.get("agg")
    if not isinstance(agg, str) or agg not in _AGGREGATES:
        raise ValueError(f"its measure {name!r} has the aggregate {agg!r}")
    expr = measure.get("expr") or name
    if not isinstance(expr, str):
        raise ValueError(f"its measure {name!r} has a non-string expr")
    expr = validate_sql_expression_allowlist(expr)
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    if agg == "percentile":
        params = measure.get("aggParams") or {}
        raw = params.get("percentile", DEFAULT_PERCENTILE) if isinstance(params, Mapping) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 1:
            raise ValueError(f"its measure {name!r} has aggParams.percentile {raw!r}")
        fn = "PERCENTILE_DISC" if params.get("useDiscretePercentile") else "PERCENTILE_CONT"
        return f"{fn}({float(raw):g}) WITHIN GROUP (ORDER BY {expr})"
    return f"{_AGGREGATES[agg]}({expr})"


def metric_sql(
    metric: Mapping[str, Any], semantics: Mapping[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """``(SQL, None)`` for a simple metric over a measure, its filter as ``FILTER (WHERE ...)``;
    ``(None, why)`` for one that is not rendered (``None`` for a derived or ratio metric, which
    the text describes in words)."""
    if (_text(metric.get("type")) or "simple") != "simple":
        return None, None
    measure_name = _text(metric.get("measure"))
    measure = next(
        (
            m
            for m in semantics.get("measures") or ()
            if isinstance(m, Mapping) and _text(m.get("name")) == measure_name
        ),
        None,
    )
    if measure is None:
        return None, f"its measure {measure_name!r} is not in semantics.measures"
    try:
        sql = aggregate_sql(measure_name, measure)
        condition = metric.get("filter")
        if condition is not None:
            condition = validate_sql_expression_allowlist(condition)
            if not _balanced(condition):
                raise ValueError("its filter has unbalanced parentheses")
            sql += f" FILTER (WHERE {condition})"
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("its ") else "its SQL is not a plain expression"
        return None, f"{reason}, so the Genie space gets its description without SQL."
    return sql, None


def _measure_snippets(product_id: str, table: Any) -> List[Dict[str, Any]]:
    semantics = _semantics(table.exposure)
    snippets = []
    for metric in _exposed_metrics(semantics):
        name = _text(metric.get("name"))
        sql, _problem = metric_sql(metric, semantics)
        if not sql or not name:
            continue
        try:
            alias = validate_ident(name)
        except ValueError:
            continue
        snippets.append(
            {
                "id": _id(product_id, "measure", table.full_name, alias),
                "alias": alias,
                "display_name": alias.replace("_", " "),
                "sql": [sql],
            }
        )
    return _by_id(snippets)


# ---------------------------------------------------------------------- converge (live)


def converge(
    compiled: Compiled,
    say: Say,
    *,
    client_factory: Optional[ClientFactory] = None,
    dry_run: bool = False,
) -> None:
    """Create, update or trash the product's space so it is what the contract asks.

    Runs after ``tofu apply`` and the masks and row filters, so the tables exist and are
    protected first. With ``dry_run`` it reads and prints, and writes nothing. Raises
    :class:`GenieError` (or a refusal) for what it cannot do.
    """
    if compiled.refusals:
        raise compiled.refusals[0]
    space = compiled.space
    if space is None:
        _retire(compiled.product_id, say, client_factory, dry_run)
        return
    if not space.warehouse_id:
        raise GenieError(
            f"{WAREHOUSE_ENV} is not set, and a Genie space runs its queries on that warehouse.",
            (f"Set {WAREHOUSE_ENV} to the id of a SQL warehouse in the workspace.",),
        )
    client = _client(client_factory)
    say("\n  Databricks Genie space (Genie API, not OpenTofu):")
    if space.parent_path:
        _check_folder(client, space.parent_path, say)
    found = _call(client, "list the Genie spaces", lambda: _find(client, space.marker))
    found, own = _call(
        client,
        "check whose Genie spaces those are",
        lambda: _placed(client, found, say, space.parent_path),
    )
    # One space per product. A copy in this identity's own folder is moved into parentPath
    # when the folder has none yet (one copy only: which one would be a guess), and trashed
    # once the product's space is there.
    _at_most_one(found or own, space)
    serialized = _with_metric_views(client, space)
    space_id = str(found[0]["space_id"]) if found else ""
    if not found and own:
        moving = own.pop()
        space_id = str(moving["space_id"])
        _move(client, moving, space, say, dry_run)
    if space_id:
        _update_found(client, space_id, space, serialized, say, dry_run)
    elif dry_run:
        say(f"    would create {space.title!r}{_in(space)}: {_summary(serialized, space.tables)}")
        if space.readers:
            say(f"    would share it ({READER_LEVEL}) with {', '.join(space.readers)}")
        say(_DRY_RUN)
        return
    else:
        space_id = _create_new(client, space, serialized, say)
    _share(client, space_id, space.readers, say, dry_run)
    for copy in own:
        _trash_copy(client, copy, space, say, dry_run)
    if dry_run:
        say(_DRY_RUN)


_DRY_RUN = "    dry-run: no Genie space was created, changed, moved, shared or trashed."


def _in(space: Space) -> str:
    """`` in <folder>`` for a space ``parentPath`` places, else nothing (its own folder)."""
    return f" in {space.parent_path}" if space.parent_path else ""


def _at_most_one(spaces: Sequence[Mapping[str, Any]], space: Space) -> None:
    if len(spaces) > 1:
        raise GenieError(
            f"{len(spaces)} Genie spaces carry {space.marker} in their description: "
            + ", ".join(str(s.get("space_id")) for s in spaces)
            + ". fluid apply keeps one space per product and cannot tell which is the product's.",
            ("Trash the extra spaces (or remove the marker from their description), then re-run.",),
        )


def _check_folder(client: Any, folder: str, say: Say) -> None:
    """Stop unless ``folder`` is a workspace folder on which no group of every user
    (:data:`ALL_USERS_GROUPS`) holds ``CAN_EDIT`` or ``CAN_MANAGE``, directly or inherited.

    A space inherits every permission on its folder (measured, 2026-11-12): whoever can edit
    the folder can edit the space's instructions, and whoever manages it can also read every
    reader's conversations and make a space there carrying the product's marker, which the
    apply then adopts. The folder's permissions are the trust boundary, and one every user
    holds is none. Permissions this identity cannot read stop the apply too: they could be
    those.
    """
    object_id = _folder_object_id(client, folder)
    acl = _call(
        client,
        f"read the permissions on the folder {folder}",
        lambda: client.api_client.do("GET", FOLDER_PERMISSIONS_PATH.format(object_id)),
    )
    group, level = _broadest(acl, folder)
    if level in _KEPT_LEVELS:
        harm = "edit the space's instructions"
        if level == "CAN_MANAGE":
            harm = (
                "edit the space's instructions, read every reader's conversations and make a "
                "space there that the apply would take for the product's"
            )
        raise GenieError(
            f"every workspace user holds {level} on the folder {folder} (the {group} group), "
            f"and a Genie space inherits its folder's permissions: each of them could {harm}.",
            (
                f"Take CAN_EDIT and CAN_MANAGE on {folder} away from {group}, or set "
                f"{_WHERE}.parentPath to a folder that only the identities deploying the product "
                f"can edit or manage (not under {SHARED_FOLDER}, which grants {ALL_USERS} "
                "CAN_MANAGE).",
            ),
        )
    if level:
        say(
            f"    note: every workspace user holds {level} on {folder} (the {group} group), "
            "which the space inherits: they can open it, and each answer still follows the "
            "asker's own Unity Catalog grants."
        )


def _broadest(acl: Any, folder: str) -> Tuple[str, str]:
    """The group of every user holding the highest level on ``folder``, and that level (``""``
    for none of them). Any other answer than the measured shape stops the apply: every folder
    lists at least ``admins`` (measured), so an empty list, an entry without its levels or a
    level this module does not know could hide a grant to every user."""
    entries = acl.get("access_control_list") if isinstance(acl, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise GenieError(
            f"the permissions on the folder {folder} came back without an access control list, "
            "so whether every workspace user can edit or manage it is unknown.",
            (_remediation(None, f"read the permissions on the folder {folder}"),),
        )
    best = (ALL_USERS, "")
    for entry in entries:
        group = entry.get("group_name") if isinstance(entry, Mapping) else None
        if group not in ALL_USERS_GROUPS:
            continue
        held = entry.get("all_permissions")
        levels = [
            str(item.get("permission_level") or "").upper()
            for item in (held if isinstance(held, list) else ())
            if isinstance(item, Mapping)
        ]
        unknown = [level for level in levels if level not in _LEVEL_RANK]
        if not levels or unknown:
            raise GenieError(
                f"the permissions on the folder {folder} do not say plainly what the {group} "
                f"group holds ({unknown or held!r}), so whether every workspace user can edit "
                "or manage it is unknown.",
                (f"Check the permissions on {folder}, then re-run fluid apply.",),
            )
        for level in levels:
            if _LEVEL_RANK[level] > _LEVEL_RANK.get(best[1], 0):
                best = (str(group), level)
    return best


def _folder_object_id(client: Any, folder: str) -> str:
    """The object id of the folder ``parentPath`` names, for the Permissions API path; a folder
    that does not exist, or a path that is no folder, stops the apply with what to do."""
    where = f"{_WHERE}.parentPath"
    try:
        status = client.api_client.do("GET", WORKSPACE_STATUS_PATH, query={"path": folder}) or {}
    except Exception as exc:  # noqa: BLE001 — a missing folder is said as such
        if _status(exc) != 404:
            raise GenieError(
                f"could not read the folder {folder}: {type(exc).__name__}: {_scrub(exc)}",
                (_remediation(_status(exc), f"read the folder {folder}"),),
            ) from exc
        raise GenieError(
            f"{where} names {folder}, and the workspace has no such folder (Genie makes a space "
            "only in a folder that exists).",
            (
                f"A workspace admin creates {folder} (outside {SHARED_FOLDER}) and gives CAN_MANAGE "
                "on it only to the identities that deploy the product; then re-run fluid apply.",
            ),
        ) from exc
    kind = str(status.get("object_type") or "") if isinstance(status, Mapping) else ""
    if kind.upper() != "DIRECTORY":
        raise GenieError(
            f"{where} names {folder}, which is "
            + (f"a {kind.lower()}" if kind else "not a folder")
            + ", and a Genie space goes in a folder.",
            (f"Set {where} to a workspace folder.",),
        )
    object_id = status.get("object_id")
    if object_id is None or str(object_id) == "":
        raise GenieError(
            f"the workspace answered for the folder {folder} without its object id, so its "
            "permissions cannot be read.",
            (f"Check that {folder} is a workspace folder, then re-run fluid apply.",),
        )
    return quote(str(object_id))


def _move(client: Any, moving: Mapping[str, Any], space: Space, say: Say, dry_run: bool) -> None:
    """Move a space from this identity's own folder into ``parentPath`` (``PATCH parent_path``,
    measured 2026-11-12), so its id, link and conversations stay."""
    space_id = str(moving.get("space_id"))
    what = (
        f"{moving.get('title')!r} ({space_id}) from this identity's own folder into "
        f"{space.parent_path}"
    )
    if dry_run:
        say(f"    would move {what}")
        return
    try:
        client.api_client.do(
            "PATCH", f"{SPACES_PATH}/{quote(space_id)}", body={"parent_path": space.parent_path}
        )
    except Exception as exc:  # noqa: BLE001 — a taken title is said as such
        if _status(exc) == 409:
            raise GenieError(
                f"could not move space {space_id} into {space.parent_path}: another space there "
                f"is already called {moving.get('title')!r}, and Genie keeps titles unique in a "
                "folder (measured for a create and a rename).",
                (
                    f"Give this space a title of its own with extensions.{EXTENSION_KEY}.title, "
                    "or rename the other space, then re-run fluid apply.",
                ),
            ) from exc
        raise GenieError(
            f"could not move the Genie space into {space.parent_path}: "
            f"{type(exc).__name__}: {_scrub(exc)}",
            (_remediation(_status(exc), f"move the Genie space into {space.parent_path}"),),
        ) from exc
    parent = _call(client, "read the moved Genie space", lambda: _parent(client, space_id))
    if workspace_folder(parent) != space.parent_path:
        raise GenieError(
            f"the Genie API accepted the move of space {space_id} into {space.parent_path}, and "
            f"the space is in {parent or 'a folder this identity cannot read'}.",
            (f"Move it into {space.parent_path} by hand, or trash it, then re-run fluid apply.",),
        )
    say(f"    moved {what}")


def _trash_copy(
    client: Any, copy: Mapping[str, Any], space: Space, say: Say, dry_run: bool
) -> None:
    """Trash a space carrying the marker in this identity's own folder, now that the product's
    space is the one in ``parentPath``."""
    space_id = str(copy.get("space_id"))
    what = (
        f"{copy.get('title')!r} ({space_id}), a copy in this identity's own folder: the product's "
        f"space is the one{_in(space)}"
    )
    if dry_run:
        say(f"    would trash {what}")
        return
    _call(
        client,
        "trash the Genie space",
        lambda: client.api_client.do("DELETE", f"{SPACES_PATH}/{quote(space_id)}"),
    )
    say(f"    trashed {what}")


def _with_metric_views(client: Any, space: Space) -> Dict[str, Any]:
    """``serialized_space`` with every metric view over the space's tables among the tables:
    the service keeps one there, and moves one sent under ``data_sources.metric_views`` there
    too (measured on a live workspace, 2026-11-12), so sent anywhere else it would never
    compare equal and every apply would update."""
    serialized = dict(space.serialized)
    views = _call(
        client, "read the metric views in Unity Catalog", lambda: metric_views(client, space.tables)
    )
    if views:
        tables = sorted(
            [*serialized["data_sources"]["tables"], *views], key=lambda t: t["identifier"]
        )
        if len(tables) > MAX_TABLES:
            raise GenieError(
                f"the space would hold {len(space.tables)} tables and {len(views)} metric views "
                f"over them, and Genie holds at most {MAX_TABLES} tables, views or metric views.",
                ("Drop the metric views the space does not need, or split the product.",),
            )
        serialized["data_sources"] = {**serialized["data_sources"], "tables": tables}
    return serialized


def _update_found(
    client: Any,
    space_id: str,
    space: Space,
    serialized: Mapping[str, Any],
    say: Say,
    dry_run: bool,
) -> None:
    live = _call(client, "read the Genie space", lambda: _get(client, space_id))
    title = _title_kept(live.get("title"), space, say)
    changes = differences(live, space, serialized)
    if not changes:
        say(f"    {space.title!r}: no change ({_url(space_id)})")
    elif dry_run:
        say(f"    would update {space.title!r}: {', '.join(changes)} ({_url(space_id)})")
    else:
        _call(
            client,
            "update the Genie space",
            lambda: _update(client, space_id, space, serialized, live.get("etag"), title),
        )
        say(f"    updated {space.title!r}: {', '.join(changes)} ({_url(space_id)})")


def _create_new(client: Any, space: Space, serialized: Mapping[str, Any], say: Say) -> str:
    created = _call(client, "create the Genie space", lambda: _create(client, space, serialized))
    space_id = str(created.get("space_id") or "")
    if not space_id:
        raise GenieError("the Genie API created a space and returned no space_id.")
    say(
        f"    created {space.title!r}{_in(space)}: {_summary(serialized, space.tables)} "
        f"({_url(space_id)})"
    )
    _title_kept(created.get("title"), space, say)
    _wait_listed(client, space, space_id, say)
    _found_again(client, space, space_id)
    return space_id


def _found_again(client: Any, space: Space, space_id: str) -> None:
    """Stop, and trash the new space, when it is not where the next apply looks for it: a
    space every apply could not find would be made again on each one."""
    home = ""
    if not space.parent_path:
        home = _call(client, "read which identity this run is", lambda: _home(client))
    where = space.parent_path or f"this identity's own folder ({home})"
    parent = _call(client, "read the new Genie space", lambda: _parent(client, space_id))
    landed = (
        workspace_folder(parent) == space.parent_path
        if space.parent_path
        else _in_folder(parent, home)
    )
    if landed:
        return
    try:
        _call(
            client,
            "trash the new Genie space",
            lambda: client.api_client.do("DELETE", f"{SPACES_PATH}/{quote(space_id)}"),
        )
    except GenieError as exc:
        raise GenieError(
            f"the Genie API put the new space {space_id} in "
            f"{parent or 'a folder this identity cannot read'}, not in {where}, where the next "
            f"apply looks for it, and trashing it failed: {exc}",
            (f"Trash space {space_id} by hand before the next apply, which would make another.",),
        ) from exc
    raise GenieError(
        f"the Genie API put the new space in {parent or 'a folder this identity cannot read'}, "
        f"not in {where}, where the next apply looks for it, so it was trashed rather than made "
        "again on every apply.",
        (
            (
                (
                    f"Check that {space.parent_path} is a folder this identity can read, or report "
                    "this to forge-cli with the folder named above."
                )
                if space.parent_path
                else (
                    "Deploy with an identity whose home folder is /Users/<its userName>, or report "
                    "this to forge-cli with the folder named above."
                )
            ),
        ),
    )


def _wait_listed(client: Any, space: Space, space_id: str, say: Say) -> None:
    """Return once the list shows the new space (at most :data:`LIST_WAIT_SECONDS`), so the
    next apply finds it instead of making a second one."""
    deadline = time.monotonic() + LIST_WAIT_SECONDS
    while True:
        try:
            if any(str(s.get("space_id")) == space_id for s in _find(client, space.marker)):
                return
        except Exception:  # noqa: BLE001 — the space exists; only the wait is cut short
            pass
        if time.monotonic() >= deadline:
            say(
                "    note: the Genie list does not show the new space yet; an apply in the next "
                "minute could make a second one, which the apply after refuses until one is "
                "trashed."
            )
            return
        time.sleep(_POLL_SECONDS)


def _title_kept(live: Any, space: Space, say: Say) -> str:
    """The title to send: the contract's, or the live one when the service suffixed it because
    another space in the folder has the contract's (sending that again is 409), said as
    such."""
    live = str(live or "")
    wanted = space.title
    if live != wanted and _same_title(live, wanted):
        say(
            f"    note: the space is titled {live!r}: another space in {space.folder} is "
            f"already called {wanted!r} and Genie keeps titles unique there. Give this one a "
            f"title of its own with extensions.{EXTENSION_KEY}.title."
        )
        return live
    return wanted


def _same_title(live: Any, wanted: str) -> bool:
    live = str(live or "")
    return live == wanted or (
        live.startswith(wanted) and bool(_TITLE_SUFFIX.fullmatch(live[len(wanted) :]))
    )


def preflight(compiled: Compiled, client_factory: Optional[ClientFactory] = None) -> None:
    """Before ``tofu apply``: the client a space the contract asks for needs (the SDK extra and
    the credential), and the folder ``parentPath`` names (:func:`_check_folder`), so a missing
    one, or a folder every workspace user can edit, stops the run before anything is applied."""
    if compiled.space is not None:
        client = _client(client_factory)
        if compiled.space.parent_path:
            _check_folder(client, compiled.space.parent_path, lambda _line: None)


def _retire(product_id: str, say: Say, factory: Optional[ClientFactory], dry_run: bool) -> None:
    """Trash the space an earlier apply made, now the contract asks for none.

    Without a client, or where the workspace has no Genie API (403 or 404), there is nothing to
    say: no space can have been made there. Any other failure to look is a note, since the
    space stays as it was and grants nothing.
    """
    if not product_id:
        return
    try:
        client = (factory or _default_client)()
    except ImportError:
        return
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        say(f"  note: not checked for a Genie space an earlier apply made: {_scrub(exc)}")
        return
    try:
        found = _find(client, marker(product_id))
        if found:
            found = _placed(client, found, say)[0]
    except Exception as exc:  # noqa: BLE001
        if _status(exc) not in (403, 404):
            say(f"  note: not checked for a Genie space an earlier apply made: {_scrub(exc)}")
        return
    for space in found:
        space_id = str(space.get("space_id"))
        title = space.get("title")
        if dry_run:
            say(
                f"\n  Databricks Genie space: would trash {title!r} ({space_id}), which "
                f"extensions.{EXTENSION_KEY} no longer asks for."
            )
            continue
        _call(
            client,
            "trash the Genie space",
            lambda sid=space_id: client.api_client.do("DELETE", f"{SPACES_PATH}/{quote(sid)}"),
        )
        say(
            f"\n  Databricks Genie space: trashed {title!r} ({space_id}), which "
            f"extensions.{EXTENSION_KEY} no longer asks for."
        )


def _find(client: Any, token: str) -> List[Dict[str, Any]]:
    """Every space whose description ends with ``token``, across the list's pages (a
    description that only quotes another product's marker is not that product's)."""
    found: List[Dict[str, Any]] = []
    query: Dict[str, Any] = {"page_size": _PAGE_SIZE}
    seen: set = set()
    while True:
        page = client.api_client.do("GET", SPACES_PATH, query=query) or {}
        for space in page.get("spaces") or ():
            description = str(space.get("description") or "") if isinstance(space, Mapping) else ""
            if description.rstrip().endswith(token):
                found.append(dict(space))
        token_next = page.get("next_page_token")
        if not token_next or token_next in seen:
            return found
        seen.add(token_next)
        query = {"page_size": _PAGE_SIZE, "page_token": token_next}


def _placed(
    client: Any, found: List[Dict[str, Any]], say: Say, folder: str = ""
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``found`` by folder: ``(the product's spaces, this identity's own copies)``.

    The product's are the spaces in ``folder`` itself (``parentPath``, compared as
    :func:`workspace_folder` writes it, case and all) or, without one, in this identity's own
    folder, ``/Users/<userName>``, where the Genie API puts a space it creates (measured
    2026-11-12; the list leaves ``parent_path`` out, a plain GET has it). A service
    principal's folder is ``/Users/<application id>``, its userName, the folder Databricks Asset
    Bundles' default ``root_path`` builds from ``workspace.current_user.userName``. With
    ``folder``, the spaces in this identity's own folder are its copies, from an apply before
    ``parentPath`` was set.

    A space anyone else made carrying the marker is left alone, with a note: adopting it would
    fill it with the product, share it to the readers and keep its maker's ``CAN_MANAGE``
    (inherited from their folder, which a share cannot remove), and that level can read the
    readers' conversations. Only this identity and an admin can make a space in its folder, and
    only whom ``folder``'s permissions let (:func:`_check_folder`) in that one. A failure to read
    a space's folder other than 403 or 404 is raised, not taken for "someone else's": that would
    make a second space for the product.
    """
    if not found:
        return [], []
    home = _home(client)
    product: List[Dict[str, Any]] = []
    own: List[Dict[str, Any]] = []
    for space in found:
        space_id = str(space.get("space_id"))
        parent = _parent(client, space_id)
        if folder and workspace_folder(parent) == folder:
            product.append(space)
        elif _in_folder(parent, home):
            (own if folder else product).append(space)
        else:
            say(
                f"    note: left alone: space {space_id} ({space.get('title')!r}) carries "
                f"{_marker_of(space)} but is in {parent or 'a folder this identity cannot read'}, "
                "not in " + (f"{folder} or " if folder else "") + "this identity's own folder, "
                "so it is not taken for the product's. Trash it, or take the marker out of its "
                "description. If it is this product's own space, left in a folder the contract no "
                f"longer names as {_WHERE}.parentPath, trash it there by hand: the apply trusts no "
                "other folder."
            )
    return product, own


def _home(client: Any) -> str:
    """This identity's own folder, lower-cased: ``/users/<userName>``."""
    return f"/users/{_enforce.deploy_identity(client).lower()}"


def _parent(client: Any, space_id: str) -> str:
    """The folder a space is in, ``""`` when this identity cannot read it (403, 404)."""
    try:
        doc = client.api_client.do("GET", f"{SPACES_PATH}/{quote(space_id)}") or {}
    except Exception as exc:  # noqa: BLE001 — only "cannot read it" means "not its own"
        if _status(exc) in (403, 404):
            return ""
        raise
    return str(doc.get("parent_path") or "")


def _in_folder(parent: str, home: str) -> bool:
    folder = parent.rstrip("/").lower()
    return folder == home or folder.startswith(home + "/")


def _marker_of(space: Mapping[str, Any]) -> str:
    """The marker a listed space's description ends with (for a message)."""
    text = str(space.get("description") or "").rstrip()
    start = text.rfind("[fluid-genie:")
    return text[start:] if start >= 0 else "a product's marker"


def _get(client: Any, space_id: str) -> Dict[str, Any]:
    return client.api_client.do(
        "GET",
        f"{SPACES_PATH}/{quote(space_id)}",
        query={"include_serialized_space": "true"},
    )


def _body(space: Space, serialized: Mapping[str, Any], title: str = "") -> Dict[str, Any]:
    return {
        "title": title or space.title,
        "description": space.description,
        "warehouse_id": space.warehouse_id,
        "serialized_space": json.dumps(serialized, sort_keys=True),
    }


def _create(client: Any, space: Space, serialized: Mapping[str, Any]) -> Dict[str, Any]:
    body = _body(space, serialized)
    if space.parent_path:
        body["parent_path"] = space.parent_path
    return client.api_client.do("POST", SPACES_PATH, body=body) or {}


def _update(
    client: Any,
    space_id: str,
    space: Space,
    serialized: Mapping[str, Any],
    etag: Any,
    title: str = "",
) -> None:
    body = _body(space, serialized, title)
    if etag:
        body["etag"] = etag
    try:
        client.api_client.do("PATCH", f"{SPACES_PATH}/{quote(space_id)}", body=body)
    except Exception as exc:  # noqa: BLE001 — a 409 is a taken title or someone else's edit
        if _status(exc) == 409 and _taken_title(exc):
            raise GenieError(
                f"another Genie space in {space.folder} is already called "
                f"{body['title']!r}, and Genie keeps titles unique there.",
                (
                    f"Give this space a title of its own with extensions.{EXTENSION_KEY}.title, "
                    "or rename the other space.",
                ),
            ) from exc
        if _status(exc) == 409:
            raise GenieError(
                "the Genie space changed while this apply was updating it (its etag no longer "
                "matched), so nothing was overwritten.",
                ("Re-run fluid apply.",),
            ) from exc
        raise


def _taken_title(exc: BaseException) -> bool:
    """A 409 for a title another space holds (``ALREADY_EXISTS``), not for a stale etag."""
    code = str(getattr(exc, "error_code", "") or "")
    names = {cls.__name__ for cls in type(exc).__mro__}
    return code in ("ALREADY_EXISTS", "RESOURCE_ALREADY_EXISTS") or bool(
        names & {"AlreadyExists", "ResourceAlreadyExists"}
    )


def differences(live: Mapping[str, Any], space: Space, serialized: Mapping[str, Any]) -> List[str]:
    """What differs between the live space and the one the contract asks for, by name. A title
    the service suffixed because another space has it counts as the same title."""
    changes = []
    if not _same_title(live.get("title"), space.title):
        changes.append("title")
    if (live.get("description") or "") != space.description:
        changes.append("description")
    if live.get("warehouse_id") != space.warehouse_id:
        changes.append("warehouse")
    try:
        held = json.loads(live.get("serialized_space") or "{}")
    except (TypeError, ValueError):
        held = {}
    if held != serialized:
        named = []
        if _dig(held, ("config", "sample_questions")) != _dig(
            serialized, ("config", "sample_questions")
        ):
            named.append("sample questions")
        named += _source_changes(held, serialized, set(space.tables))
        if _dig(held, ("instructions", "text_instructions")) != _dig(
            serialized, ("instructions", "text_instructions")
        ):
            named.append("instructions")
        if _dig(held, ("instructions", "sql_snippets")) != _dig(
            serialized, ("instructions", "sql_snippets")
        ):
            named.append("SQL snippets")
        changes += named or ["other settings"]
    return changes


def _source_changes(held: Any, wanted: Any, own: set) -> List[str]:
    """``tables`` and/or ``metric views``: which of the space's data sources differ. Its own
    tables are the product's; any other entry in the list is a metric view over them."""
    path = ("data_sources", "tables")
    before = {str(t.get("identifier")): t for t in _dig(held, path) or () if isinstance(t, Mapping)}
    after = {
        str(t.get("identifier")): t for t in _dig(wanted, path) or () if isinstance(t, Mapping)
    }
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    out = []
    if any(name in own for name in changed) or (
        not changed and _dig(held, path) != _dig(wanted, path)
    ):
        out.append("tables")
    if any(name not in own for name in changed):
        out.append("metric views")
    return list(dict.fromkeys(out))


def metric_views(client: Any, tables: Sequence[str]) -> List[Dict[str, Any]]:
    """The metric views whose ``source`` is one of ``tables``, as space data sources, sorted.

    Read from each table's schema (``GET /api/2.1/unity-catalog/tables``, ``table_type
    METRIC_VIEW``); the definition is the metric view's YAML, whose ``source`` names the table.
    """
    wanted = {t.lower() for t in tables}
    out: Dict[str, Dict[str, Any]] = {}
    for catalog, schema in sorted({tuple(t.split(".")[:2]) for t in tables}):
        for info in _list_tables(client, catalog, schema):
            view = _metric_view_over(client, info, wanted)
            if view is not None:
                out[view["identifier"]] = view
    return [out[name] for name in sorted(out)]


def _metric_view_over(
    client: Any, info: Mapping[str, Any], wanted: Collection[str]
) -> Optional[Dict[str, Any]]:
    """``info`` as a space data source when it is a metric view over one of ``wanted``."""
    if str(info.get("table_type") or "").upper() != "METRIC_VIEW":
        return None
    full_name = str(info.get("full_name") or "").lower()
    if not full_name:
        return None
    definition = info.get("view_definition")
    if definition is None:  # a list that leaves the definition out: read the view itself
        definition = (_read_table(client, full_name) or {}).get("view_definition")
    if _metric_view_source(definition) not in wanted:
        return None
    view: Dict[str, Any] = {"identifier": full_name}
    comment = _sentence(_text(info.get("comment")))
    if comment:
        view["description"] = [comment]
    return view


def _list_tables(client: Any, catalog: str, schema: str) -> List[Mapping[str, Any]]:
    query: Dict[str, Any] = {
        "catalog_name": catalog,
        "schema_name": schema,
        "omit_columns": "true",
    }
    out: List[Mapping[str, Any]] = []
    seen: set = set()
    while True:
        try:
            page = client.api_client.do("GET", TABLES_PATH, query=query) or {}
        except Exception as exc:  # noqa: BLE001 — a schema not created yet holds no view
            if _status(exc) == 404:
                return out
            raise
        out += [t for t in page.get("tables") or () if isinstance(t, Mapping)]
        token = page.get("next_page_token")
        if not token or token in seen:
            return out
        seen.add(token)
        query = {**query, "page_token": token}


def _read_table(client: Any, full_name: str) -> Optional[Dict[str, Any]]:
    try:
        return client.api_client.do("GET", f"{TABLES_PATH}/{quote(full_name, safe='.')}")
    except Exception as exc:  # noqa: BLE001 — a view this identity cannot read is not used
        if _status(exc) in (403, 404):
            return None
        raise


def _metric_view_source(definition: Any) -> str:
    """The table a metric view's YAML names in ``source``, lower-cased; ``""`` for a query."""
    if not isinstance(definition, str) or not definition.strip():
        return ""
    import yaml

    try:
        doc = yaml.safe_load(definition)
    except Exception:  # noqa: BLE001 — YAMLError, or ValueError for an impossible date

        return ""
    source = doc.get("source") if isinstance(doc, Mapping) else None
    if not isinstance(source, str):
        return ""
    name = source.strip().replace("`", "").lower()
    return "" if any(ch.isspace() for ch in name) else name


def _share(client: Any, space_id: str, readers: Sequence[str], say: Say, dry_run: bool) -> None:
    """Give ``readers`` ``CAN_RUN`` on the space and take it from anyone else who holds it
    directly. Direct ``CAN_EDIT`` and ``CAN_MANAGE`` stay, and so does every inherited level
    (the creator's ``CAN_MANAGE`` comes from the folder the space is in)."""
    path = PERMISSIONS_PATH.format(quote(space_id))
    doc = _call(
        client, "read the Genie space's permissions", lambda: client.api_client.do("GET", path)
    )
    held = _direct_levels(doc)
    spelled = {_folded_key(key): key for key in held}  # the service's own spelling
    reader_keys = {_folded_key(acl_key(r)) for r in readers}
    wanted = {key: level for key, level in held.items() if level in _KEPT_LEVELS}
    kept = sorted(key[1] for key in wanted if _folded_key(key) not in reader_keys)
    if kept:
        say(
            f"    note: {', '.join(kept)} also hold CAN_EDIT or CAN_MANAGE on the space, set on it "
            "directly; fluid apply keeps them (CAN_MANAGE can read other people's conversations)."
        )
    for principal in readers:
        key = acl_key(principal)
        wanted.setdefault(spelled.get(_folded_key(key), key), READER_LEVEL)
    if wanted == held:
        return
    granted = sorted(key[1] for key, level in wanted.items() if held.get(key) != level)
    removed = sorted(key[1] for key in held if key not in wanted)
    what = []
    if granted:
        what.append(f"{READER_LEVEL} for {', '.join(granted)}")
    if removed:
        what.append(f"removed from {', '.join(removed)}")
    if dry_run:
        say(f"    would share: {'; '.join(what)}")
        return
    body = {
        "access_control_list": [
            {kind: name, "permission_level": level}
            for (kind, name), level in sorted(wanted.items())
        ]
    }
    _call(client, "share the Genie space", lambda: client.api_client.do("PUT", path, body=body))
    say(f"    shared: {'; '.join(what)}")


def acl_key(principal: str) -> Tuple[str, str]:
    """The Permissions API key for a Unity Catalog principal: a user's email, a service
    principal's application ID (a UUID) or a group's name, the three forms Unity Catalog
    grants to (``iac/principals.py::_databricks_identity``)."""
    if "@" in principal:
        return ("user_name", principal)
    if _UUID.fullmatch(principal):
        return ("service_principal_name", principal)
    return ("group_name", principal)


def _folded_key(key: Tuple[str, str]) -> Tuple[str, str]:
    """An ACL key compared as Databricks compares it: a user's email and a service principal's
    application ID without case, a group's name as written."""
    kind, name = key
    return key if kind == "group_name" else (kind, name.casefold())


def _direct_levels(doc: Any) -> Dict[Tuple[str, str], str]:
    """``{(key, principal): level}`` of the permissions set on the space itself."""
    out: Dict[Tuple[str, str], str] = {}
    for entry in (doc or {}).get("access_control_list") or ():
        key = _acl_entry_key(entry)
        level = _direct_level(entry) if key is not None else ""
        if level:
            out[key] = level
    return out


def _direct_level(entry: Mapping[str, Any]) -> str:
    """The highest Genie level set on the space itself in one ACL entry, ``""`` for none
    (an inherited level is the folder's, which a share never touches)."""
    levels = [
        str(permission.get("permission_level") or "")
        for permission in entry.get("all_permissions") or ()
        if isinstance(permission, Mapping) and not permission.get("inherited")
    ]
    known = [level for level in levels if level in _LEVEL_RANK]
    return max(known, key=_LEVEL_RANK.__getitem__, default="")


def _acl_entry_key(entry: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(entry, Mapping):
        return None
    for kind in ("user_name", "service_principal_name", "group_name"):
        if entry.get(kind):
            return (kind, str(entry[kind]))
    return None


# ---------------------------------------------------------------------- helpers


def _client(factory: Optional[ClientFactory]) -> Any:
    try:
        return (factory or _default_client)()
    except UnsupportedBindingError:
        raise
    except ImportError:
        raise GenieError(
            "databricks-sdk is not installed, and the Genie space is created through it.",
            (f"Install the extra: {DATABRICKS_EXTRA_INSTALL}",),
        ) from None
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        raise GenieError(
            f"could not create the Databricks client for the Genie space: "
            f"{type(exc).__name__}: {_scrub(exc)}"
        ) from exc


def _default_client() -> Any:
    # Through the module, so a test that patches ``databricks_enforce.default_client`` (the
    # in-process stand-in) reaches this client too.
    return _enforce.default_client()


def _call(client: Any, what: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except UnsupportedBindingError:
        raise
    except Exception as exc:  # noqa: BLE001 — every API failure is said, with what to do
        raise GenieError(
            f"could not {what}: {type(exc).__name__}: {_scrub(exc)}",
            (_remediation(_status(exc), what),),
        ) from exc


def _remediation(status: Optional[int], what: str) -> str:
    """What to do about a failed call, by its HTTP status."""
    if status in (403, 404):
        genie = "Genie (AI/BI) is enabled in the workspace and that " if "Genie" in what else ""
        return f"Check that {genie}this identity may {what}, then re-run fluid apply."
    if status == 400:
        return "Fix what the message names, then re-run fluid apply."
    if status == 429 or (status or 0) >= 500:
        return "The workspace is busy or unavailable: re-run fluid apply in a minute."
    return "Check DATABRICKS_HOST and the credential, then re-run fluid apply."


#: The HTTP status each databricks-sdk error class stands for (``databricks/sdk/errors/
#: platform.py`` ``STATUS_CODE_MAPPING``): its errors carry the status as their class, not as an
#: attribute, and a subclass (``Aborted``, ``ResourceDoesNotExist``) as its parent's.
_SDK_STATUS = {
    "BadRequest": 400,
    "Unauthenticated": 401,
    "PermissionDenied": 403,
    "NotFound": 404,
    "ResourceConflict": 409,
    "TooManyRequests": 429,
}


def _status(exc: BaseException) -> Optional[int]:
    """The HTTP status of a failed call: the attribute a plain HTTP error carries, else the
    databricks-sdk error class (matched by name, so the SDK is not imported here)."""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(value, int):
        return value
    for cls in type(exc).__mro__:
        if cls.__name__ in _SDK_STATUS:
            return _SDK_STATUS[cls.__name__]
    return None


def _scrub(text: Any) -> str:
    """One short line with every credential value in the environment removed."""
    return _enforce.scrub(text)


def _url(space_id: str) -> str:
    host = str(os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
    return f"{host}/genie/rooms/{space_id}" if host.startswith("https://") else space_id


def _summary(serialized: Mapping[str, Any], own: Sequence[str]) -> str:
    sources = [t.get("identifier") for t in _dig(serialized, ("data_sources", "tables")) or ()]
    tables = sum(1 for name in sources if name in own)
    views = len(sources) - tables
    questions = len(_dig(serialized, ("config", "sample_questions")) or ())
    measures = len(_dig(serialized, ("instructions", "sql_snippets", "measures")) or ())
    parts = [f"{tables} table(s)"]
    if views:
        parts.append(f"{views} metric view(s)")
    parts.append(f"{questions} sample question(s)")
    if measures:
        parts.append(f"{measures} measure snippet(s)")
    return ", ".join(parts)


def _id(*parts: str) -> str:
    """A stable id of 32 lowercase hex characters, which Genie requires of every item."""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def _by_id(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Genie refuses an id-keyed list that is not sorted by id."""
    return sorted(items, key=lambda item: item["id"])


def _dig(doc: Any, path: Sequence[str]) -> Any:
    for key in path:
        doc = doc.get(key) if isinstance(doc, Mapping) else None
    return doc


def _strings_in(doc: Any) -> Iterable[str]:
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, Mapping):
        for value in doc.values():
            yield from _strings_in(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _strings_in(value)


def _semantics(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    semantics = exposure.get("semantics")
    return semantics if isinstance(semantics, Mapping) else {}


def _agent_policy(exposure: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = exposure.get("policy")
    agent = policy.get("agentPolicy") if isinstance(policy, Mapping) else None
    return agent if isinstance(agent, Mapping) else {}


def _labels(raw: Any) -> Dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _text(raw: Any) -> str:
    """A contract string, stripped and on one line; ``""`` for none. A structured description
    (``$defs.descriptionObject``) gives its purpose."""
    if isinstance(raw, Mapping):
        raw = raw.get("purpose")
    if raw is None or isinstance(raw, (Mapping, list, bool)):
        return ""
    return " ".join(str(raw).split())


def _sentence(text: str) -> str:
    return text if not text or text[-1] in ".!?" else text + "."


def _split(raw: Any, *separators: str) -> List[str]:
    """``raw`` split on any of ``separators`` (or a list as is), stripped, empties dropped."""
    if isinstance(raw, list):
        items = [_text(item) for item in raw]
    else:
        text = _text(raw)
        for separator in separators[1:]:
            text = text.replace(separator, separators[0])
        items = [part.strip() for part in text.split(separators[0])] if text else []
    return [item for item in items if item]


def _string_list(raw: Any) -> List[str]:
    return [_text(item) for item in raw if _text(item)] if isinstance(raw, list) else []


def _folded(text: str) -> str:
    return " ".join(text.replace("_", " ").casefold().split())


def _balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth < 0:
            return False
    return depth == 0
