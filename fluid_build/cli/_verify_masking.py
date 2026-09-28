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

"""``fluid verify``'s masking dimension: did the masked columns land treated?

For every ``exposes[].policy.privacy.masking[]`` rule, every non-null value of
the column in the landed data must have the shape its strategy produces
(``build_runners/_masking.py``, ``MaskingRule.shape``): 64 lowercase hex
characters for ``hash``, 32 for ``tokenize``, ``aesgcm:v1:`` and base64url for
``encrypt``, the kept characters around ``*`` for ``mask``. A value that does
not is counted, and one is enough to fail the dimension, CRITICAL, so
``--strict`` fails a pipeline that landed cleartext whatever wrote it: the
DuckDB runner, a dbt model, or a hand-copied file.

It is a shape check, not proof: a cleartext value that happens to be 64 hex
characters passes as a hash. It never reads a value back into the report: only
counts leave the query, because the values that fail are the cleartext.

A rule naming a column the data does not have fails (the build refuses that
rule, so the data came from somewhere else), and so does ``k_anonymity``,
which the build refuses and which has no per-value shape to check.

Local files are checked with DuckDB's ``regexp_full_match`` (RE2); S3+Glue
tables with Athena's ``regexp_like`` anchored with ``\\A(?:...)\\z``, in the
same query that counts the rows (``_verify_athena._count_rows``). Trino's
``regexp_like`` finds rather than matches ("the pattern only needs to be
contained within string"), and in the Java pattern syntax it documents ``$``
also matches before a final line terminator, so ``^...$`` would pass
``"<64 hex>\\n"``; ``\\A`` and ``\\z`` anchor the whole string in Java, Joni
and RE2J alike.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import quote_ansi_string_literal

#: Statuses, in each verifier's own vocabulary.
LOCAL_OK, LOCAL_BAD = "match", "mismatch"
ATHENA_OK, ATHENA_BAD = "pass", "fail"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def declared_rules(expose: Mapping[str, Any]) -> Tuple[List[Any], Optional[str]]:
    """``(rules, problem)``: the expose's masking rules, or why they cannot be read."""
    from fluid_build.build_runners._masking import MaskingPolicyError, masking_rules

    try:
        return masking_rules(expose, allow_unsupported=True), None
    except MaskingPolicyError as exc:
        return [], str(exc)


def _entry(rule: Any, **fields: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "column": rule.column,
        "strategy": rule.strategy,
        "expected_shape": rule.shape_description,
    }
    entry.update(fields)
    return entry


def _unchecked(rule: Any, present: bool, where: str) -> Optional[Dict[str, Any]]:
    """The entry for a rule that cannot be queried; ``None`` when it can."""
    if not rule.appliable:
        return _entry(
            rule,
            status="fail",
            offending=None,
            non_null=None,
            message=(
                f"{rule.strategy} is not applied at landing (the build refuses it) and has no "
                "per-value shape to check"
            ),
        )
    if not present:
        return _entry(
            rule,
            status="fail",
            offending=None,
            non_null=None,
            message=f"column {rule.column} is not in {where}, so the rule cannot be checked",
        )
    return None


def dimension(
    entries: Sequence[Dict[str, Any]], *, ok: str, bad: str, problem: Optional[str] = None
) -> Dict[str, Any]:
    """The dimension from per-column entries (or from a rule set that would not parse)."""
    if problem:
        return {"status": bad, "columns": [], "message": f"masking rules unreadable: {problem}"}
    failed = [e for e in entries if e["status"] == "fail"]
    if not failed:
        checked = ", ".join(f"{e['column']} ({e['strategy']})" for e in entries)
        return {
            "status": ok,
            "columns": list(entries),
            "message": f"every non-null value has its strategy's shape: {checked}",
        }
    return {
        "status": bad,
        "columns": list(entries),
        "message": "; ".join(f"{e['column']}: {e['message']}" for e in failed),
    }


def _counted(rule: Any, non_null: int, offending: int) -> Dict[str, Any]:
    if offending:
        return _entry(
            rule,
            status="fail",
            offending=offending,
            non_null=non_null,
            message=(
                f"{offending:,} of {non_null:,} non-null value(s) are not "
                f"{rule.shape_description}; the column holds untreated values"
            ),
        )
    return _entry(
        rule,
        status="pass",
        offending=0,
        non_null=non_null,
        message=f"{non_null:,} non-null value(s), all {rule.shape_description}",
    )


#: ``(finish)``: the query's values, in order, to the dimension (``None``: no rules).
Finish = Callable[[Sequence[int]], Optional[Dict[str, Any]]]


def _plan(
    expose: Mapping[str, Any],
    columns: Sequence[str],
    *,
    where: str,
    untreated: Callable[[str, Any], str],
    ok: str,
    bad: str,
) -> Tuple[List[str], Finish]:
    """``(select expressions, finish)``: two per queryable rule, the column's
    non-null count and ``untreated(quoted column, rule)``, the count of its
    values without the rule's shape. Rules that cannot be queried get their
    entry without a query."""
    rules, problem = declared_rules(expose)
    present = {c.lower(): c for c in columns}
    entries: List[Optional[Dict[str, Any]]] = []
    queried: List[Tuple[int, Any]] = []
    selects: List[str] = []
    for rule in rules:
        actual = present.get(rule.column.lower())
        entry = _unchecked(rule, actual is not None, where)
        entries.append(entry)
        if entry is None and actual is not None:
            ident = _quote(actual)
            selects += [f"count({ident})", untreated(ident, rule)]
            queried.append((len(entries) - 1, rule))

    def finish(values: Sequence[int]) -> Optional[Dict[str, Any]]:
        if problem:
            return dimension([], ok=ok, bad=bad, problem=problem)
        if not rules:
            return None
        for n, (index, rule) in enumerate(queried):
            entries[index] = _counted(rule, int(values[2 * n]), int(values[2 * n + 1]))
        return dimension([e for e in entries if e is not None], ok=ok, bad=bad)

    return selects, finish


# ── Local files (DuckDB) ────────────────────────────────────────────────


def _duckdb_untreated(ident: str, rule: Any) -> str:
    pattern = quote_ansi_string_literal(rule.shape)
    return (
        f"count(*) FILTER (WHERE {ident} IS NOT NULL AND NOT "
        f"regexp_full_match(CAST({ident} AS VARCHAR), {pattern}))"
    )


def local_dimension(
    con: Any, relation_sql: str, columns: Sequence[str], expose: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """The masking dimension for a file DuckDB reads as ``relation_sql``.

    ``None`` when the expose declares no masking rule.
    """
    selects, finish = _plan(
        expose, columns, where="the file", untreated=_duckdb_untreated, ok=LOCAL_OK, bad=LOCAL_BAD
    )
    values: List[int] = []
    if selects:
        row = con.execute(f"SELECT {', '.join(selects)} FROM ({relation_sql})").fetchone()
        values = [int(v or 0) for v in row]
    return finish(values)


# ── Athena ──────────────────────────────────────────────────────────────


def athena_regex(shape: str) -> str:
    """``shape`` anchored for Athena's ``regexp_like``, which finds rather than matches."""
    return f"\\A(?:{shape})\\z"


def _athena_untreated(ident: str, rule: Any) -> str:
    pattern = quote_ansi_string_literal(athena_regex(rule.shape))
    return (
        f"count_if({ident} IS NOT NULL AND NOT "
        f"regexp_like(CAST({ident} AS varchar), {pattern}))"
    )


def athena_plan(
    expose: Mapping[str, Any], table_columns: Sequence[str]
) -> Tuple[List[str], Finish]:
    """``(select expressions, finish)`` for the count query.

    The expressions go after ``COUNT(*)``; ``finish(values)`` turns the values
    they return (in order) into the dimension, or ``None`` when the expose
    declares no masking rule. Column names come from the Glue table.
    """
    return _plan(
        expose,
        table_columns,
        where="the Glue table",
        untreated=_athena_untreated,
        ok=ATHENA_OK,
        bad=ATHENA_BAD,
    )


def severity_problem(masking: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    """``(reason, action)`` when the dimension failed, for the CRITICAL grading."""
    if not masking or masking.get("status") in (LOCAL_OK, ATHENA_OK):
        return None
    return (
        f"masked column(s) did not land treated: {masking.get('message')}",
        "Rebuild with the masking rules applied (the DuckDB runner applies them at "
        "landing), then delete or overwrite the data that landed untreated",
    )
