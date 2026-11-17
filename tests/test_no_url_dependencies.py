# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""No dependency in pyproject.toml is a direct URL.

PyPI refuses an upload whose ``Requires-Dist`` holds a URL (``name @ git+https://...``), so a
pinned commit in the base list or in any extra blocks a release. A dependency that is not on
PyPI yet is installed from ``requirements/*.txt`` by CI and by hand, never named here.
"""

from __future__ import annotations

import re

import pytest

from tests.ci_guards import extras_alongside_base as g

pytestmark = pytest.mark.unit

_URL = re.compile(r"@\s*\S|://|\b(?:git|hg|svn|bzr)\+|\bfile:", re.IGNORECASE)


def _all_requirements() -> list:
    project = g.load()
    found = [("dependencies", r) for r in project["dependencies"]]
    for extra, reqs in project["optional-dependencies"].items():
        found += [(f"[{extra}]", r) for r in reqs]
    for group, reqs in (g.load_raw().get("dependency-groups") or {}).items():
        found += [(f"group {group}", r) for r in reqs if isinstance(r, str)]
    return found


def _is_url(requirement: str) -> bool:
    return bool(_URL.search(requirement.split(";", 1)[0]))


def test_no_dependency_is_a_direct_url():
    bad = [f"{where}: {req}" for where, req in _all_requirements() if _is_url(req)]
    assert not bad, "PyPI refuses URL dependencies:\n" + "\n".join(bad)


def test_the_scan_sees_the_requirements():
    assert len(_all_requirements()) > 50


@pytest.mark.parametrize(
    "requirement",
    [
        "x @ git+https://github.com/a/b.git@abc",
        "x @ https://example.com/x.whl",
        "x @ file:///tmp/x.whl",
        "https://example.com/x.whl",
        "git+https://github.com/a/b.git",
        "x[extra] @ http://h/x.zip ; python_version >= '3.10'",
    ],
)
def test_the_scan_catches_each_url_shape(requirement):
    assert _is_url(requirement)


@pytest.mark.parametrize("requirement", ["duckdb>=1.5.0", "x[a,b]>=1; python_version < '3.11'"])
def test_the_scan_passes_a_version_requirement(requirement):
    assert not _is_url(requirement)
