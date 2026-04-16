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

"""Progressive ``--help`` — hide power flags behind ``--advanced``.

UX audit F7 ("cognitive overload: too many flags in the default
help").  ``fluid init --help`` and ``fluid forge --help`` both print
a long wall of flags that a first-time user does not need.  Slice
UX-D adds an ``--advanced`` top-level flag and a helper that marks
individual parser arguments as hidden-from-default-help.

How to use in a parser registration::

    from fluid_build.cli.help_advanced import mark_advanced

    p = subparsers.add_parser("init", ...)
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="skip prompts")
    mark_advanced(
        p.add_argument("--industry", help="...")
    )
    mark_advanced(
        p.add_argument("--provider", help="...")
    )

Behavior:

* By default, ``--help`` shows only the non-advanced flags.  Advanced
  flags are present in the parser (they still work on the command
  line) but their help text is suppressed.
* When the user passes ``--advanced`` anywhere in the command line,
  every flag becomes visible in ``--help`` output.  The flag is
  consumed before argparse's own parse-args runs so it doesn't leak
  into subcommand namespaces.
* Works with argparse out of the box — we simply toggle the ``help``
  attribute to ``argparse.SUPPRESS`` based on whether ``--advanced``
  is present in ``sys.argv``.

The helper is intentionally small (no subclassing of
``ArgumentParser``) so existing parser-registration code in
``bootstrap.py`` / ``init.py`` / ``forge.py`` can opt in one flag at
a time without a mass refactor.
"""

from __future__ import annotations

__all__ = [
    "ADVANCED_FLAG",
    "is_advanced_help_requested",
    "mark_advanced",
]

import argparse
import sys
from typing import List, Optional

#: The top-level flag users pass to unlock advanced help.
#:
#: Unlike most CLI flags, this one is detected by inspecting
#: ``sys.argv`` before argparse runs — it must be consumed so that
#: subcommands (which may also define the same flag name) don't see
#: it in their own namespaces.
ADVANCED_FLAG = "--advanced"


def is_advanced_help_requested(argv: Optional[List[str]] = None) -> bool:
    """Return ``True`` when the user is asking for the full help surface.

    The check is a simple scan of *argv* (defaulting to ``sys.argv[1:]``)
    because we need to make the decision before argparse constructs
    namespaces.  Matches ``--advanced`` as a standalone token only —
    values like ``--advanced-something`` are not matched.
    """
    tokens = argv if argv is not None else sys.argv[1:]
    return ADVANCED_FLAG in tokens


def mark_advanced(action: argparse.Action) -> argparse.Action:
    """Mark a parser argument as advanced.

    When ``--advanced`` is NOT present in ``sys.argv``, the argument's
    help text is suppressed so ``--help`` hides it.  The argument still
    works at the command line — users who know the flag name can use
    it without passing ``--advanced``.

    Pass-through return so callers can chain the call onto
    ``parser.add_argument(...)`` in a single expression::

        from fluid_build.cli.help_advanced import mark_advanced

        mark_advanced(parser.add_argument("--industry", help="..."))
    """
    if not is_advanced_help_requested():
        action.help = argparse.SUPPRESS
    return action
