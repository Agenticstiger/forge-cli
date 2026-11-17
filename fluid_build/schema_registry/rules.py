# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Confluent data-contract rules (CEL) from a produced subject's invariants.

``rules: true`` on a produced Confluent subject's registry block registers the
expose's error-severity row checks as ``domain_rules`` of the subject's rule set:
``CONDITION`` rules of type ``CEL``, mode ``WRITE``, ``onFailure`` ``ERROR``, which
Confluent's serializers evaluate on every record (they need Confluent Platform
Enterprise 7.4+ with the rule set extension, or Confluent Cloud's Stream Governance
Advanced package). The CEL text comes from
:func:`fluid_build.fluid_sql.render_cel`, which is exact or refuses.

Only a row check becomes a rule: an invariant with ``check`` (and optionally ``when``),
no ``mustBe*`` violation band, and severity ``error``. Every other invariant is listed
as not enforced by the registry (a warning); a row check with no exact CEL form is an
error. Avro subjects only in v1. A consumed subject's rules are its producer's.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .reference import Subject

Rule = Dict[str, Any]


def _band(inv: Mapping[str, Any]) -> Optional[str]:
    from fluid_build.data_rules._lint import OPERATORS

    return next((op for op in OPERATORS if op in inv), None)


def _expose(contract: Mapping[str, Any], subject: Subject) -> Any:
    from fluid_build.data_rules import _model

    exposes = _model.build(contract)
    return next(
        (
            e
            for e in exposes
            if (e.expose_id and e.expose_id == subject.owner) or str(e.index) == subject.owner
        ),
        None,
    )


def confluent_rules(
    contract: Mapping[str, Any], subject: Subject
) -> Tuple[List[Rule], List[str], List[str]]:
    """``(rules, errors, warnings)`` for ``subject`` when its registry sets ``rules: true``.

    Each rule is a ``domain_rules`` entry of ``confluent_schema.ruleset`` (name, doc,
    kind, type, mode, expr, on_failure). Nothing for a subject without ``rules: true``.
    """
    ref = subject.ref
    if ref.kind != "confluent" or ref.options.get("rules") is not True:
        return [], [], []
    where = ref.where
    if subject.role != "produced":
        return (
            [],
            [f"{where}.rules: a consumed subject's rules are its producer's; remove rules"],
            [],
        )
    from .produced import ProducedSubjectError, data_format

    try:
        fmt = data_format(subject)
    except ProducedSubjectError:
        fmt = subject.format
    if fmt != "AVRO":
        return (
            [],
            [
                f"{where}.rules: forge writes Confluent data-contract rules for Avro subjects "
                f"only; this subject is {fmt or 'not Avro'}. Use serialization.value: avro, "
                "or remove rules"
            ],
            [],
        )
    expose = _expose(contract, subject)
    if expose is None:  # pragma: no cover - a produced subject always has its expose
        return [], [], []

    from fluid_build import fluid_sql as fs
    from fluid_build.data_rules._model import as_list

    rules: List[Rule] = []
    errors: List[str] = []
    warnings: List[str] = []
    for inv in as_list(expose.contract.get("invariants")):
        if not isinstance(inv, Mapping) or not isinstance(inv.get("id"), str):
            continue
        inv_id = inv["id"]

        def skip(why: str, inv_id: str = inv_id) -> None:
            warnings.append(f"{where}: invariant '{inv_id}' is not enforced by the registry: {why}")

        if "derive" in inv:
            skip(
                "an aggregate is checked on the data, not per record"
                if "aggregate" in inv
                else "a derive is checked on the data, not per record"
            )
        elif "dependency" in inv:
            skip("a dependency compares rows, which a per-record rule cannot")
        elif "check" not in inv:
            continue  # the schema's oneOf reports it
        elif _band(inv):
            skip(f"it allows violating rows ({_band(inv)})")
        elif inv.get("severity", "error") != "error":
            skip(f"its severity is {inv.get('severity')}; only error checks become rules")
        else:
            try:
                cel = fs.render_cel(fs.parse(inv["check"]), expose.model)
                if isinstance(inv.get("when"), str):
                    when = fs.render_cel(fs.parse(inv["when"]), expose.model)
                    cel = (
                        f"(!({when}) || {cel})"
                        if when not in ("true", "false")
                        else (cel if when == "true" else "true")
                    )
            except fs.CelUntranslatable as exc:
                errors.append(
                    f"{where}: invariant '{inv_id}' has no exact CEL form for a Confluent rule: "
                    f"{exc.reason}"
                )
                continue
            except fs.FluidSqlError as exc:
                errors.append(
                    f"{where}: invariant '{inv_id}' has no exact CEL form for a Confluent rule: "
                    f"it is not valid FLUID SQL/1 ({exc.detail})"
                )
                continue
            rule: Rule = {"name": inv_id}
            if inv.get("description"):
                rule["doc"] = str(inv["description"])
            rule.update({"kind": "CONDITION", "type": "CEL", "mode": "WRITE", "expr": cel})
            rule["on_failure"] = "ERROR"
            rules.append(rule)
    if not rules:
        warnings.append(
            f"{where}.rules is true, and no invariant of this expose becomes a Confluent rule"
        )
    return rules, errors, warnings
