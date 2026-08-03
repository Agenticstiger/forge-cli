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

"""Credential plumbing for ``fluid apply`` (the OpenTofu engine).

The ``tofu`` child process inherits the full environment, so cloud
credentials present in the environment (or in standard credential files
such as ``~/.aws/credentials`` or GCP Application Default Credentials)
flow through unchanged. The emitted ``.tf.json`` never carries secrets —
this module only plumbs the *environment* to the child process.
"""

from __future__ import annotations

import os
from typing import Dict, List, Mapping, Optional, Tuple

from .base import IacProviderPlugin


def build_tofu_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return the environment for the ``tofu`` child process.

    A mutable copy of the full environment — so ``tofu`` keeps ``PATH``
    and any cloud credentials already present. Callers may overlay
    additional provider-specific variables onto the result.
    """
    return dict(base if base is not None else os.environ)


def credential_report(
    plugin: IacProviderPlugin, env: Mapping[str, str]
) -> Tuple[List[str], List[str]]:
    """Split a plugin's credential env vars into ``(present, absent)``.

    Informational only: providers also accept file-based credentials
    (``~/.aws/credentials``, GCP ADC), so an absent env var does not by
    itself mean credentials are unavailable.
    """
    present: List[str] = []
    absent: List[str] = []
    for var in getattr(plugin, "credential_env_vars", ()):
        (present if env.get(var) else absent).append(var)
    return present, absent
