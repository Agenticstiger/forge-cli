# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``{{ env.* }}`` resolution for the spec exporters.

An exported ODCS / ODPS document is a *published artifact* — it goes to a
catalog and to downstream teams. Shipping one whose ``servers[0].account`` is
the literal string ``{{ env.SNOWFLAKE_ACCOUNT }}`` makes it unusable: nothing
in the document names the object it describes.

``plan``/``apply`` resolve these at the Snowflake provider boundary and
``publish`` resolves them for the same reason this module exists (the catalog
adapter forwards the contract body downstream). The exporters were the one
path that forwarded the raw templates.

Resolution routes through :func:`resolve_contract_env_templates`, which is
already secret-aware: placeholders whose *name* looks like a credential
(``{{ env.SNOWFLAKE_PASSWORD }}``, ``{{ env.*_API_KEY }}``) are deliberately
left literal so an export never exfiltrates one, and an unset variable is left
as-is rather than becoming an empty string.

Set ``FLUID_EXPORT_RESOLVE_ENV=false`` to publish the templates verbatim —
useful when the same document is rendered once and deployed to several
environments.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping

_DISABLE_ENV = "FLUID_EXPORT_RESOLVE_ENV"


def resolve_enabled() -> bool:
    return os.getenv(_DISABLE_ENV, "true").lower() != "false"


def resolve_for_export(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve ``{{ env.* }}`` in *contract* unless the opt-out is set."""
    if not resolve_enabled():
        return dict(contract)
    from fluid_build._contract_loader import resolve_contract_env_templates

    resolved = resolve_contract_env_templates(dict(contract))
    return resolved if isinstance(resolved, dict) else dict(contract)


__all__ = ["resolve_for_export", "resolve_enabled"]
