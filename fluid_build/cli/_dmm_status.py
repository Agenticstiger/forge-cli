# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid datamesh-manager status``: where each product of a migration stands in Entropy Data.

A migration publishes its products as drafts, verifies them and promotes the ones that pass
(``publish --status draft``, ``verify --out``, ``promote``). This is the read-only question
that runs through it: of the products these contracts declare, how many are active? It reads
the contracts (files, globs or folders, as ``publish`` and ``promote`` do), reads Entropy's
product list once (it pages at 1000 and is read by :meth:`DataMeshManagerProvider.list_products`),
and reports each product's status beside ``active N of M``. Nothing is written anywhere.

A contract that cannot be read is a row of its own (``unreadable``), not a reason to stop: a
report over a folder has to show the file that is wrong beside the ones that are not.

Light at import, like :mod:`fluid_build.cli._dmm_promote`: the provider is reached when the
command runs.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint_literal

__all__ = ["run_status"]

ACTIVE = "active"
#: The product is not in Entropy's list.
MISSING = "missing"
#: Entropy's list holds the product and carries no status for it.
UNKNOWN = "unknown"
#: The contract could not be read.
UNREADABLE = "unreadable"
#: The contract declares no id.
UNNAMED = "unnamed"
#: The words a row's status is when the tool, not Entropy, decided it: a status of the same
#: spelling from the server is shown as ``status-<word>``, so the two never read alike.
_OWN_WORDS = frozenset({MISSING, UNREADABLE, UNNAMED})

#: A status longer than this is cut: it is the server's text, and is printed.
_STATUS_MAX = 40


def _say(text: str) -> None:
    """One line of output, literal (no markup) and never word-wrapped: an id or a path
    must stay on one line, also when the output is piped."""
    cprint_literal(text, soft_wrap=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_word(status: Optional[str]) -> str:
    """A status as the server wrote it, made safe to print: Entropy's own words (``draft``,
    ``active``) pass unchanged, and any run of other characters (a newline, an escape
    sequence, markup) becomes one hyphen, so text from the server can neither start a line of
    its own nor run long. ``unknown`` when nothing is left."""
    word = re.sub(r"[^a-z0-9_.-]+", "-", (status or "").lower()).strip("-")
    if not word:
        return UNKNOWN
    return _clip(f"status-{word}" if word in _OWN_WORDS else word, _STATUS_MAX)


def _held_statuses(provider: Any) -> Dict[str, Optional[str]]:
    """``{product id: status}`` for every product Entropy holds: one read of its list.

    Unlike :meth:`DataMeshManagerProvider.held_products`, a list that cannot be read is an
    error here, not a quiet ``None``: a report that says every product is missing because the
    server did not answer would be wrong, not partial.
    """
    from fluid_build.providers.base import ProviderError
    from fluid_build.providers.datamesh_manager._lifecycle import product_status

    products = provider.list_products()
    if not isinstance(products, list):
        raise ProviderError(
            "Entropy Data answered the product list with something else than a list"
        )
    held: Dict[str, Optional[str]] = {}
    for product in products:
        if isinstance(product, dict) and product.get("id"):
            held[str(product["id"])] = product_status(product)
    return held


def _load(args: Any, log: logging.Logger, machine: bool) -> List[Dict[str, Any]]:
    """One row per contract the arguments name, in the order met: the contract, the product id
    it declares (the id Entropy knows it by) and, for a contract that cannot be used, its status
    and why. A usable one carries its id as written under ``as_written`` until Entropy's list is
    known. Nothing is sent anywhere."""
    import fluid_build.cli.datamesh_manager as dmm
    from fluid_build.cli._dmm_batch import (
        BatchItem,
        _declared_id,
        _one_line,
        refuse_duplicate_ids,
        resolve_contract_paths,
        stdout_to_stderr,
    )
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    rows: List[Dict[str, Any]] = []
    items: List[BatchItem] = []
    for label in resolve_contract_paths(list(args.contract)):
        try:
            with stdout_to_stderr(machine):
                contract = dmm.load_contract_with_overlay(
                    label, getattr(args, "overlay", None), log
                )
        except Exception as exc:  # noqa: BLE001 - one unreadable contract is a row, not the end
            rows.append(
                {
                    "contract": label,
                    "product_id": _declared_id(label),
                    "status": UNREADABLE,
                    "detail": _one_line(exc),
                }
            )
            continue
        raw_id = contract.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            rows.append(
                {
                    "contract": label,
                    "product_id": None,
                    "status": UNNAMED,
                    "detail": (
                        "the contract's id is not a string"
                        if raw_id is not None
                        else "the contract declares no id"
                    ),
                }
            )
            continue
        product_id = dmm_product_id(raw_id)
        items.append(BatchItem(label, contract, product_id))
        rows.append({"contract": label, "product_id": product_id, "as_written": raw_id})
    refuse_duplicate_ids(items)
    return rows


def _place(row: Dict[str, Any], held: Dict[str, Optional[str]]) -> None:
    """Set the status of a usable row from Entropy's list, and drop its ``as_written`` id."""
    raw_id = row.pop("as_written", None)
    if raw_id is None:
        return
    if row["product_id"] in held:
        row["status"] = _status_word(held[row["product_id"]])
    elif raw_id in held:
        # Entropy ids are case-sensitive: a product held as written is another product.
        row["status"] = MISSING
        row["detail"] = (
            f"Entropy Data holds {_clip(raw_id, 80)!r} as written; forge publishes it as "
            f"{row['product_id']!r}"
        )
    else:
        row["status"] = MISSING
        row["detail"] = "not in Entropy Data"


def run_status(args: Any, log: logging.Logger) -> int:
    """Report the status of the products the contracts declare. Returns 1 with ``--strict`` when
    any is not active, and when Entropy's list cannot be read."""
    import fluid_build.cli.datamesh_manager as dmm

    as_json = getattr(args, "format", "text") == "json"
    strict = bool(getattr(args, "strict", False))
    rows = _load(args, log, as_json)  # what is wrong with the arguments is said before any request
    provider = dmm._make_provider(args)
    provider._require_api_key()
    held = _held_statuses(provider)
    for row in rows:
        _place(row, held)

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    total, active = len(rows), counts.get(ACTIVE, 0)
    code = 1 if strict and active < total else 0
    if as_json:
        # Written straight to stdout, as ``fluid datamesh-manager list`` writes its JSON: the
        # console's redaction works on the serialised text and can swallow the closing quote
        # of a string that looks like a credential, so the document would not parse.
        sys.stdout.write(
            json.dumps(
                {
                    "products": rows,
                    "summary": {"total": total, "active": active, "by_status": counts},
                },
                indent=2,
            )
            + "\n"
        )
        sys.stdout.flush()
        return code
    from fluid_build.cli._dmm_batch import _one_line

    for row in rows:
        # One line per row whatever a contract holds: its id and path are files' text, and a
        # newline in them would start a row of their own.
        if row["status"] in (UNREADABLE, UNNAMED):
            name = _one_line(row["contract"])
            if row["product_id"]:
                name += f" (declares {_one_line(row['product_id'])})"
        else:
            name = _one_line(row["product_id"])
        detail = f" - {row['detail']}" if row.get("detail") else ""
        _say(f"  {row['status']:<10} {name}{detail}")
    others = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()) if status != ACTIVE)
    _say(f"Summary: active {active} of {total}" + (f" ({others})." if others else "."))
    return code
