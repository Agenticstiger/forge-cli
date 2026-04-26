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

"""
FLUID Build - Data Products as Code

Version and feature detection for staged release management.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - optional fallback
        tomllib = None  # type: ignore[assignment]

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("data-product-forge")
except PackageNotFoundError:

    def _version_from_git_checkout() -> Optional[str]:
        repo_root = Path(__file__).resolve().parent.parent
        try:
            raw = subprocess.check_output(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            return None

        if raw.startswith("v"):
            raw = raw[1:]
        raw = raw.removesuffix("-dirty")
        match = re.match(r"(?P<tag>[^-]+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)$", raw)
        if match:
            return f"{match.group('tag')}.dev{match.group('distance')}"
        return raw or None

    def _fallback_version() -> str:
        git_version = _version_from_git_checkout()
        if git_version:
            return git_version

        if tomllib is not None:
            pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                fallback = (
                    ((data.get("tool") or {}).get("setuptools_scm") or {}).get("fallback_version")
                ) or "0.0.0+source"
                if fallback != "0.0.0+unknown":
                    return str(fallback)
            except Exception:
                pass
        return "0.0.0+source"

    # Editable / source checkout before the package is installed.
    __version__ = _fallback_version()

__all__ = [
    "__version__",
    "get_build_profile",
    "get_enabled_providers",
    "get_enabled_commands",
    "is_provider_enabled",
    "is_command_enabled",
    "get_feature_status",
]

# ============================================================================
# Feature Release System (MVP)
# ============================================================================

# Load build manifest at import time (cached)
_MANIFEST_PATH = Path(__file__).parent / "build-manifest.yaml"
_MANIFEST: Optional[Dict[str, Any]] = None


def _load_manifest() -> Dict[str, Any]:
    """Load build manifest (cached for performance)."""
    global _MANIFEST
    if _MANIFEST is None:
        if _MANIFEST_PATH.exists():
            with open(_MANIFEST_PATH) as f:
                _MANIFEST = yaml.safe_load(f)
        else:
            # Fallback: everything enabled (backwards compatibility)
            _MANIFEST = {
                "builds": {
                    "experimental": {"commands": [], "providers": []},
                    "alpha": {"commands": [], "providers": []},
                    "beta": {"commands": [], "providers": []},
                    "stable": {"commands": [], "providers": []},
                }
            }
    return _MANIFEST


def get_build_profile() -> str:
    """
    Get current build profile from environment or default to experimental.

    Profiles:
    - experimental: Kitchen sink - all commands/providers (no filtering)
    - alpha: Bleeding edge features (development)
    - beta: Stable + beta features (public testing)
    - stable: Stable features only (production)

    Set via: export FLUID_BUILD_PROFILE=stable
    """
    return os.getenv("FLUID_BUILD_PROFILE", "experimental")


def get_enabled_providers() -> Set[str]:
    """
    Get providers enabled in current build profile.

    Returns:
        Set of provider names (e.g., {'local', 'gcp', 'snowflake'})

    Example:
        >>> import fluid_build
        >>> fluid_build.get_enabled_providers()
        {'local', 'gcp'}
    """
    manifest = _load_manifest()
    profile = get_build_profile()

    # Get providers list for this profile
    build_config = manifest.get("builds", {}).get(profile, {})
    providers = build_config.get("providers", [])

    return set(providers)


def get_enabled_commands() -> Set[str]:
    """
    Get commands enabled in current build profile.

    Returns:
        Set of command names (e.g., {'validate', 'plan', 'apply'})

    Example:
        >>> import fluid_build
        >>> len(fluid_build.get_enabled_commands())
        15
    """
    manifest = _load_manifest()
    profile = get_build_profile()

    # Get commands list for this profile
    build_config = manifest.get("builds", {}).get(profile, {})
    commands = build_config.get("commands", [])

    return set(commands)


def is_provider_enabled(name: str) -> bool:
    """
    Check if a provider is enabled in current build.

    Args:
        name: Provider name (e.g., 'gcp', 'snowflake')

    Returns:
        True if enabled, False otherwise

    Example:
        >>> import fluid_build
        >>> fluid_build.is_provider_enabled('gcp')
        True
        >>> fluid_build.is_provider_enabled('aws')  # alpha only
        False
    """
    return name in get_enabled_providers()


def is_command_enabled(name: str) -> bool:
    """
    Check if a command is enabled in current build.

    Args:
        name: Command name (e.g., 'validate', 'copilot')

    Returns:
        True if enabled, False otherwise

    Example:
        >>> import fluid_build
        >>> fluid_build.is_command_enabled('validate')
        True
        >>> fluid_build.is_command_enabled('copilot')  # alpha only
        False
    """
    return name in get_enabled_commands()


def get_feature_status(feature_type: str, name: str) -> Optional[str]:
    """
    Get the maturity status of a provider or command.

    NOTE: This looks up which profile a feature appears in, not its maturity status.
    For maturity tracking, see features.yaml (used by test reporting).

    Args:
        feature_type: 'provider' or 'command'
        name: Name of the feature

    Returns:
        Profile name where feature first appears ('stable', 'beta', 'alpha', 'experimental')
        or None if not found

    Example:
        >>> import fluid_build
        >>> fluid_build.get_feature_status('provider', 'gcp')
        'stable'
        >>> fluid_build.get_feature_status('provider', 'snowflake')
        'beta'
    """
    manifest = _load_manifest()

    # Check profiles in order: stable -> beta -> alpha -> experimental
    for profile in ["stable", "beta", "alpha", "experimental"]:
        build = manifest.get("builds", {}).get(profile, {})
        if feature_type == "provider":
            if name in build.get("providers", []):
                return profile
        elif feature_type == "command":
            if name in build.get("commands", []):
                return profile

    return None


def get_features_summary() -> Dict[str, Any]:
    """
    Get summary of current build profile for debugging.

    Returns:
        Dict with profile info, enabled providers, commands, etc.

    Example:
        >>> import fluid_build
        >>> summary = fluid_build.get_features_summary()
        >>> print(summary['profile'])
        'stable'
        >>> print(summary['providers'])
        ['gcp', 'local']
    """
    return {
        "version": __version__,
        "profile": get_build_profile(),
        "providers": sorted(get_enabled_providers()),
        "commands": sorted(get_enabled_commands()),
        "provider_count": len(get_enabled_providers()),
        "command_count": len(get_enabled_commands()),
    }
