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

"""Guard against version drift between pyproject.toml, __init__.py, and build-manifest.yaml."""

import sys
from pathlib import Path

import yaml

# Locate the repo root (parent of tests/)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_pyproject_version() -> str:
    """Read version from pyproject.toml without importing the package."""
    pyproject = REPO_ROOT / "pyproject.toml"
    assert pyproject.exists(), f"pyproject.toml not found at {pyproject}"

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def _read_init_version() -> str:
    """Read __version__ from fluid_build/__init__.py."""
    import fluid_build

    return fluid_build.__version__


def _read_manifest_base_version() -> str:
    """Read base_version from build-manifest.yaml."""
    manifest = REPO_ROOT / "fluid_build" / "build-manifest.yaml"
    assert manifest.exists(), f"build-manifest.yaml not found at {manifest}"

    with open(manifest) as f:
        data = yaml.safe_load(f)
    return data["metadata"]["pypi"]["base_version"]


def test_pyproject_and_init_versions_match():
    """pyproject.toml version must equal fluid_build.__version__."""
    pyproject_ver = _read_pyproject_version()
    init_ver = _read_init_version()
    assert pyproject_ver == init_ver, (
        f"Version mismatch: pyproject.toml={pyproject_ver}, " f"fluid_build.__version__={init_ver}"
    )


def test_build_manifest_base_version_matches():
    """build-manifest.yaml base_version must equal pyproject.toml version."""
    pyproject_ver = _read_pyproject_version()
    manifest_ver = _read_manifest_base_version()
    assert pyproject_ver == manifest_ver, (
        f"Version mismatch: pyproject.toml={pyproject_ver}, "
        f"build-manifest.yaml base_version={manifest_ver}"
    )
