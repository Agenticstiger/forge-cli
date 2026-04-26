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

"""Pytest entry point for the corpus regression suite (E17).

Discovers every fixture in ``tests/corpus/fixtures/`` and runs each as
its own deterministic replay. This is the golden-suite layer that catches
semantic regressions a narrow unit test will miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import CORPUS_DIR, _discover_fixtures, run_corpus_fixture


def pytest_generate_tests(metafunc):
    """Discover fixtures and parametrize the test."""
    if "fixture_path" in metafunc.fixturenames:
        fixtures = _discover_fixtures()
        metafunc.parametrize(
            "fixture_path",
            fixtures,
            ids=[f.stem for f in fixtures],
        )


def test_corpus_fixture(fixture_path: Path) -> None:
    """Run one corpus fixture through forge and assert its golden constraints."""
    result = run_corpus_fixture(fixture_path)
    assert "fixture" in result
    assert result["smoke_only"] is False
    assert result["constraint_count"] >= 1, (
        f"Fixture {fixture_path.name} has no constraints — every "
        "fixture should pin at least one expected behaviour."
    )


def test_corpus_directory_exists():
    assert CORPUS_DIR.is_dir(), (
        f"Expected corpus directory at {CORPUS_DIR}; v1.6 replay " "tooling requires it to exist."
    )


def test_at_least_one_fixture_present():
    """Pin: the corpus must have at least the smoke template
    so contributors see the expected fixture format."""
    fixtures = _discover_fixtures()
    assert len(fixtures) >= 1, "Expected at least one fixture in tests/corpus/fixtures/."
