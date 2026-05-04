# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid contract`` subcommand surface.

Subcommands:

* ``apply-suggestion <suggestion-file>`` — merge a forge-generated
  ``<contract>.suggested.json`` (with per-field provenance annotations)
  into a target contract YAML/JSON. Hard-rejects any AI-provenance
  values that land on safety-critical paths.
* ``migrate-product-type`` — fill the missing twin of ``metadata.layer``
  / ``metadata.productType`` (Bronze↔SDP, Silver↔ADP, Gold↔CDP) on
  every contract under cwd. Equivalence axiom enforcement: the user
  should never have to remember which spelling lives in which field.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from ._common import CLIError
from ._forge_ai_guardrails import (
    GuardrailViolation,
    apply_suggestion,
    read_suggestion_file,
)

COMMAND = "contract"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(COMMAND, help="Inspect / mutate FLUID contracts")
    sub = p.add_subparsers(dest="contract_subcmd", required=True)

    apply_p = sub.add_parser(
        "apply-suggestion",
        help="Merge a *.suggested.json file into a target contract",
    )
    apply_p.add_argument("suggestion", help="Path to <contract>.suggested.json")
    apply_p.add_argument(
        "--target",
        required=True,
        help="Path to the target contract YAML/JSON to merge into",
    )
    apply_p.add_argument(
        "--accept-provenance",
        nargs="*",
        choices=["ai", "introspection", "template", "user"],
        default=None,
        help="Accept only fields with these provenance kinds (default: all)",
    )
    apply_p.add_argument(
        "--out",
        help="Output path. Default: overwrite --target after a one-line backup",
    )
    apply_p.set_defaults(cmd=COMMAND, func=_run_apply_suggestion)

    migrate_p = sub.add_parser(
        "migrate-product-type",
        help=(
            "Fill the missing twin of metadata.layer / metadata.productType "
            "(Bronze↔SDP, Silver↔ADP, Gold↔CDP) on contracts "
            "under cwd. --check exits non-zero if any contract is "
            "incomplete; --write rewrites the contract files in place."
        ),
    )
    migrate_p.add_argument(
        "--write",
        action="store_true",
        help=("Rewrite contract files in place (default: dry-run / report " "what would change)."),
    )
    migrate_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero when any contract still has only one of "
            "layer/productType set after the dry-run pass. Useful as a CI "
            "gate."
        ),
    )
    migrate_p.add_argument(
        "--root",
        default=".",
        help=(
            "Directory to walk for ``**/*.fluid.yaml`` (default: cwd). "
            "Skips ``.git/`` and ``__pycache__/`` automatically."
        ),
    )
    migrate_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "Skip the interactive confirmation prompt that fires before "
            "``--write`` rewrites contract files in place. Required for "
            "non-interactive / CI usage; ignored without ``--write``."
        ),
    )
    migrate_p.set_defaults(cmd=COMMAND, func=_run_migrate_product_type)


def _load_contract(path: Path) -> Dict[str, Any]:
    body = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(body) or {}
    return json.loads(body)


def _dump_contract(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in (".yaml", ".yml"):
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _run_apply_suggestion(args, logger: logging.Logger) -> int:
    suggestion_path = Path(args.suggestion)
    target_path = Path(args.target)

    if not suggestion_path.exists():
        raise CLIError(1, "suggestion_not_found", {"path": str(suggestion_path)})
    if not target_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(target_path)})

    try:
        suggestion = read_suggestion_file(suggestion_path)
    except Exception as exc:  # noqa: BLE001
        raise CLIError(1, "suggestion_parse_failed", {"error": str(exc)}) from exc

    contract = _load_contract(target_path)

    accept = tuple(args.accept_provenance) if args.accept_provenance else None
    try:
        merged = apply_suggestion(contract, suggestion, accept_provenance=accept)
    except GuardrailViolation as exc:
        # The guardrails reject AI-provenance values on safety-critical
        # paths. Surface the message verbatim — it tells the user
        # exactly which path was blocked and why.
        logger.error(str(exc))
        raise CLIError(1, "ai_guardrail_violation", {"reason": str(exc)}) from exc

    out_path = Path(args.out) if args.out else target_path
    if not args.out:
        backup = target_path.with_suffix(target_path.suffix + ".bak")
        backup.write_bytes(target_path.read_bytes())
        logger.info(f"Backup written: {backup}")
    _dump_contract(out_path, merged)
    logger.info(f"Merged {len(suggestion.fields)} field(s) → {out_path}")
    return 0


# ---------------------------------------------------------------------------
# migrate-product-type — Phase 1.2 of the world-class plan
# ---------------------------------------------------------------------------


_SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _walk_contracts(root: Path) -> List[Path]:
    """Find every ``*.fluid.yaml`` under ``root``, skipping noisy dirs."""
    out: List[Path] = []
    for path in root.rglob("*.fluid.yaml"):
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _twin_status(metadata: Dict[str, Any]) -> Tuple[str, str]:
    """Return (layer, productType) where each is "" if missing."""
    return (
        str(metadata.get("layer") or ""),
        str(metadata.get("productType") or ""),
    )


def _run_migrate_product_type(args, logger: logging.Logger) -> int:
    """Walk every contract under ``--root`` and fill the missing twin
    of ``metadata.layer`` / ``metadata.productType``.

    Three modes:

    * default (no flag) — dry-run, prints which files would change.
    * ``--write`` — rewrites the contract files in place (preserves the
      caller's serialisation format: yaml stays yaml, json stays json).
    * ``--check`` — like dry-run, but exits non-zero if any contract has
      only one twin set. Useful as a CI gate.

    Both ``--write`` and ``--check`` may be combined: rewrite, then
    fail if the rewrite didn't fully resolve every contract.
    """
    # Lazy import — keeps the cold start of `fluid contract` cheap when
    # the user is only running ``apply-suggestion``.
    from fluid_build.forge.product_types import normalize_metadata_in_place

    root = Path(args.root).resolve()
    if not root.exists():
        raise CLIError(1, "migrate_root_not_found", {"path": str(root)})

    paths = _walk_contracts(root)
    if not paths:
        logger.info(f"No *.fluid.yaml contracts found under {root}")
        return 0

    # Pass 1 — dry-run scan. Plan every change, gather counts. We
    # ALWAYS scan first so the operator gets a preview before any
    # filesystem mutation. ``--write`` only flips the write switch
    # AFTER the optional confirmation prompt.
    n_changed = 0
    n_already_complete = 0
    n_incomplete_after_normalize = 0
    incomplete_paths: List[Path] = []
    pending_writes: List[Tuple[Path, Dict[str, Any]]] = []  # (path, normalised contract)

    for path in paths:
        try:
            contract = _load_contract(path)
        except Exception as exc:  # noqa: BLE001 — broken YAML shouldn't kill the run
            logger.warning(f"  ⚠️  {path}: parse error ({exc}); skipped")
            continue

        if not isinstance(contract, dict):
            logger.warning(f"  ⚠️  {path}: not a dict; skipped")
            continue

        metadata = contract.get("metadata")
        if not isinstance(metadata, dict):
            logger.warning(f"  ⚠️  {path}: no metadata block; skipped")
            continue

        before_layer, before_type = _twin_status(metadata)

        # Deterministic, idempotent — fills the missing twin from the
        # canonical mapping in product_types.PRODUCT_TYPES.
        normalize_metadata_in_place(metadata)

        after_layer, after_type = _twin_status(metadata)
        changed = (before_layer, before_type) != (after_layer, after_type)

        if changed:
            n_changed += 1
            verb = "would write" if not args.write else "rewrote"
            logger.info(
                f"  ✏️  {path}: {verb} layer={after_layer or '<unset>'} "
                f"productType={after_type or '<unset>'} "
                f"(was layer={before_layer or '<unset>'} "
                f"productType={before_type or '<unset>'})"
            )
            if args.write:
                pending_writes.append((path, contract))
        elif after_layer and after_type:
            n_already_complete += 1
        else:
            # Neither field set — out of scope for this normaliser; the
            # contract was authored without any layer / productType
            # metadata, which is a separate authoring problem.
            n_incomplete_after_normalize += 1
            incomplete_paths.append(path)

    # Pass 2 — confirm + write. ``--write`` without ``--yes`` prompts
    # interactively; non-interactive / CI users pass ``--yes`` to
    # skip. Mirrors the apply / forge confirm pattern.
    #
    # SECURITY (S-015): when stdin OR stdout is non-TTY (piped input,
    # CI runner with redirected output, ``script /dev/null``, etc.),
    # an attacker could pipe ``echo y`` into the prompt to auto-
    # confirm a destructive ``--write`` even though the operator
    # intended an interactive review. Require ``--yes`` explicitly in
    # any non-interactive context — there is no scenario where a
    # human typing into a TTY would accidentally trigger this gate.
    if pending_writes and args.write:
        if not getattr(args, "yes", False):
            import sys as _sys

            try:
                fully_interactive = _sys.stdin.isatty() and _sys.stdout.isatty()
            except Exception:  # noqa: BLE001
                fully_interactive = False
            if not fully_interactive:
                # Refuse silently-piped destructive writes. The exit
                # code + typed event lets CI users wire the gate into
                # an explicit ``--yes`` invocation rather than
                # accidentally letting it through.
                logger.error(
                    "Refusing to rewrite contracts non-interactively without "
                    "--yes. Stdin or stdout is not a TTY (CI / piped); "
                    "re-run with --yes to confirm."
                )
                raise CLIError(
                    1,
                    "interactive_write_requires_yes",
                    {
                        "pending_writes": len(pending_writes),
                        "stdin_tty": _sys.stdin.isatty(),
                        "stdout_tty": _sys.stdout.isatty(),
                    },
                )
            prompt = (
                f"\nAbout to rewrite {len(pending_writes)} contract file(s) "
                "in place. Continue? [y/N]: "
            )
            try:
                answer = input(prompt).strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = ""
            if answer not in ("y", "yes"):
                logger.info(
                    "Migration cancelled — no files were rewritten. "
                    "Re-run with --yes to skip the prompt."
                )
                return 0
        for path, contract_dict in pending_writes:
            _dump_contract(path, contract_dict)

    logger.info(
        f"Scanned {len(paths)} contract(s) under {root}: "
        f"{n_changed} {'rewritten' if args.write else 'would be rewritten'}, "
        f"{n_already_complete} already complete, "
        f"{n_incomplete_after_normalize} still missing both twins."
    )

    if args.check and (n_incomplete_after_normalize > 0 or (n_changed > 0 and not args.write)):
        # Surface the still-incomplete contracts so CI logs catch them.
        for p in incomplete_paths[:10]:
            logger.error(f"  ❌ {p} still has neither layer nor productType set")
        if n_changed > 0 and not args.write:
            logger.error(
                f"❌ {n_changed} contract(s) need migration; "
                "re-run with --write to apply or fix metadata by hand."
            )
        return 1

    return 0
