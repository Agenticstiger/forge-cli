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

"""Render a contract's data-quality rules as a SodaCL document.

SodaCL is the YAML DSL Soda Core consumes. The grammar is a flat list of
``checks for <table>:`` blocks. We emit one block per ``exposes[i]``.

Where the rules come from
-------------------------
The **canonical** source is ``exposes[].contract.dq.rules[]`` (``$defs.dqSpec``
/ ``$defs.dqRule`` in every 0.7.x schema) — the same list the native quality
engine executes (``providers/quality_engine.execute_quality_checks``). Reading
anywhere else is how this exporter came to be dead code: it used to read
``exposes[].quality.tests[]``, and ``$defs.expose`` is ``additionalProperties:
false`` in 0.7.1–0.7.6 without a ``quality`` key, so *no schema-valid contract
could ever produce a single check*. ``fluid test --engine soda`` therefore
always printed "nothing to check" and exited 0 — a silent pass on the same
surface a loud crash used to occupy.

``exposes[].quality.tests[]`` is still read for backwards compatibility with
hand-written (schema-invalid) files, but it is no longer the only source.

Honesty contract
----------------
Every declared rule ends up in exactly one of two buckets: ``mapped`` (a
SodaCL check was emitted for it) or ``unmapped`` (with a machine- and
human-readable reason). Nothing is dropped on the floor. Callers are
expected to treat a non-empty ``unmapped`` list as a failure — a quality gate
nobody executed must never read as green.

Prior art
---------
The check shapes follow **datacontract-cli**'s SodaCL exporter
(https://github.com/datacontract/datacontract-cli —
``datacontract/export/sodacl_exporter.py`` + ``sodacl_check_builder``):
required → ``missing_count(f) = 0``, unique → ``duplicate_count(f) = 0``,
enum → ``invalid_count(f) = 0`` + ``valid values``, min/max →
``valid min`` / ``valid max``, freshness → ``freshness(f) < 24h``.

We deliberately **diverge from them on unmapped rules**: datacontract-cli
silently skips any check it cannot build. That is precisely the failure mode
this module exists to avoid, so unmappable rules are surfaced instead.

SodaCL grammar references (verified against soda-core 3.5.6's ANTLR lexer,
which admits only ``d`` / ``h`` / ``m`` freshness units):
    https://docs.soda.io/soda-v3/sodacl-reference/numeric-metrics.md
    https://docs.soda.io/soda-v3/sodacl-reference/validity-metrics.md
    https://docs.soda.io/soda-v3/sodacl-reference/missing-metrics.md
    https://docs.soda.io/soda-v3/sodacl-reference/freshness.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Same identifier rule the native engine enforces before building SQL
# (``providers/quality_engine._SAFE_IDENT``). SodaCL check expressions are
# parsed by an ANTLR grammar, so an exotic identifier would surface as a
# confusing parse error deep inside soda instead of an actionable message.
_SAFE_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``$defs.dqRule.type`` values the native engine executes but that have no
# faithful SodaCL equivalent, plus the two the native engine cannot run
# either. Listed explicitly so a new schema rule type shows up as an
# unmapped rule rather than vanishing.
_NO_SODACL_EQUIVALENT = {
    "schema": (
        "SodaCL's schema check compares against an explicit column list, "
        "which a dqRule does not carry"
    ),
    "drift_detection": (
        "SodaCL drift/anomaly checks require Soda Cloud scan history, which "
        "the shell-out runner does not have"
    ),
}


@dataclass(frozen=True)
class UnmappedRule:
    """A declared DQ rule for which no SodaCL check could be emitted."""

    expose: str
    rule_id: str
    rule_type: str
    reason: str

    def describe(self) -> str:
        return f"{self.expose}.{self.rule_id} (type={self.rule_type or '?'}): {self.reason}"


@dataclass
class SodaclRendering:
    """Result of rendering a contract to SodaCL.

    Attributes
    ----------
    text:
        The SodaCL YAML document.
    mapped:
        ``"<expose>.<rule id>"`` for every rule that produced a check.
    unmapped:
        Every declared rule that produced no check, with the reason.
    legacy_checks:
        Checks emitted from the pre-schema ``exposes[].quality.tests[]`` block.
        Those entries carry no rule ids, so they cannot appear in ``mapped``
        — but they are real checks in ``text`` and a caller that ignored them
        would exit 0 on a contract whose scan it never ran.
    """

    text: str
    mapped: list[str] = field(default_factory=list)
    unmapped: list[UnmappedRule] = field(default_factory=list)
    legacy_checks: int = 0

    @property
    def declared(self) -> int:
        """Total quality rules found in the contract, mapped or not."""
        return len(self.mapped) + len(self.unmapped) + self.legacy_checks

    @property
    def emitted_checks(self) -> int:
        """How many SodaCL checks ``text`` carries."""
        return len(self.mapped) + self.legacy_checks

    @property
    def has_checks(self) -> bool:
        """True when ``text`` actually contains at least one SodaCL check."""
        return self.emitted_checks > 0


def render_sodacl(contract: Mapping[str, Any]) -> str:
    """Render a parsed fluid contract as a SodaCL YAML document.

    Thin wrapper over :func:`render_sodacl_document` for callers that only
    want the YAML. **Prefer the structured call** — the string alone cannot
    tell "no rules were declared" from "rules were declared and every one of
    them was dropped", and conflating those two is what made the Soda engine
    exit 0 without checking anything.
    """
    return render_sodacl_document(contract).text


def render_sodacl_document(contract: Mapping[str, Any]) -> SodaclRendering:
    """Render a parsed fluid contract to SodaCL, accounting for every rule.

    Parameters
    ----------
    contract:
        Parsed fluid contract.

    Returns
    -------
    SodaclRendering
        YAML text plus the mapped / unmapped rule accounting.
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required to render SodaCL") from e

    if not isinstance(contract, Mapping):
        raise TypeError(f"contract must be a Mapping, got {type(contract).__name__}")

    doc: dict[str, Any] = {}
    mapped: list[str] = []
    unmapped: list[UnmappedRule] = []
    legacy_checks = 0

    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        table_name = _soda_table_name(expose)
        expose_id = _expose_label(expose)

        checks: list[Any] = []
        checks.extend(_dq_rules_to_checks(expose, expose_id, mapped, unmapped))
        legacy = _legacy_quality_block_to_checks(expose)
        checks.extend(legacy)

        # SQL-expression rules (``$defs.exposeContract.quality``). Neither
        # engine executes these today; counting them as declared-but-unmapped
        # keeps a gate the author wrote from reading as "nothing to check".
        unmapped.extend(_expression_rules_unmapped(expose, expose_id))

        if not checks:
            continue
        if not table_name:
            # Emitting a ``checks for <blank>`` block would produce a document
            # soda cannot parse, so nothing is emitted — which means these
            # rules were not executed and must be reported as such.
            for rid in _dq_rule_ids(expose):
                unmapped.append(
                    UnmappedRule(
                        expose=expose_id,
                        rule_id=rid,
                        rule_type="",
                        reason=(
                            "cannot resolve a table name for this expose — set "
                            "binding.location.table"
                        ),
                    )
                )
                if f"{expose_id}.{rid}" in mapped:
                    mapped.remove(f"{expose_id}.{rid}")
            for idx in range(len(legacy)):
                unmapped.append(
                    UnmappedRule(
                        expose=expose_id,
                        rule_id=f"quality.tests[{idx}]",
                        rule_type="legacy",
                        reason=(
                            "cannot resolve a table name for this expose — set "
                            "binding.location.table"
                        ),
                    )
                )
            continue
        legacy_checks += len(legacy)
        # SodaCL key format: "checks for <table_name>"
        doc.setdefault(f"checks for {table_name}", []).extend(checks)

    if not doc:
        text = "# No quality tests defined in contract\n"
    else:
        text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return SodaclRendering(text=text, mapped=mapped, unmapped=unmapped, legacy_checks=legacy_checks)


# ----------------------------------------------------------------------
# Canonical path — exposes[].contract.dq.rules[]
# ----------------------------------------------------------------------


def _dq_spec(expose: Mapping[str, Any]) -> Mapping[str, Any]:
    """Locate the expose's dq block.

    Mirrors ``ContractValidator._validate_expose`` exactly: ``dq`` directly on
    the expose first, then the schema-valid ``contract.dq``. Diverging here
    would let the two engines disagree about which rules exist.
    """
    dq = expose.get("dq")
    if isinstance(dq, Mapping) and dq.get("rules"):
        return dq
    contract_block = expose.get("contract")
    if isinstance(contract_block, Mapping):
        nested = contract_block.get("dq")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _dq_rules(expose: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rules = _dq_spec(expose).get("rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    return [r for r in rules if isinstance(r, Mapping)]


def _dq_rule_ids(expose: Mapping[str, Any]) -> list[str]:
    return [str(r.get("id") or "unnamed") for r in _dq_rules(expose)]


def _expose_label(expose: Mapping[str, Any]) -> str:
    for key in ("exposeId", "id"):
        value = expose.get(key)
        if isinstance(value, str) and value:
            return value
    return "<expose>"


def _dq_rules_to_checks(
    expose: Mapping[str, Any],
    expose_id: str,
    mapped: list[str],
    unmapped: list[UnmappedRule],
) -> list[Any]:
    """Convert ``contract.dq.rules[]`` to SodaCL checks, accounting for each."""
    checks: list[Any] = []
    for rule in _dq_rules(expose):
        rule_id = str(rule.get("id") or "unnamed")
        rule_type = str(rule.get("type") or "").strip().lower()
        emitted, reason = _convert_dq_rule(rule, rule_type)
        if emitted:
            checks.extend(emitted)
            mapped.append(f"{expose_id}.{rule_id}")
        else:
            unmapped.append(
                UnmappedRule(
                    expose=expose_id,
                    rule_id=rule_id,
                    rule_type=rule_type,
                    reason=reason or "no SodaCL equivalent",
                )
            )
    return checks


def _convert_dq_rule(rule: Mapping[str, Any], rule_type: str) -> tuple[list[Any], str]:
    """Map one ``$defs.dqRule`` to SodaCL checks, or explain why we can't.

    Returns ``(checks, reason)``. ``checks`` empty ⇒ unmapped, and ``reason``
    says why in terms the contract author can act on.
    """
    selector = rule.get("selector")
    selector = selector.strip() if isinstance(selector, str) else ""
    threshold = rule.get("threshold")
    operator = rule.get("operator") or ">="
    if not isinstance(operator, str):
        return [], f"'operator' must be a string, got {type(operator).__name__}"
    operator = operator.strip()

    if rule_type in _NO_SODACL_EQUIVALENT:
        return [], _NO_SODACL_EQUIVALENT[rule_type]

    if rule_type == "anomaly_detection" and selector in ("", "*"):
        # Row-count rule. Native: COUNT(*) <operator> threshold, or > 0 when
        # no threshold is declared.
        if threshold is None:
            return ["row_count > 0"], ""
        if operator not in _SODACL_OPERATORS:
            return [], f"operator {operator!r} has no SodaCL equivalent"
        return [f"row_count {_SODACL_OPERATORS[operator]} {_num(threshold)}"], ""

    if not selector:
        return [], "rule declares no 'selector' (column name)"
    if not _SAFE_COLUMN.match(selector):
        # SodaCL check expressions are parsed by an ANTLR grammar; an
        # exotic identifier would produce a confusing parse error deep
        # inside soda rather than an actionable message here.
        return [], f"selector {selector!r} is not a plain SQL identifier"

    if rule_type == "completeness":
        return _completeness_checks(selector, threshold, operator)
    if rule_type == "uniqueness":
        return _uniqueness_checks(selector, threshold, operator)
    if rule_type in ("accuracy", "anomaly_detection"):
        # Column-scoped anomaly_detection delegates to the same bound logic
        # the native engine uses (``_check_anomaly_detection`` → ``_check_accuracy``).
        return _bound_checks(selector, threshold, operator)
    if rule_type in ("validity", "valid_values"):
        return _valid_values_checks(selector, rule)
    if rule_type == "freshness":
        return _freshness_checks(selector, rule)

    return [], (
        "unknown rule type — $defs.dqRule.type accepts: freshness, "
        "completeness, uniqueness, valid_values, accuracy, schema, "
        "anomaly_detection, drift_detection"
    )


# SodaCL threshold comparators (soda-core 3.x supports =, !=, <, >, <=, >=).
_SODACL_OPERATORS = {
    ">=": ">=",
    ">": ">",
    "<=": "<=",
    "<": "<",
    "==": "=",
    "=": "=",
    "!=": "!=",
}

# Operators for which "ratio at least/exactly 1.0" is the author's intent.
_AT_LEAST_OPERATORS = frozenset({">=", "==", "="})


def _completeness_checks(selector: str, threshold, operator: str) -> tuple[list[Any], str]:
    """completeness ⇒ SodaCL missing metrics.

    Native computes ``count(non-null) / count(*)``; SodaCL's
    ``missing_percent`` is ``missing_count / row_count * 100``, so
    ``completeness = 1 - missing_percent/100`` exactly. The comparison is
    therefore mirrored (a lower bound on completeness is an upper bound on
    missing).
    """
    if threshold is None:
        # Native's no-threshold behaviour is ``ratio == 1.0``.
        return [f"missing_count({selector}) = 0"], ""
    try:
        t = float(threshold)
    except (TypeError, ValueError):
        return [], f"'threshold' must be a number, got {threshold!r}"
    if t == 1.0 and operator in _AT_LEAST_OPERATORS:
        # The overwhelmingly common case, and the form datacontract-cli emits
        # for `required: true`.
        return [f"missing_count({selector}) = 0"], ""
    mirrored = _MIRRORED_OPERATORS.get(operator)
    if mirrored is None:
        return [], f"operator {operator!r} has no SodaCL equivalent"
    return [f"missing_percent({selector}) {mirrored} {_num((1.0 - t) * 100)}"], ""


# ``ratio <op> t`` ⟺ ``missing <mirrored-op> (1 - t)``.
_MIRRORED_OPERATORS = {
    ">=": "<=",
    ">": "<",
    "<=": ">=",
    "<": ">",
    "==": "=",
    "=": "=",
    "!=": "!=",
}


def _uniqueness_checks(selector: str, threshold, operator: str) -> tuple[list[Any], str]:
    """uniqueness ⇒ ``duplicate_count(col) = 0``, and only that.

    Soda's ``duplicate_count`` filters ``IS NOT NULL`` (see
    ``soda/execution/query/duplicates_query.py``), so it is zero exactly when
    the native engine's ``COUNT(DISTINCT col) / COUNT(col)`` is 1.0. For any
    *partial* uniqueness threshold the two metrics are not the same quantity
    — ``duplicate_percent`` is over ``row_count``, not over distinct values —
    so we refuse rather than emit a check that silently means something else.
    """
    if threshold is None or (_is_one(threshold) and operator in _AT_LEAST_OPERATORS):
        return [f"duplicate_count({selector}) = 0"], ""
    return [], (
        "SodaCL has no metric equal to the native engine's "
        "distinct/non-null ratio for a partial uniqueness threshold; only "
        "full uniqueness (threshold 1.0 with >=, == or =) is expressible"
    )


def _bound_checks(selector: str, threshold, operator: str) -> tuple[list[Any], str]:
    """accuracy / column anomaly_detection ⇒ numeric bound checks.

    A bound rule asserts something about *every* row, so the aggregate that
    decides it depends on the direction — the same reasoning as the native
    engine's ``_check_accuracy``.
    """
    if threshold is None:
        return [], (
            "bound rule declares no 'threshold', so there is nothing to "
            "assert (the native engine treats this as a no-op pass)"
        )
    if operator in (">=", ">"):
        return [f"min({selector}) {operator} {_num(threshold)}"], ""
    if operator in ("<=", "<"):
        return [f"max({selector}) {operator} {_num(threshold)}"], ""
    if operator in ("==", "="):
        # Every non-null value must equal the threshold. Soda's valid
        # min/max bracket ignores nulls, matching the native engine's
        # ``col IS NOT NULL AND col <> t`` violation count.
        return (
            [
                {
                    f"invalid_count({selector}) = 0": {
                        "valid min": _num_value(threshold),
                        "valid max": _num_value(threshold),
                    }
                }
            ],
            "",
        )
    return [], (
        f"operator {operator!r} cannot be expressed as a SodaCL bound "
        "(no metric asserts 'every value differs from N')"
    )


def _valid_values_checks(selector: str, rule: Mapping[str, Any]) -> tuple[list[Any], str]:
    """validity / valid_values ⇒ ``invalid_count(col) = 0`` + ``valid values``."""
    from fluid_build.providers.quality_engine import extract_valid_values

    values = extract_valid_values(rule)
    if not values:
        return [], (
            "valid_values rule declares no allowed values — declare them in "
            'the description ("COLUMN valid values: A, B, C.") or as a '
            "'validValues' list"
        )
    return [{f"invalid_count({selector}) = 0": {"valid values": list(values)}}], ""


def _freshness_checks(selector: str, rule: Mapping[str, Any]) -> tuple[list[Any], str]:
    """freshness ⇒ ``freshness(col) < <threshold>``.

    The native engine accepts ISO-8601 (``PT6H``) and shorthand (``6h``);
    SodaCL's grammar admits only ``d`` / ``h`` / ``m`` (verified in
    soda-core 3.5.6's ANTLR lexer literals), so the window is normalised
    through the shared parser and re-formatted.
    """
    from fluid_build.providers.quality_engine import parse_duration_seconds

    window = rule.get("window", rule.get("freshness"))
    if window is None or (isinstance(window, str) and not window.strip()):
        return [], (
            "freshness rule declares no 'window', and a SodaCL freshness "
            "check requires a threshold"
        )
    seconds = parse_duration_seconds(str(window))
    if seconds is None:
        return [], (
            f"unparseable freshness window {window!r} — use ISO-8601 "
            "('PT6H', 'P2D') or shorthand ('6h', '2d')"
        )
    formatted = _sodacl_duration(seconds)
    if formatted is None:
        return [], (
            f"freshness window {window!r} is {seconds}s, which SodaCL cannot "
            "express — its smallest unit is a minute"
        )
    return [f"freshness({selector}) < {formatted}"], ""


def _sodacl_duration(seconds: int) -> str | None:
    """Format whole seconds as a SodaCL duration (``1d6h``, ``30m``).

    Returns ``None`` for a sub-minute remainder — SodaCL's lexer has no
    seconds unit, so there is no honest rendering.
    """
    if seconds <= 0 or seconds % 60:
        return None
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    out = ""
    if days:
        out += f"{days}d"
    if hours:
        out += f"{hours}h"
    if minutes:
        out += f"{minutes}m"
    return out or None


def _expression_rules_unmapped(expose: Mapping[str, Any], expose_id: str) -> list[UnmappedRule]:
    """Account for ``exposes[].contract.quality[]`` SQL-expression rules.

    ``$defs.exposeContract.quality`` is a list of ``{rule, expression,
    severity}``. No fluid engine executes it — not the native one either —
    so it is reported as declared-but-unrun rather than ignored.
    """
    contract_block = expose.get("contract")
    if not isinstance(contract_block, Mapping):
        return []
    entries = contract_block.get("quality")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    out: list[UnmappedRule] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        rule_id = entry.get("rule")
        rule_id = rule_id if isinstance(rule_id, str) and rule_id else f"quality[{idx}]"
        out.append(
            UnmappedRule(
                expose=expose_id,
                rule_id=rule_id,
                rule_type="expression",
                reason=(
                    "SQL-expression rules under exposes[].contract.quality[] "
                    "are not executed by any fluid quality engine; express "
                    "the assertion as an exposes[].contract.dq.rules[] entry"
                ),
            )
        )
    return out


# ----------------------------------------------------------------------
# Legacy path — exposes[].quality.tests[]
# ----------------------------------------------------------------------


def _legacy_quality_block_to_checks(expose: Mapping[str, Any]) -> list[Any]:
    """Convert the pre-0.7 ``expose.quality`` block, if one is present.

    ``$defs.expose`` has no ``quality`` key and is ``additionalProperties:
    false``, so this never fires for a contract that passes ``fluid
    validate``. Retained so hand-written files that predate the schema keep
    working; it is not counted in the mapped/unmapped accounting because
    those files have no rule ids to account for.
    """
    quality = expose.get("quality")
    if not isinstance(quality, Mapping):
        return []
    tests = quality.get("tests") or []
    if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes)):
        return []

    checks: list[Any] = []
    for t in tests:
        if not isinstance(t, Mapping):
            continue
        # ``_convert_test`` always returns a list — 0, 1, or more SodaCL
        # entries — so the caller's job is just to ``extend``. This lets a
        # single fluid test (e.g. ``range`` with min + max) expand to
        # multiple SodaCL checks without nesting lists inside lists.
        checks.extend(_convert_test(t))

    # Also pull SLA freshness into a Soda freshness check if present.
    # SodaCL freshness is a plain string check, not a dict.
    sla = quality.get("sla")
    if isinstance(sla, Mapping):
        freshness = sla.get("freshness")
        if isinstance(freshness, str) and freshness.strip():
            checks.append(f"freshness using <last_updated> < {freshness}")

    return checks


def _soda_table_name(expose: Mapping[str, Any]) -> str:
    """Best-effort table name for Soda's ``checks for <table>`` key.

    ``binding.location.table`` is the schema-valid location
    (``$defs.bindingLocation``, ``additionalProperties: false``);
    ``location.properties`` is the legacy shape the Snowflake validation
    provider still falls back to, so we mirror that order.
    """
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        location = binding.get("location")
        if isinstance(location, Mapping):
            table = location.get("table")
            if isinstance(table, str) and table:
                return table
            props = location.get("properties")
            if isinstance(props, Mapping):
                table = props.get("table") or props.get("name")
                if isinstance(table, str) and table:
                    return table
    eid = expose.get("id") or expose.get("exposeId")
    return eid if isinstance(eid, str) else ""


def _convert_test(test: Mapping[str, Any]) -> list[Any]:
    """Map one legacy ``quality.tests[]`` entry to zero-or-more SodaCL entries.

    SodaCL admits two check shapes:
      * a bare string (``missing_count(x) = 0``)
      * a dict ``{<check name>: {<config-key>: <config-value>, ...}}``

    Returning a list lets a single fluid test (e.g. ``range`` with min and
    max) emit two SodaCL checks without nesting lists. Empty list = skip.
    """
    kind = test.get("type") or ""
    if not isinstance(kind, str):
        return []
    kind = kind.strip().lower()
    col = test.get("column") if isinstance(test.get("column"), str) else None

    if kind == "not_null" and col:
        return [f"missing_count({col}) = 0"]
    if kind == "unique" and col:
        return [f"duplicate_count({col}) = 0"]
    if kind == "accepted_values" and col:
        values = test.get("values")
        if isinstance(values, Sequence):
            return [
                {
                    f"invalid_count({col}) = 0": {
                        "valid values": [v for v in values],
                    }
                }
            ]
        return []
    if kind == "range" and col:
        lo = test.get("min")
        hi = test.get("max")
        out: list[Any] = []
        if lo is not None:
            out.append(f"min({col}) >= {lo}")
        if hi is not None:
            out.append(f"max({col}) <= {hi}")
        return out
    if kind == "regex" and col:
        pattern = test.get("pattern")
        if isinstance(pattern, str):
            return [
                {
                    f"invalid_count({col}) = 0": {
                        "valid regex": pattern,
                    }
                }
            ]
        return []
    if kind == "row_count_anomaly":
        return ["anomaly score for row_count < default"]
    if kind == "freshness":
        threshold = test.get("threshold") or test.get("max_age")
        if isinstance(threshold, str):
            ts_col = test.get("column") or "last_updated"
            return [f"freshness using <{ts_col}> < {threshold}"]
        return []
    if kind == "relationships" and col:
        to_model = test.get("to") or test.get("relation")
        field_name = test.get("field") or "id"
        if isinstance(to_model, str):
            return [f"values in ({col}) must exist in {to_model} ({field_name})"]
        return []

    # Unknown — emit a comment line so operators see the intent without
    # the YAML parser tripping over a structured-but-unmapped entry.
    return [f"# unmapped fluid test: type={kind} test={dict(test)!r}"]


# ----------------------------------------------------------------------
# Number formatting
# ----------------------------------------------------------------------


def _is_one(value: Any) -> bool:
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def _num_value(value: Any) -> Any:
    """Coerce a schema ``number`` to the tidiest JSON/YAML scalar."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    # Binary float noise (``(1 - 0.9) * 100 == 10.000000000000002``) would
    # otherwise land in the emitted SodaCL.
    f = round(f, 10)
    return int(f) if f.is_integer() else f


def _num(value: Any) -> str:
    """Render a schema ``number`` as a SodaCL threshold literal."""
    coerced = _num_value(value)
    return repr(coerced) if isinstance(coerced, float) else str(coerced)
