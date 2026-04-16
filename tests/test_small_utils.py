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

"""Tests for small utility modules at 0% coverage."""

import fluid_build.tools
from fluid_build.util.schema import project_id_from_contract


def test_project_id_from_contract_returns_fallback():
    assert project_id_from_contract({}, "my-project") == "my-project"


def test_project_id_from_contract_no_fallback():
    assert project_id_from_contract({}) is None


def test_tools_all():
    assert fluid_build.tools.__all__ == []
