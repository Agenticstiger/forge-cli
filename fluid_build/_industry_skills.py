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

"""Industry-skills YAML loader (tier-0 shared leaf).

The single function :func:`load_industry_skills` reads one bundled industry
skills file (``<name>.yaml``) from the ``cli/industry_skills`` package-data
directory. It is the one piece of that package that ``copilot.industry.compiler``
needs, so it lives here — below both ``cli`` and ``copilot`` — to keep
``copilot`` free of the ``cli`` import that the ``[tool.importlinter]``
contracts forbid.

The industry YAML files stay as ``cli`` package data (next to the fuller
``cli.industry_skills`` API — ``load_tools`` / ``list_industries`` /
``generate_skills_file`` — that only the CLI uses); this leaf resolves that
directory by a package-relative path, a *data-file* reference rather than a code
import of ``cli``. ``cli.industry_skills`` re-exports this function so its own
callers and the existing tests keep resolving it unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

# The industry skills YAML lives under ``cli/industry_skills`` (bundled package
# data). ``fluid_build/`` is this module's parent, so the directory is located
# by a package-relative path — a data lookup, not a ``cli`` code import.
_SKILLS_DIR: Path = Path(__file__).with_name("cli") / "industry_skills"


def load_industry_skills(name: str) -> Dict[str, Any]:
    """Load an industry-specific skills file by name (e.g. ``telco``).

    Returns the raw YAML dict (without the tools section — that is merged
    separately via ``cli.industry_skills.generate_skills_file``).

    Raises ``FileNotFoundError`` if the industry YAML does not exist.
    """
    import yaml

    path = _SKILLS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No industry skills file for '{name}' at {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


__all__ = ["load_industry_skills"]
