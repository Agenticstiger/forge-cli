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

"""Which relation a build stage produces, resolved once for every engine.

`stages[].outputs` names the exposes a stage feeds, and a contract's own SQL
refers to stages by those OUTPUT names -- `FROM customer_base`, never
`FROM stage_1_customer_base`. Every engine that turns a stage into a named
thing therefore needs the same mapping, and needs to agree about it: the dbt
engine names `models/<layer>/<name>.sql` after it, and the sql engine names
the view each stage materialises.

Kept as a sibling leaf rather than in `base.py` (which is a pure ABC +
dataclass module) or imported across from `engines/dbt/` (which would be the
first sibling-engine import in the tree). `engines/__init__.py` pkgutil-imports
only packages, so a private module here is never mistaken for an engine.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)

DEFAULT_COLLISION_EVENT = "stage_output_name_collision"


def resolve_stage_outputs(
    stages: Any,
    *,
    log_event: str = DEFAULT_COLLISION_EVENT,
) -> Dict[str, Optional[str]]:
    """Map each stage name to the output it EXCLUSIVELY owns, else ``None``.

    The first usable output wins. Only the first is used -- a stage declaring
    several still produces one relation today, and silently picking one of
    many would be worse than not renaming at all, so the rest are left alone.

    A name two stages would both claim is given to neither, and both come back
    ``None``. Callers key things by that name -- a file path, a view in a
    catalog -- so letting both claim it would drop one stage on the floor,
    which is the same class of silent loss the mapping exists to end.
    Contested stages fall back to whatever the caller did before outputs were
    read at all, and the collision is logged once per contested name.

    Contest detection runs on EFFECTIVE names, not on declared outputs alone:
    a stage literally named ``orders`` contests a stage whose output is
    ``orders``, because both would end up asking for ``orders``.

    Malformed entries (non-dicts, missing or empty names) are skipped rather
    than raised on. This runs before anything is emitted, and a contract that
    is merely odd should not turn a naming improvement into a failed generate.
    """
    claimed: Dict[str, Optional[str]] = {}
    effective: Dict[str, str] = {}

    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        name = stage.get("name")
        if not name:
            continue
        key = str(name)
        outputs = stage.get("outputs") or []
        first = next((o for o in outputs if isinstance(o, str) and o), None)
        claimed[key] = first
        effective[key] = first or key

    contested = {n for n, count in Counter(effective.values()).items() if count > 1}
    if not contested:
        return claimed

    for name in sorted(contested):
        losers = sorted(s for s, eff in effective.items() if eff == name)
        _logger.warning(
            "%s: stages %s all resolve to %r; none of them claims it, "
            "so no stage is overwritten",
            log_event,
            losers,
            name,
        )

    return {
        stage_name: (None if effective[stage_name] in contested else output)
        for stage_name, output in claimed.items()
    }


def stage_model_names(stages: Any) -> Dict[str, str]:
    """``resolve_stage_outputs`` with a stage's own name as the fallback.

    The shape the dbt engine needs: a model is a file, so it always needs
    *some* name. A stage with no exclusively-owned output keeps its own.
    """
    return {
        stage_name: (output or stage_name)
        for stage_name, output in resolve_stage_outputs(
            stages, log_event="dbt_stage_model_name_collision"
        ).items()
    }
