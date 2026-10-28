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

"""The system directories no path FLUID reads may name: a tier-0 leaf.

The CLI's path checks (``cli.security``) and the contract composition
(``contract_refs``, which ``build_runners`` reaches through the loader)
deny the same set. It lives here, stdlib-only, so the composition does not
import ``cli``. ``cli.security`` re-exports both names.
"""

from __future__ import annotations

import platform
from typing import Set

__all__ = ["FORBIDDEN_PATHS", "build_forbidden_paths"]


def build_forbidden_paths() -> Set[str]:
    """Return the platform-specific set of system-path prefixes to deny.

    SECURITY_REVIEW S-002: the previous hard-coded Linux-only set missed
    macOS (``/etc`` resolves to ``/private/etc``, which has no prefix in
    the Linux set) and Windows entirely. The check site uses
    ``Path.is_relative_to`` for a proper domain-boundary comparison
    rather than a naive string ``startswith`` — that fixes the
    false-positive on siblings like ``/etcd/file.yaml``.
    """
    system = platform.system()
    if system == "Darwin":
        # macOS resolves ``/etc`` to ``/private/etc`` via symlink. Include
        # ``/private/etc`` so paths that have already been through
        # ``Path.resolve()`` — which all our validators do — still hit
        # the deny. Deliberately NOT including ``/private/var`` or
        # ``/private/tmp``: pytest's ``tmp_path`` fixture lives at
        # ``/private/var/folders/…`` on macOS, and legitimate app code
        # uses ``/tmp``. The review's concern is config + binary +
        # password locations; ``/etc`` (and its ``/private/etc``
        # resolved form) is the one that actually matters there.
        #
        # F4: ``/opt`` (third-party installs), ``/Library`` (system +
        # app support / LaunchAgents), and ``/var/lib`` (service state)
        # are added. ``/var/lib`` is deliberately narrower than bare
        # ``/var`` so the macOS ``/private/var/folders`` temp tree
        # (pytest's ``tmp_path`` on macOS) remains reachable.
        # NOTE: bare ``/var`` is intentionally absent — on macOS it
        # resolves to ``/private/var`` anyway (inert), and on Linux it
        # over-broadly blocks Jenkins/Docker workspace paths under
        # ``/var/jenkins_home``. See the Linux branch note below.
        return {
            "/etc",
            "/private/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/root",
            "/System",
            "/Library",
            "/opt",
            "/var/lib",
        }
    if system == "Windows":
        return {
            "C:\\Windows",
            "C:\\Program Files",
            "C:\\Program Files (x86)",
            "C:\\ProgramData",
        }
    # Linux and other Unix-likes.
    # F4: ``/opt`` (third-party installs), ``/Library`` (harmless on
    # Linux but kept for cross-platform parity), and ``/var/lib``
    # (service state — databases, container layers) are added.
    #
    # NOTE: bare ``/var`` is intentionally NOT in this set. Jenkins
    # workspaces live at ``/var/jenkins_home/…`` and Docker bind-mounts
    # commonly resolve under ``/var/jenkins_home`` or ``/var/lib/docker``.
    # ``/var/lib`` (already present) is the specific concern (service
    # state / container layers); blocking all of ``/var`` is over-broad
    # and breaks every Jenkins-container pipeline build at Stage 1 (bundle).
    return {
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/var/lib",
        "/root",
        "/opt",
        "/Library",
    }


FORBIDDEN_PATHS = build_forbidden_paths()
