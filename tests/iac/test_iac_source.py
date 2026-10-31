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

"""Which contract file a module is compiled from (``fluid_build/iac/source.py``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from fluid_build.iac.source import contract_source, current_contract_source, source_contract_file

pytestmark = [pytest.mark.unit]


def test_a_block_names_the_file_and_restores_what_was_before(tmp_path: Path) -> None:
    outer, inner = tmp_path / "a.fluid.yaml", tmp_path / "b.fluid.yaml"
    assert current_contract_source() is None
    with contract_source(outer):
        assert current_contract_source() == outer
        with contract_source(inner):
            assert current_contract_source() == inner
        assert current_contract_source() == outer
        with contract_source(None):
            assert current_contract_source() is None
    assert current_contract_source() is None


def test_another_thread_does_not_see_this_block(tmp_path: Path) -> None:
    seen = []
    with contract_source(tmp_path / "a.fluid.yaml"):
        thread = threading.Thread(target=lambda: seen.append(current_contract_source()))
        thread.start()
        thread.join()
    assert seen == [None]


def test_a_contract_file_is_itself(tmp_path: Path) -> None:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text("id: x\n", encoding="utf-8")
    assert source_contract_file(str(contract)) == contract.resolve()
    assert source_contract_file(None) is None and source_contract_file("") is None


def test_a_plan_is_the_source_contract_it_records(tmp_path: Path) -> None:
    contract = tmp_path / "src" / "contract.fluid.yaml"
    contract.parent.mkdir()
    contract.write_text("id: x\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"contract_metadata": {"source_path": str(contract)}, "actions": []}),
        encoding="utf-8",
    )
    assert source_contract_file(plan) == contract.resolve()


def test_a_plan_whose_source_is_gone_is_unknown(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"contract_metadata": {"source_path": str(tmp_path / "gone.yaml")}}),
        encoding="utf-8",
    )
    assert source_contract_file(plan) is None


def test_a_json_contract_that_is_no_plan_is_itself(tmp_path: Path) -> None:
    contract = tmp_path / "contract.fluid.json"
    contract.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    assert source_contract_file(contract) == contract.resolve()
