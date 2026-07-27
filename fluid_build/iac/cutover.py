# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Per-provider apply-engine registry.

``fluid apply`` resolves its engine per provider — there is no user-facing
switch. A provider listed in ``OPENTOFU_DEFAULT_PROVIDERS`` compiles its
contract to ``.tf.json`` and runs ``tofu``; any other provider keeps its
native apply. The OpenTofu migration cut the clouds over one at a time
behind this registry (the strangler-fig pattern — see ``AUTOGEN_SPIKE.md``);
it is now the stable extension point for adding a new cloud.
"""

from __future__ import annotations

from typing import FrozenSet, Optional

#: Providers whose default apply engine is OpenTofu. A cutover PR adds a
#: name here and, in the same PR, retires that provider's native CRUD.
#:
#: ``gcp``, ``aws``, ``snowflake`` — all cut over: their ``actions/``
#: packages and native apply paths are retired (see ``AUTOGEN_SPIKE.md``);
#: ``fluid apply`` compiles their contracts to ``.tf.json`` and runs
#: ``tofu``. The OpenTofu engine is the default for every provider.
OPENTOFU_DEFAULT_PROVIDERS: FrozenSet[str] = frozenset({"aws", "gcp", "snowflake", "confluent"})


def default_engine(provider: str) -> str:
    """Return the default apply engine (``native``/``opentofu``) for ``provider``.

    ``OPENTOFU_DEFAULT_PROVIDERS`` stays authoritative for the in-tree clouds
    — it is the strangler-fig cutover switch, and a cloud with an emitter but
    no entry in the set is deliberately still on its native path.

    An **out-of-tree** cloud, registered through the ``fluid_build.iac_providers``
    entry-point group, cannot appear in that frozenset without a core edit —
    which is precisely the "zero edits to forge-cli core" promise
    ``registry.py`` makes. Such a plugin exists only to emit an OpenTofu
    module, so it routes to the OpenTofu engine automatically. Before this,
    ``default_engine("<plugin-cloud>")`` returned ``native`` and the IaC path
    the plugin was written for was never selected.
    """
    if provider in OPENTOFU_DEFAULT_PROVIDERS:
        return "opentofu"
    # Local import: ``iac/__init__`` pulls every provider plugin, and this
    # module is imported on the apply cold path.
    try:
        from .registry import IAC_ENTRYPOINT_PLUGINS

        if provider in IAC_ENTRYPOINT_PLUGINS:
            return "opentofu"
    except Exception:  # noqa: BLE001 — registry unavailable → the safe default
        pass
    return "native"


def resolve_engine(explicit: Optional[str], provider: str) -> str:
    """Resolve the apply engine for a run.

    A non-``None`` ``explicit`` (``native``/``opentofu``) is a programmatic
    override and wins; ``None`` or ``"auto"`` means "no override" — the
    per-provider default from ``OPENTOFU_DEFAULT_PROVIDERS`` applies.
    """
    if explicit in ("native", "opentofu"):
        return explicit
    return default_engine(provider)
