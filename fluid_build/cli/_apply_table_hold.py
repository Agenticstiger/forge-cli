# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Live Iceberg tables in the OpenTofu engine (``cli/_apply_opentofu_engine.py``): left as
they are, never updated in place, unless ``--allow-iceberg-update``.

The rule is :mod:`fluid_build.iac.table_hold`'s; this module applies it to a run of
``fluid apply`` or ``fluid plan --tofu``, whatever brought the table into the plan: a
table the state holds, one a fresh state re-attaches by import, one ``--adopt-tables``
adopts.

1. :func:`planned_updates`: every resource the saved plan updates in place that the
   plugin keeps as it is (``update_hold``); :func:`to_hold` drops them all when the run
   asked for ``--allow-iceberg-update``;
2. :func:`hold_still`, then the engine plans again, then :func:`check_held`: the held
   attributes go into the table's ``ignore_changes`` for this run's plan only (the module
   is written again on every run), and the plan made again must leave the table with no
   change at all;
3. :func:`say_held`: what the contract wanted of them, with the remedy, printed and logged
   at WARNING (``iceberg_table_update_held``), so a CI log finds it;
4. :func:`review`, the backstop of the data-loss review: an in-place update of such a
   table still in the plan is refused (``iceberg_table_update_in_place``), unless the run
   asked for it; a saved plan only when its record names the table (``icebergUpdates``,
   under ``planDigest``), so an approved ``plan.json`` cannot gain one later;
5. :func:`audit_updated`: a run that applies such an update audits it at WARNING
   (``iceberg_table_update_accepted``).

A refusal by default with an explicit, audited override is the plan-policy pattern of
Atlantis (``conftest`` over the plan JSON, then ``atlantis approve_policies``) and of
Spacelift's plan policies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from fluid_build._contract_loader import CLIError
from fluid_build.cli.console import cprint_literal
from fluid_build.iac.adoption import declared_resources
from fluid_build.iac.table_hold import ALLOW_ICEBERG_UPDATE_FLAG, TableHold

from ._logging import warn

#: How to apply what a held table's contract wants, in every message that names one.
REMEDY = (
    "To apply it, change it through the table's Iceberg catalog (an ALTER TABLE in Spark "
    "or Athena: Iceberg's commit updates Glue with the version it read), change the "
    f"contract to describe the table as it is, or pause the table's writers and re-run "
    f"with {ALLOW_ICEBERG_UPDATE_FLAG}"
)


@dataclass(frozen=True)
class Held:
    """One resource the saved plan updates in place that the plugin keeps as it is."""

    address: str
    hold: TableHold
    #: The plan imports it (an ``import`` block: ``--adopt-tables``, or ``fluid plan
    #: --tofu`` re-attaching a table forge made).
    imported: bool = False

    @property
    def table(self) -> str:
        return self.hold.table

    def as_event(self) -> Dict[str, Any]:
        return {"address": self.address, **self.hold.as_event()}


def asked(args: Any) -> bool:
    """Did the run ask to update live Iceberg tables in place?"""
    return bool(getattr(args, "allow_iceberg_update", False))


def may_hold(plugin: Any, changes: Mapping[str, int]) -> bool:
    """Can the plan hold an update the plugin keeps a table from? Only a plan that updates
    something in place, with a plugin that says which tables it keeps as they are."""
    return callable(getattr(plugin, "update_hold", None)) and int(changes.get("change", 0)) > 0


def planned_updates(plugin: Any, module: str, shown: Any) -> Dict[str, Held]:
    """``{address: held}`` of every resource the saved plan updates in place (actions
    exactly ``update``, an import's included) that the plugin keeps as it is.

    ``shown`` is the saved plan as ``tofu show -json`` prints it, ``module`` the module
    text the resources are declared in. A replace is not here: it stays with the data-loss
    gate.
    """
    hold = getattr(plugin, "update_hold", None)
    if not callable(hold) or not isinstance(shown, Mapping):
        return {}
    declared = declared_resources(module)
    found: Dict[str, Held] = {}
    for address, change, detail in _in_place(shown):
        table_hold = hold(change, declared.get(address) or {})
        if isinstance(table_hold, TableHold) and table_hold.attributes:
            found[address] = Held(address, table_hold, detail.get("importing") is not None)
    return found


def _in_place(
    shown: Mapping[str, Any],
) -> Iterator[Tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    """``(address, resource change, change)`` of each managed resource the plan updates in
    place."""
    for change in shown.get("resource_changes") or []:
        if not isinstance(change, Mapping) or change.get("mode", "managed") != "managed":
            continue
        detail = change.get("change")
        if isinstance(detail, Mapping) and [str(a) for a in detail.get("actions") or []] == [
            "update"
        ]:
            yield str(change.get("address") or ""), change, detail


def to_hold(args: Any, updates: Mapping[str, Held]) -> Dict[str, Held]:
    """The updates the run holds: all of them, unless it asked to make them."""
    return {} if asked(args) else dict(updates)


def hold_still(module_path: Path, held: Mapping[str, Held]) -> bool:
    """Add each held table's attributes to its ``ignore_changes`` in the module at
    ``module_path``, for this run's plan only (the module is written again on every run).
    Returns whether there was anything to hold."""
    if not held:
        return False
    document = json.loads(module_path.read_text(encoding="utf-8"))
    resources = document.get("resource") or {}
    for address, entry in sorted(held.items()):
        kind, _, name = address.partition(".")
        body = (resources.get(kind) or {}).get(name)
        if not isinstance(body, dict):
            raise _still_updated(entry, "the module does not declare it", adopted=False)
        lifecycle = body.setdefault("lifecycle", {})
        ignored = list(lifecycle.get("ignore_changes") or [])
        lifecycle["ignore_changes"] = ignored + [
            attribute for attribute in entry.hold.attributes if attribute not in ignored
        ]
    module_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return True


def check_held(
    shown: Optional[Mapping[str, Any]],
    held: Mapping[str, Held],
    adopted: Collection[str] = (),
) -> None:
    """The plan made again must leave every held table with no change at all (an imported
    one imported with none). ``adopted``: the addresses ``--adopt-tables`` adopts, whose
    refusal keeps the adoption's own event."""
    planned: Dict[str, Mapping[str, Any]] = {}
    for change in (shown or {}).get("resource_changes") or []:
        if isinstance(change, Mapping):
            planned[str(change.get("address") or "")] = change
    for address, entry in sorted(held.items()):
        detail = (planned.get(address) or {}).get("change") or {}
        actions = [str(action) for action in detail.get("actions") or []]
        if actions != ["no-op"]:
            why = f"the plan still {'/'.join(actions) or 'leaves it out'}"
            raise _still_updated(entry, why, adopted=address in adopted)


def say_held(logger: logging.Logger, held: Mapping[str, Held]) -> None:
    """Print what each held table's contract wanted, with the remedy, and log it."""
    if not held:
        return
    for entry in held.values():
        # soft_wrap: a piped run (a CI log) keeps each line whole; Rich would cut it at 80.
        cprint_literal(
            f"  iceberg:     {entry.table} is a live Iceberg table ({entry.hold.why}): the "
            "plan leaves it as it is, as an in-place update could revert a commit its "
            "writers make meanwhile. Not applied:",
            soft_wrap=True,
        )
        for line in entry.hold.drift or ("(the plan changes nothing it names)",):
            cprint_literal(f"                 {line}", soft_wrap=True)
    cprint_literal(f"               {REMEDY}", soft_wrap=True)
    warn(
        logger,
        "iceberg_table_update_held",
        tables=[entry.as_event() for entry in held.values()],
        remediation=REMEDY,
    )


def review(
    plugin: Any,
    module: str,
    shown: Any,
    args: Any,
    changes: Mapping[str, int],
    *,
    recorded: Optional[Sequence[str]] = None,
    enforce: bool = True,
) -> List[Held]:
    """The backstop of the data-loss review: the in-place updates the plugin keeps a table
    from that are still in the plan the run applies. Returns those the run may make (to
    audit); refuses the others, or, with ``enforce=False`` (``fluid plan --tofu``), says
    that ``fluid apply`` will.

    A run that plans (``fluid apply <contract>``, ``fluid plan --tofu``) may make them
    with ``--allow-iceberg-update``. A saved plan (``fluid apply plan.json``, ``recorded``
    its record's ``icebergUpdates``) may make only those its record names, whatever the
    apply's flags. ``shown`` is ``None`` when the plan could not be read back: refused
    whatever the flags, as nothing can say which tables it updates. A plan that updates
    nothing in place, or a plugin that keeps no table from it, passes (:func:`may_hold`).
    """
    if not may_hold(plugin, changes):
        return []
    if shown is None:
        _refuse(
            "the saved plan could not be read back, so an in-place update of a live Iceberg "
            "table cannot be ruled out",
            [],
            enforce=enforce,
            remediation=[
                "Re-run the apply: its plan is made again. If `tofu show -json` still cannot "
                "read it, check that the OpenTofu version and providers in the workdir can "
                "read their own plan; no flag lets an unread plan through"
            ],
        )
        return []
    updates = planned_updates(plugin, module, shown)
    if not updates:
        return []
    if recorded is None:
        allowed = set(updates) if asked(args) else set()
    else:
        named = set(recorded)
        allowed = {address for address, entry in updates.items() if entry.table in named}
    # A saved plan named its tables already (:func:`say_bound`), and the apply audits them.
    for address in sorted(allowed) if recorded is None else ():
        entry = updates[address]
        cprint_literal(
            f"  iceberg:     {entry.table} is updated in place ({ALLOW_ICEBERG_UPDATE_FLAG}); "
            "a commit its writers make during the apply can be reverted: pause them first",
            soft_wrap=True,
        )
    refused = [entry for address, entry in sorted(updates.items()) if address not in allowed]
    if refused:
        why = (
            "the saved plan updates in place live Iceberg tables its record does not name"
            if recorded is not None
            else "the plan updates live Iceberg tables in place"
        )
        _refuse(why, refused, enforce=enforce, bound=recorded is not None)
    return [updates[address] for address in sorted(allowed)]


def recorded(record: Mapping[str, Any]) -> List[str]:
    """The tables a saved plan's record lets it update in place (``icebergUpdates``)."""
    names = record.get("icebergUpdates")
    return [str(name) for name in names] if isinstance(names, list) else []


def record_field(updated: Sequence[Held]) -> Dict[str, Any]:
    """``fluid plan --tofu``'s record of the tables its saved plan updates in place, under
    ``planDigest``: an approval of ``plan.json`` is an approval of these updates."""
    return {"icebergUpdates": sorted(entry.table for entry in updated)} if updated else {}


def say_bound(args: Any, record: Mapping[str, Any]) -> List[str]:
    """``fluid apply plan.json``: name the tables the saved plan updates in place, and say
    that the apply's own flag decides nothing there. Returns those names (:func:`recorded`)."""
    names = recorded(record)
    if names:
        cprint_literal(
            "  iceberg:     the saved plan updates in place, as its plan asked "
            f"({ALLOW_ICEBERG_UPDATE_FLAG}): " + ", ".join(names),
            soft_wrap=True,
        )
    if asked(args):
        cprint_literal(
            f"  iceberg:     {ALLOW_ICEBERG_UPDATE_FLAG} changes nothing here: a saved plan "
            "updates in place only the tables its record names; give the flag to "
            "`fluid plan --tofu` instead",
            soft_wrap=True,
        )
    return names


def audit_updated(logger: logging.Logger, updated: Sequence[Held]) -> None:
    """The run is about to apply a plan that updates live Iceberg tables in place. A safety
    override, audited at WARNING as the data-loss and ownership overrides are
    (``opentofu_destructive_gate_override``); never for a run that applies nothing (a dry
    run, a refusal)."""
    if not updated:
        return
    cprint_literal(
        f"\n  ⚠️  {ALLOW_ICEBERG_UPDATE_FLAG}: "
        + ", ".join(entry.table for entry in updated)
        + " updated in place with no VersionId: a commit their writers make during the "
        "apply can be reverted",
        soft_wrap=True,
    )
    warn(
        logger,
        "iceberg_table_update_accepted",
        tables=[entry.as_event() for entry in updated],
    )


def _refuse(
    why: str,
    refused: Sequence[Held],
    *,
    enforce: bool,
    bound: bool = False,
    remediation: Optional[List[str]] = None,
) -> None:
    if not enforce:
        unless = "" if remediation else f" unless the plan is made with {ALLOW_ICEBERG_UPDATE_FLAG}"
        cprint_literal(f"  ⚠️  {why}: `fluid apply` will refuse it{unless}", soft_wrap=True)
        return
    names = [entry.table for entry in refused]
    if remediation is None:
        remediation = [REMEDY]
    if bound:
        remediation = [
            "Make the plan again with `fluid plan --tofu`: it leaves these tables as they "
            f"are, or, with the writers paused, updates them with {ALLOW_ICEBERG_UPDATE_FLAG}",
            REMEDY,
        ]
    raise CLIError(
        1,
        "iceberg_table_update_in_place",
        {
            "error": f"{why}"
            + (f" ({', '.join(names)})" if names else "")
            + ": hashicorp/aws sends UpdateTable with no VersionId, so a commit a writer "
            "makes during the apply could be reverted; nothing was applied",
            "tables": [entry.as_event() for entry in refused],
            "remediation": remediation,
        },
    )


def _still_updated(entry: Held, why: str, *, adopted: bool) -> CLIError:
    """The plan made again still updates a held table: refused, nothing applied. A table
    ``--adopt-tables`` adopts keeps the adoption's own event."""
    if adopted:
        return CLIError(
            1,
            "table_adoption_mismatch",
            {
                "error": f"{entry.address} could not be planned to be adopted without an "
                f"in-place update ({why}); an adopted Iceberg table is never updated in "
                "place, so nothing was adopted",
                "tables": [{"address": entry.address}],
            },
        )
    return CLIError(
        1,
        "iceberg_table_update_in_place",
        {
            "error": f"{entry.table} ({entry.address}) could not be planned without an "
            f"in-place update ({why}); a live Iceberg table is never updated in place "
            f"without {ALLOW_ICEBERG_UPDATE_FLAG}, so nothing was applied",
            "tables": [entry.as_event()],
            "remediation": [REMEDY],
        },
    )
