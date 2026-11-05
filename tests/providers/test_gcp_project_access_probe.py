# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
