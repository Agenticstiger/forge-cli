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

"""An ``--env`` that names no overlay is said out loud, not logged at DEBUG.

``load_with_overlay(contract, "awz")`` (a typo for ``aws``) used to return
the base contract with a DEBUG line only, so every stage silently planned,
applied and verified the base contract at the default log level. ``dev``
with no overlay is the base by convention and stays accepted, at INFO.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluid_build import loader

_BASE = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.overlay
name: Overlay Demo
exposes:
  - exposeId: rows
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/rows.parquet
"""

_AWS = """\
exposes:
  - binding:
      platform: aws
      location:
        bucket: lake
        path: bronze/rows/
"""


@pytest.fixture(autouse=True)
def _fresh_once_cache():
    # The notice is emitted once per (contract, env) per process.
    noted = getattr(loader, "_NOTED_MISSING_OVERLAYS", None)
    if noted is not None:
        noted.clear()
    yield
    if noted is not None:
        noted.clear()


@pytest.fixture
def contract(tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_BASE, encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "aws.yaml").write_text(_AWS, encoding="utf-8")
    (tmp_path / "overlays" / "gcp.yaml").write_text(_AWS.replace("aws", "gcp"), encoding="utf-8")
    return path


def _records(caplog: pytest.LogCaptureFixture, level: int):
    return [r for r in caplog.records if r.levelno == level and "overlay" in r.getMessage()]


def test_missing_overlay_is_a_warning_naming_env_and_existing_overlays(
    contract: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    merged = loader.load_with_overlay(contract, "awz")
    assert merged["exposes"][0]["binding"]["platform"] == "local"  # still the base
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "'awz'" in message
    assert "aws, gcp" in message
    assert "BASE contract" in message


def test_the_warning_is_emitted_once_per_contract_and_env(
    contract: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    loader.load_with_overlay(contract, "awz")
    loader.load_with_overlay(contract, "awz")
    assert len(_records(caplog, logging.WARNING)) == 1


def test_dev_without_overlay_is_the_base_by_convention_at_info(
    contract: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    merged = loader.load_with_overlay(contract, "dev")
    assert merged["exposes"][0]["binding"]["platform"] == "local"
    assert _records(caplog, logging.WARNING) == []
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert "base by convention" in infos[0].getMessage()


def test_an_existing_overlay_is_applied_without_a_notice(
    contract: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    merged = loader.load_with_overlay(contract, "aws")
    assert merged["exposes"][0]["binding"]["platform"] == "aws"
    assert _records(caplog, logging.WARNING) == []


def test_no_overlays_at_all_says_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_BASE, encoding="utf-8")
    caplog.set_level(logging.INFO)
    loader.load_with_overlay(path, "prod")
    (warning,) = _records(caplog, logging.WARNING)
    assert "Overlays that exist: none" in warning.getMessage()
