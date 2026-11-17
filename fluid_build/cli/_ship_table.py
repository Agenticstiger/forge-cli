# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The 11 stages of ``fluid ship``: their numbers, names and switches, and the
variables ship reads. A leaf that imports nothing from fluid_build, so the
generated CI wrappers (``forge/core/pipeline_systems/_thin.py``) name the same
stages and variables without importing ship itself.
"""

from __future__ import annotations

from typing import Dict, Tuple

__all__ = ["SHIP_VARIABLES", "STAGES", "STAGE_NAMES", "STAGE_NUMBERS", "STAGE_TOGGLES"]

#: (number, name, aliases). The names are what ``--stage`` / ``--until`` /
#: ``--skip-stage`` accept next to the number.
STAGES: Tuple[Tuple[int, str, Tuple[str, ...]], ...] = (
    (1, "bundle", ()),
    (2, "validate", ()),
    (3, "generate-artifacts", ("artifacts", "generate_artifacts")),
    (4, "validate-artifacts", ("validate_artifacts",)),
    (5, "diff", ()),
    (6, "plan", ()),
    (7, "apply", ()),
    (8, "policy-apply", ("policy", "policy_apply")),
    (9, "verify", ()),
    (10, "publish", ()),
    (11, "schedule-sync", ("schedule_sync",)),
)
STAGE_NUMBERS: Tuple[int, ...] = tuple(s[0] for s in STAGES)
STAGE_NAMES: Dict[int, str] = {num: name for num, name, _ in STAGES}

#: The switch a generated pipeline exposes for each stage, read from the
#: environment: ``false`` switches a stage off (like ``--skip-stage``, and with
#: ``--stage`` too, since a CI step names a stage it does not decide on), and
#: ``true`` switches on the opt-in stages 10 and 11 when ``--stage`` is not
#: given (a step that names its stage would otherwise run them too).
STAGE_TOGGLES: Dict[int, str] = {
    1: "RUN_STAGE_1_BUNDLE",
    2: "RUN_STAGE_2_VALIDATE",
    3: "RUN_STAGE_3_GENERATE_ARTIFACTS",
    4: "RUN_STAGE_4_VALIDATE_ARTIFACTS",
    5: "RUN_STAGE_5_DIFF",
    6: "RUN_STAGE_6_PLAN",
    7: "RUN_STAGE_7_APPLY",
    8: "RUN_STAGE_8_POLICY_APPLY",
    9: "RUN_STAGE_9_VERIFY",
    10: "RUN_STAGE_10_PUBLISH",
    11: "RUN_STAGE_11_SCHEDULE_SYNC",
}

#: Every variable ``_ship_stages.resolve_options`` reads, the switches aside: the job
#: environment a generated wrapper passes through to ship. A test holds this
#: to what ``resolve_options`` actually reads.
SHIP_VARIABLES: Tuple[str, ...] = (
    "FLUID_ENV",
    "APPLY_MODE",
    "APPLY_BUILD_ID",
    "ALLOW_DATA_LOSS",
    "NO_VERIFY_DIGEST",
    "PUBLISH_TARGETS",
    "CATALOG",
    "SCHEDULER",
    "SCHEDULER_DESTINATION",
    "SCHEDULER_ENVIRONMENT_NAME",
    "SCHEDULER_LOCATION",
    "SCHEDULER_WORKSPACE",
    "SCHEDULE_SYNC_DRY_RUN",
    "VALIDATE_STRICT",
    "VERIFY_STRICT",
    "GENERATE_EMIT",
    "DIFF_EXIT_ON_DRIFT",
    "DIFF_LAST_APPLIED",
    "PLAN_HTML",
    "POLICY_APPLY_MODE",
)
