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

"""Kubernetes harness — detect minikube / kind / OrbStack and skip cleanly when absent.

Tests that need a live cluster import ``requires_k8s`` and skip with a clear
reason on dev machines without a cluster. CI that runs the full integration
matrix provisions a cluster before the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import pytest


def _kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


def _helm_available() -> bool:
    return shutil.which("helm") is not None


def _has_cluster() -> bool:
    if not _kubectl_available():
        return False
    if os.environ.get("FLUID_DISABLE_K8S") == "1":
        return False
    try:
        r = subprocess.run(
            ["kubectl", "cluster-info"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def requires_kubectl(reason: str = "kubectl not installed") -> Any:
    return pytest.mark.skipif(not _kubectl_available(), reason=reason)


def requires_helm(reason: str = "helm not installed") -> Any:
    return pytest.mark.skipif(not _helm_available(), reason=reason)


def requires_k8s(reason: str = "no live K8s cluster reachable") -> Any:
    return pytest.mark.skipif(not _has_cluster(), reason=reason)
