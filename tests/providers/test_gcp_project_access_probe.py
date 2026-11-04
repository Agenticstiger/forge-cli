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

"""The GCP project-access probe says "not checked" without its client library."""

import sys

import pytest

from fluid_build.providers.gcp.util import auth

pytestmark = [pytest.mark.unit]


def test_without_google_api_python_client_the_probe_is_not_checked(monkeypatch):
    # ``None`` in sys.modules is how the import system spells "not installed".
    monkeypatch.setitem(sys.modules, "googleapiclient", None)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", None)
    assert auth._test_project_access("my-project", credentials=None) is None
