# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The environment-name grammar ``fluid publish --env`` accepts.

One definition, read by ``fluid publish`` (its ``--env`` argparse type and
its programmatic entry point) and by the pipeline generator, which holds a
generated pipeline's ``FLUID_ENV`` default to it: a default ``fluid
publish`` refuses would fail the publish stage of every build that runs on
the defaults, after the apply stage has already run.

Stdlib-only tier-0 leaf, enforced by the ``shared tier-0 leaves`` contract in
``[tool.importlinter]`` (``pyproject.toml``): ``forge`` imports it without
pulling ``cli.publish``, and what that imports, into ``fluid apply``'s import
closure.
"""

from __future__ import annotations

import re
from typing import Any

#: Letters, digits, '.', '_' and '-', starting with a letter or digit, at most
#: 64 characters. Match it with ``fullmatch``: a ``$`` anchor would also match
#: before a trailing newline.
ENV_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

#: :data:`ENV_NAME_RE` in words, for error messages.
ENV_NAME_RULE = (
    "use letters, digits, '.', '_' and '-' "
    "(at most 64 characters, starting with a letter or digit)"
)


def is_env_name(value: Any) -> bool:
    """True when ``value`` is a name ``fluid publish --env`` accepts."""
    return isinstance(value, str) and ENV_NAME_RE.fullmatch(value) is not None
