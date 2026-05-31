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

"""Provision the OpenTofu (``tofu``) binary — pinned, SHA256-verified, no root.

``fluid apply`` on a cloud provider shells out to ``tofu`` via the OpenTofu
engine. Asking every CI runner / Dockerfile to install it is fragile — the
official standalone installer needs root (it writes ``/usr/local/bin``) and
``gpg``/``cosign`` to verify, which locked-down runners (e.g. a non-root
Jenkins agent) can't provide.

This installs ``tofu`` using only the Python standard library — which every
host running ``fluid`` has by definition, since ``fluid`` is a Python package —
so it works **without root, gpg, cosign, curl, or unzip**, on any OS/arch:

* download the pinned release zip + the release's ``SHA256SUMS`` over TLS from
  the official OpenTofu GitHub release,
* verify the zip's SHA-256 against the published sums (integrity) **before**
  extracting,
* extract only the ``tofu`` entry (no ``extractall`` — no zip-slip) into a
  writable dir (the console-scripts dir that already holds ``fluid``, else a
  user cache dir), and prepend that dir to this process's ``PATH`` so the
  engine's ``tofu`` lookup resolves it even when the dir isn't on the shell
  ``PATH``.

Idempotent: if a usable ``tofu`` is already on ``PATH`` (>= the engine's
floor), it is returned untouched — so a pre-baked runner image wins.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import stat
import sys
import sysconfig
import urllib.request
import zipfile
from typing import Optional

from fluid_build.iac.runner import _MIN_REQUIRED_VERSION, tofu_path, tofu_version

# Pinned default — a recent OpenTofu stable at/above the engine's floor
# (``_MIN_REQUIRED_VERSION``). Override per-environment with
# ``FLUID_OPENTOFU_VERSION`` (e.g. to match an org-standardised tofu).
PINNED_OPENTOFU_VERSION = "1.12.1"

# Fixed host + scheme — never user-derived, so there is no SSRF surface.
_RELEASE_BASE = "https://github.com/opentofu/opentofu/releases/download"

_OS_TAGS = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
_ARCH_TAGS = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class OpenTofuInstallError(RuntimeError):
    """Raised when provisioning the ``tofu`` binary fails."""


def _platform_tags() -> tuple[str, str]:
    os_tag = _OS_TAGS.get(platform.system().lower())
    arch_tag = _ARCH_TAGS.get(platform.machine().lower())
    if not os_tag or not arch_tag:
        raise OpenTofuInstallError(
            f"unsupported platform {platform.system()}/{platform.machine()} for "
            "automatic OpenTofu provisioning — install `tofu` manually"
        )
    return os_tag, arch_tag


def _install_dir() -> str:
    """A writable dir for the binary — the console-scripts dir (already on PATH,
    holds ``fluid``) when writable, else a user cache dir we add to PATH."""
    scripts = sysconfig.get_path("scripts") or os.path.dirname(sys.executable)
    if scripts and os.path.isdir(scripts) and os.access(scripts, os.W_OK):
        return scripts
    cache = os.path.join(os.path.expanduser("~"), ".cache", "fluid", "opentofu", "bin")
    os.makedirs(cache, exist_ok=True)
    return cache


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fluid-opentofu-installer"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed HTTPS host
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - surface any fetch failure uniformly
        raise OpenTofuInstallError(f"failed to download {url}: {exc}") from exc


def _expected_sha(sums_text: str, filename: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    raise OpenTofuInstallError(f"{filename} not listed in the release SHA256SUMS")


def ensure_opentofu(
    *, version: Optional[str] = None, logger: Optional[logging.Logger] = None
) -> str:
    """Ensure a usable ``tofu`` binary is available; return its path.

    Idempotent — returns an existing PATH ``tofu`` (>= the engine floor)
    untouched. Otherwise downloads the pinned release, verifies its SHA-256
    against the published sums, installs it to a writable dir, and prepends
    that dir to ``PATH`` for the current process.
    """
    log = logger or logging.getLogger(__name__)
    version = version or os.environ.get("FLUID_OPENTOFU_VERSION") or PINNED_OPENTOFU_VERSION

    existing = tofu_path()
    if existing:
        current = tofu_version()
        if current is None or current >= _MIN_REQUIRED_VERSION:
            log.debug("OpenTofu already present at %s — skipping provisioning.", existing)
            return existing

    os_tag, arch_tag = _platform_tags()
    zip_name = f"tofu_{version}_{os_tag}_{arch_tag}.zip"
    base = f"{_RELEASE_BASE}/v{version}"
    log.info("Provisioning OpenTofu v%s (%s/%s)…", version, os_tag, arch_tag)

    zip_bytes = _download(f"{base}/{zip_name}")
    sums_text = _download(f"{base}/tofu_{version}_SHA256SUMS").decode("utf-8", "replace")
    expected = _expected_sha(sums_text, zip_name)
    actual = hashlib.sha256(zip_bytes).hexdigest()
    if actual != expected:
        raise OpenTofuInstallError(
            f"SHA-256 mismatch for {zip_name}: expected {expected}, got {actual} "
            "— refusing to install a tampered or corrupt binary"
        )

    bin_name = "tofu.exe" if os_tag == "windows" else "tofu"
    dest_dir = _install_dir()
    dest = os.path.join(dest_dir, bin_name)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Extract ONLY the tofu entry by name — never extractall (no zip-slip).
        with zf.open(bin_name) as src, open(dest, "wb") as out:
            out.write(src.read())
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Make it resolvable in THIS process — the engine shells out to `tofu` via a
    # PATH lookup, and dest_dir may not be on the shell PATH (cache-dir case).
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if dest_dir not in path_parts:
        os.environ["PATH"] = dest_dir + os.pathsep + os.environ.get("PATH", "")

    log.info("OpenTofu v%s installed at %s", version, dest)
    return dest
