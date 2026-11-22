# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--adopt-tables`` in the OpenTofu engine (``cli/_apply_opentofu_engine.py``).

The rules are :mod:`fluid_build.iac.adoption`'s; this module applies them to a run of
``fluid apply`` or ``fluid plan --tofu``:

1. :func:`before_lookup`: with the flag, a table the contract names that the run cannot
   look for (no import id) is refused before anything is imported;
2. :func:`foreign_tables`, then :func:`refuse_unless_asked`: the brownfield look-up
   (``_adopt_existing``) found tables that exist and that the state does not hold;
   one forge made is re-attached as before, and for any other the run stops without
   the flag, naming them;
3. :func:`write_import_blocks`: with the flag, each found table goes into the module as
   an OpenTofu ``import`` block, so the plan shows its import and writes no state;
4. :func:`check_plan`: the saved plan must import each one unreplaced, and the plugin's
   comparison must find it matching the contract, or the run stops naming the differences;
5. :func:`hold_still` and :func:`check_held`: a table the plugin keeps as it is while it is
   adopted (an Iceberg table on AWS) and that the plan would still update in place is
   planned again with those attributes in ``ignore_changes``, and must then be imported
   with no change at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build._contract_loader import CLIError
from fluid_build.cli.console import cprint_literal
from fluid_build.iac import runner
from fluid_build.iac.adoption import (
    ADOPT_TABLES_FLAG,
    TableReview,
    declared_resources,
    difference_lines,
    planned_imports,
    review_tables,
)
from fluid_build.iac.importer import ImportBlock, import_section
from fluid_build.iac.module import render_tofu_json

from ._logging import warn


@dataclass(frozen=True)
class Adoption:
    """What a run's adoption check found: the reviews of the tables it adopts, and the ones
    it keeps as they are while adopting them (``{address: attributes}``)."""

    reviews: Tuple[TableReview, ...] = ()
    held: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


def asked(args: Any) -> bool:
    """Did the run ask to adopt existing tables?"""
    return bool(getattr(args, "adopt_tables", False))


def tables_of(blocks: Sequence[Any]) -> List[Any]:
    """The imports among ``blocks`` that take over a table (``ImportBlock.table``)."""
    return [block for block in blocks if getattr(block, "table", None)]


def foreign_tables(
    plugin: Any, adopted: Sequence[Any], workdir: str, env: Any, contract: Mapping[str, Any]
) -> List[Any]:
    """The tables among ``adopted`` that forge did not make for this product: the ones
    ``--adopt-tables`` is for.

    A table that carries forge's own marker for this contract (``table_made_by_forge``: on
    AWS the Glue parameters ``managed_by: fluid`` and the recorded contract its module
    sets when it creates the table, kept by ``ignore_changes``) is this product's, met
    again because its state was lost or kept nowhere (a fresh workspace on every run). It
    is re-attached as before, without the flag and without the check, so such a run can
    still change the table to the contract. Read from the state the look-up imported the
    tables into; a plugin without the capability, or a state that cannot be read, makes
    every table foreign.
    """
    tables = tables_of(adopted)
    made_by_forge = getattr(plugin, "table_made_by_forge", None)
    if not tables or not callable(made_by_forge):
        return tables
    values: Dict[str, Any] = {
        str(resource.get("address") or ""): resource.get("values") or {}
        for resource in runner.tofu_state_resources(workdir, env=env)
        if isinstance(resource, Mapping)
    }
    return [block for block in tables if not made_by_forge(values.get(block.to) or {}, contract)]


def before_lookup(
    plugin: Any, provider: str, contract: Mapping[str, Any], actions: Any, args: Any
) -> None:
    """With ``--adopt-tables``: say so when the plugin adopts no table on request, and
    refuse a run that cannot look for a table the contract names."""
    if not asked(args):
        return
    named = getattr(plugin, "adoptable_tables", None)
    if not callable(named):
        cprint_literal(
            f"  adoption:    {ADOPT_TABLES_FLAG} changes nothing for {provider}: forge adopts "
            "no tables on request there, and adopts what it finds as before",
            soft_wrap=True,
        )
        return
    tables: Mapping[str, str] = named(contract)
    looked_for = {block.to for block in tables_of(plugin.discover_imports(contract, actions))}
    missing = sorted(name for address, name in tables.items() if address not in looked_for)
    if not missing:
        return
    hint = str(getattr(plugin, "table_adoption_hint", "") or "")
    raise CLIError(
        1,
        "table_adoption_unavailable",
        {
            "error": f"{ADOPT_TABLES_FLAG} could not look for {', '.join(missing)}: there "
            "is no import id for "
            + ("it" if len(missing) == 1 else "them")
            + (f". {hint}" if hint else ""),
            "tables": missing,
            "remediation": [hint] if hint else [],
        },
    )


def refuse_unless_asked(args: Any, tables: Sequence[Any]) -> None:
    """Stop a run that found existing tables the state does not hold, unless it asked
    to adopt them. Raised inside ``_imports_kept_only_by_an_apply``, or after it, so
    nothing the look-up imported stays in state."""
    if not tables or asked(args):
        return
    names = sorted(str(block.table) for block in tables)
    raise CLIError(
        1,
        "table_adoption_requires_flag",
        {
            "error": f"{len(names)} table(s) the contract names already exist, and this "
            f"OpenTofu state does not manage them: {', '.join(names)}. forge takes over an "
            "existing table only when asked",
            "tables": names,
            "remediation": [
                f"To bring them under forge, re-run with {ADOPT_TABLES_FLAG}: each is adopted "
                "only if it matches the contract (its columns, location and format), and "
                "one that differs is refused with its differences",
                "If a state that manages them was lost or moved, point --state-backend at it",
                "To leave a table outside forge, remove it from the contract's exposes",
            ],
        },
    )


def write_import_blocks(module_path: Path, blocks: Sequence[ImportBlock]) -> None:
    """Add an ``import`` block per entry of ``blocks`` to the module at ``module_path``.

    Through the module renderer (``iac.module.render_tofu_json``): an import id carries
    contract text (a Glue database or table name), and OpenTofu reads a ``${...}`` or
    ``%{...}`` in a ``.tf.json`` string as a template, so ``file("...")`` there would
    read the host's files into the plan. Escaped, it is the literal id.
    """
    if not blocks:
        return
    rendered = json.loads(render_tofu_json(import_section(list(blocks))))
    document = json.loads(module_path.read_text(encoding="utf-8"))
    document.setdefault("import", []).extend(rendered.get("import", []))
    module_path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def check_plan(
    plugin: Any,
    module: str,
    tables: Sequence[Any],
    shown: Optional[Mapping[str, Any]],
    logger: logging.Logger,
) -> Adoption:
    """Review each table the saved plan adopts; refuse the run on any difference.

    ``shown`` is the saved plan as ``tofu show -json`` prints it, ``module`` the module
    text the tables are declared in. Returns what a run that goes on adopts, with the
    tables it keeps as they are (:func:`hold_still`).
    """
    if not tables:
        return Adoption()
    named: Dict[str, str] = {block.to: str(block.table) for block in tables}
    review = getattr(plugin, "review_table_adoption", None)
    if shown is None:
        raise _unchecked(named, "`tofu show -json` could not read the saved plan")
    if not callable(review):
        raise _unchecked(named, "the provider plugin cannot compare them with the contract")
    declared = declared_resources(module)
    reviews = review_tables(shown, named, declared, review)
    refused = [entry for entry in reviews if not entry.matches]
    if refused:
        raise CLIError(
            1,
            "table_adoption_mismatch",
            {
                "error": f"{len(refused)} of the {len(reviews)} table(s) to adopt differ from "
                "the contract, so none was adopted and nothing was applied:\n"
                + "\n".join(difference_lines(refused)),
                "tables": [entry.as_event() for entry in refused],
                "remediation": [
                    "Change the contract to describe each table as it is (its schema and "
                    f"binding), or change the table, then re-run with {ADOPT_TABLES_FLAG}",
                    "To leave a table outside forge, remove it from the contract's exposes",
                ],
            },
        )
    held = _held(plugin, shown, declared, reviews)
    for entry in reviews:
        # soft_wrap: a piped run (a CI log) keeps each line whole, as Rich would cut it at 80.
        cprint_literal(
            f"  adoption:    {entry.table} matches the contract: the plan imports it",
            soft_wrap=True,
        )
        if entry.updates and entry.address in held:
            cprint_literal(
                "               and leaves as it is (an adopted Iceberg table is not updated "
                "in place): " + "; ".join(entry.updates),
                soft_wrap=True,
            )
        elif entry.updates:
            cprint_literal(
                "               and changes in place: " + "; ".join(entry.updates), soft_wrap=True
            )
    return Adoption(tuple(reviews), held)


def _held(
    plugin: Any,
    shown: Mapping[str, Any],
    declared: Mapping[str, Mapping[str, Any]],
    reviews: Sequence[TableReview],
) -> Dict[str, Tuple[str, ...]]:
    """The adopted tables the plan updates in place that the plugin keeps as they are."""
    hold = getattr(plugin, "adoption_hold", None)
    if not callable(hold):
        return {}
    imports = planned_imports(shown)
    held: Dict[str, Tuple[str, ...]] = {}
    for entry in reviews:
        change = (imports.get(entry.address) or {}).get("change") or {}
        attributes = tuple(hold(declared.get(entry.address) or {}))
        if attributes and list(change.get("actions") or []) != ["no-op"]:
            held[entry.address] = attributes
    return held


def hold_still(module_path: Path, adoption: Adoption) -> bool:
    """Add each held table's attributes to its ``ignore_changes`` in the module at
    ``module_path``, for this run's plan only (the module is written again on every run).
    Returns whether there was anything to hold."""
    if not adoption.held:
        return False
    document = json.loads(module_path.read_text(encoding="utf-8"))
    resources = document.get("resource") or {}
    for address, attributes in sorted(adoption.held.items()):
        kind, _, name = address.partition(".")
        body = (resources.get(kind) or {}).get(name)
        if not isinstance(body, dict):
            raise _not_held(address, "the module does not declare it")
        lifecycle = body.setdefault("lifecycle", {})
        ignored = list(lifecycle.get("ignore_changes") or [])
        lifecycle["ignore_changes"] = ignored + [a for a in attributes if a not in ignored]
    module_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return True


def check_held(shown: Optional[Mapping[str, Any]], adoption: Adoption) -> None:
    """The plan made again must import every held table with no change at all."""
    imports = planned_imports(shown or {})
    for address in sorted(adoption.held):
        change = (imports.get(address) or {}).get("change") or {}
        actions = [str(action) for action in change.get("actions") or []]
        if actions != ["no-op"]:
            raise _not_held(address, f"the plan still {'/'.join(actions) or 'leaves it out'}")


def _not_held(address: str, why: str) -> CLIError:
    return CLIError(
        1,
        "table_adoption_mismatch",
        {
            "error": f"{address} could not be planned to be adopted without an in-place "
            f"update ({why}); an adopted Iceberg table is never updated in place, so "
            "nothing was adopted",
            "tables": [{"address": address}],
        },
    )


def audit_take_over(logger: logging.Logger, tables: Sequence[Any]) -> None:
    """The run is about to apply a plan that takes ``tables`` over (reviews, or the names a
    saved plan recorded). An ownership change, audited at WARNING as the other ownership
    and data-loss overrides are (``packaging_adoption_override``); never for a run that
    applies nothing (a dry run, a refusal)."""
    if not tables:
        return
    described = [t.as_event() if isinstance(t, TableReview) else {"table": str(t)} for t in tables]
    warn(logger, "table_adoption_accepted", tables=described)


def _unchecked(named: Mapping[str, str], why: str) -> CLIError:
    """The tables could not be compared with the contract, so none is adopted."""
    return CLIError(
        1,
        "table_adoption_mismatch",
        {
            "error": f"{why}, so the tables to adopt ({', '.join(sorted(named.values()))}) "
            "could not be checked against the contract; nothing was adopted",
            "tables": [{"table": name, "address": a} for a, name in sorted(named.items())],
        },
    )


def say_bound(record: Mapping[str, Any]) -> None:
    """``fluid apply plan.json``: name the tables its saved plan adopts."""
    adopted = record.get("adoptedTables")
    if isinstance(adopted, list) and adopted:
        cprint_literal(
            "  adoption:    the saved plan adopts " + ", ".join(str(t) for t in adopted),
            soft_wrap=True,
        )
