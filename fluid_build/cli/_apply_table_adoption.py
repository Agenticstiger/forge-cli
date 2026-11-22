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
   reviewer must find it matching the contract, or the run stops naming the differences.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from fluid_build._contract_loader import CLIError
from fluid_build.cli.console import cprint_literal
from fluid_build.iac import runner
from fluid_build.iac.adoption import (
    ADOPT_TABLES_FLAG,
    TableReview,
    declared_resources,
    difference_lines,
    review_tables,
)
from fluid_build.iac.importer import ImportBlock, import_section
from fluid_build.iac.module import render_tofu_json

from ._logging import warn


def asked(args: Any) -> bool:
    """Did the run ask to adopt existing tables?"""
    return bool(getattr(args, "adopt_tables", False))


def tables_of(blocks: Sequence[Any]) -> List[Any]:
    """The imports among ``blocks`` that take over a table (``ImportBlock.table``)."""
    return [block for block in blocks if getattr(block, "table", None)]


def foreign_tables(plugin: Any, adopted: Sequence[Any], workdir: str, env: Any) -> List[Any]:
    """The tables among ``adopted`` that forge did not make: the ones ``--adopt-tables`` is for.

    A table that carries forge's own marker (``table_made_by_forge``: on AWS the Glue
    parameter ``managed_by: fluid`` its module sets when it creates the table, kept by
    ``ignore_changes``) is forge's, met again because its state was lost or kept nowhere
    (a fresh workspace on every run). It is re-attached as before, without the flag and
    without the check, so such a run can still change the table to the contract. Read
    from the state the look-up imported the tables into; a plugin without the capability,
    or a state that cannot be read, makes every table foreign.
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
    return [block for block in tables if not made_by_forge(values.get(block.to) or {})]


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
) -> List[TableReview]:
    """Review each table the saved plan adopts; refuse the run on any difference.

    ``shown`` is the saved plan as ``tofu show -json`` prints it, ``module`` the module
    text the tables are declared in. Returns the reviews of a run that goes on.
    """
    if not tables:
        return []
    named: Dict[str, str] = {block.to: str(block.table) for block in tables}
    review = getattr(plugin, "review_table_adoption", None)
    if shown is None:
        raise _unchecked(named, "`tofu show -json` could not read the saved plan")
    if not callable(review):
        raise _unchecked(named, "the provider plugin cannot compare them with the contract")
    reviews = review_tables(shown, named, declared_resources(module), review)
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
    for entry in reviews:
        # soft_wrap: a piped run (a CI log) keeps each line whole, as Rich would cut it at 80.
        cprint_literal(
            f"  adoption:    {entry.table} matches the contract: the plan imports it",
            soft_wrap=True,
        )
        if entry.updates:
            cprint_literal(
                "               and changes in place: " + "; ".join(entry.updates), soft_wrap=True
            )
    # Taking over a table is an ownership change: audited at WARNING, as the other
    # ownership and data-loss overrides are (``packaging_adoption_override``).
    warn(logger, "table_adoption_accepted", tables=[entry.as_event() for entry in reviews])
    return reviews


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
