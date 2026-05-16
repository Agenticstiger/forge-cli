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

"""Bounded YAML loading for untrusted input.

``yaml.safe_load`` blocks arbitrary-object construction but does NOT cap
anchor/alias expansion: a small document with nested YAML aliases can
balloon to gigabytes when composed (the "billion laughs" denial-of-service).

There is no drop-in OSS parser that fixes this without dropping anchor
support entirely (StrictYAML) — see PyYAML issue #235. So we pre-scan the
cheap event stream: ``yaml.parse`` tokenises the document WITHOUT resolving
aliases, so it stays linear-time even for a malicious payload. We count
``AliasEvent``s and reject over-budget input before the expensive
``safe_load`` compose/construct step ever runs.

Use :func:`load_yaml_safe` for any YAML that arrives from outside the
process: LLM output, network/catalog responses, and bundle contents.
"""

from __future__ import annotations

from typing import Any

import yaml

# Caps tuned well above any legitimate contract / manifest. A real data
# contract uses few-to-zero YAML aliases; a billion-laughs payload needs
# dozens of nested alias references to reach dangerous expansion.
MAX_YAML_BYTES = 5 * 1024 * 1024
MAX_YAML_ALIASES = 50


class UnsafeYamlError(ValueError):
    """Raised when untrusted YAML exceeds the size or alias-expansion caps."""


def load_yaml_safe(
    source: Any,
    *,
    max_bytes: int = MAX_YAML_BYTES,
    max_aliases: int = MAX_YAML_ALIASES,
) -> Any:
    """``yaml.safe_load`` with billion-laughs (anchor-expansion) protection.

    Accepts a ``str``, ``bytes``, or a readable stream. ``yaml.parse``
    does not resolve aliases, so the pre-scan is cheap even for a hostile
    payload — ``AliasEvent``s are counted and the document is rejected
    before the expensive ``safe_load`` compose/construct step.
    """
    if source is None:
        return None
    if hasattr(source, "read"):
        source = source.read()
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    if not isinstance(source, str):
        raise UnsafeYamlError(f"load_yaml_safe expects text, got {type(source).__name__}")
    if len(source) > max_bytes:
        raise UnsafeYamlError(
            f"YAML input is {len(source)} bytes, over the {max_bytes}-byte safety cap"
        )
    aliases = 0
    for event in yaml.parse(source, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            aliases += 1
            if aliases > max_aliases:
                raise UnsafeYamlError(
                    f"YAML input has more than {max_aliases} alias references — "
                    "possible anchor-expansion (billion-laughs) DoS"
                )
    return yaml.safe_load(source)
