# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The CI variables a release reads its source commit from, cleared so a ship test sees the
same environment on a laptop and in a CI runner (which sets GITHUB_RUN_ID, GITHUB_SHA, ...)."""

from __future__ import annotations

import pytest

from fluid_build.cli import _ship_release


def ci_source_names() -> list:
    """Every variable ``_ship_release.source_revision`` reads."""
    names = set()
    for markers, *rest in _ship_release._SOURCE_VARS:
        names.update(markers)
        names.update(rest)
    return sorted(names)


def clear_ci_source(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ci_source_names():
        monkeypatch.delenv(name, raising=False)
