# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The test cases one ``fluid test`` run reports, in one place.

``--output junit`` and ``--results-table`` both report a run as the same
cases: one per executed data-quality rule, then one per structural check
category. Reading both from here keeps Jenkins' test list and the results
table from ever disagreeing about what ran.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: The structural check categories, in report order. ``quality`` stands for
#: the data-quality rules only when no rule ran on its own (then there is no
#: per-rule case to carry them).
STRUCTURAL_CATEGORIES: Tuple[str, ...] = (
    "schema",
    "connection",
    "binding",
    "missing_resource",
    "missing_field",
    "type_mismatch",
    "mode_mismatch",
    "extra_field",
    "empty_table",
    "row_count_below_threshold",
    "quality",
    "metadata",
    "drift",
)


#: The path of an issue raised for one rule: ``exposes[0].contract.dq.rules.<id>``.
_RULE_PATH = re.compile(r"\.rules\.[^.]+$")


def rule_checks(report) -> List:
    """The data-quality rules that ran, one ``CheckOutcome`` each."""
    return [c for c in getattr(report, "checks", []) if c.category == "quality"]


def structural_categories(report) -> Tuple[List[str], List[str], Dict[str, List]]:
    """``(built-in, extra, issues by category)`` for the structural cases.

    ``built-in`` is :data:`STRUCTURAL_CATEGORIES` (without ``quality`` when a
    rule ran on its own); ``extra`` is every other category the run raised an
    issue in, in first-seen order. Each case passes unless it has an
    error-severity issue.
    """
    by_category: Dict[str, List] = {}
    for issue in report.issues:
        by_category.setdefault(issue.category, []).append(issue)
    builtin = list(STRUCTURAL_CATEGORIES)
    if rule_checks(report):
        # The per-rule cases already carry every rule outcome; a roll-up
        # ``quality`` case on top would double-count them. A quality issue no
        # rule case carries (an expose whose rules could not run at all) still
        # needs a case, so the roll-up stays, with only those issues.
        unattributed = [
            i for i in by_category.get("quality", []) if not _RULE_PATH.search(i.path or "")
        ]
        if unattributed:
            by_category["quality"] = unattributed
        else:
            builtin.remove("quality")
            by_category.pop("quality", None)
    extra = [c for c in by_category if c not in builtin]
    return builtin, extra, by_category


__all__ = ["STRUCTURAL_CATEGORIES", "rule_checks", "structural_categories"]
