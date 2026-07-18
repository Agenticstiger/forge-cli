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

"""Direnv-style trust pinning — fail-closed content-hash approval."""

from __future__ import annotations

import json

import pytest

from fluid_build.copilot.missions.spec import (
    load_builtin_mission_spec,
    load_mission_spec_from_path,
)
from fluid_build.copilot.missions.trust import (
    MissionTrustError,
    is_trusted,
    require_trusted,
    spec_trust_status,
    trust_file_path,
    trust_spec,
)

pytestmark = pytest.mark.unit

SPEC = """\
name: team-mission
description: Workspace mission.
goal: Do it.
success_criteria:
  - check: validate
"""


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Isolated user-home + workspace cwd."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("FLUID_USER_HOME", str(home))
    monkeypatch.chdir(workspace)
    return workspace


def _workspace_spec(workspace, text=SPEC):
    missions = workspace / ".fluid" / "missions"
    missions.mkdir(parents=True, exist_ok=True)
    path = missions / "team_mission.yaml"
    path.write_text(text, encoding="utf-8")
    return load_mission_spec_from_path(path)


def test_builtin_specs_are_implicitly_trusted(isolated):
    spec = load_builtin_mission_spec("gdpr-clean")
    assert spec_trust_status(spec) == "builtin"
    assert is_trusted(spec)
    assert require_trusted(spec) == "builtin"
    assert trust_spec(spec)["status"] == "builtin"  # no-op, nothing pinned
    assert not trust_file_path().exists()


def test_user_global_specs_are_implicitly_trusted(isolated, monkeypatch):
    from fluid_build.paths import user_home

    global_dir = user_home() / "missions"
    global_dir.mkdir(parents=True)
    path = global_dir / "mine.yaml"
    path.write_text(SPEC.replace("team-mission", "mine"), encoding="utf-8")
    spec = load_mission_spec_from_path(path)
    assert spec_trust_status(spec) == "user_global"
    assert require_trusted(spec) == "user_global"


def test_workspace_spec_full_lifecycle(isolated, caplog):
    spec = _workspace_spec(isolated)

    # 1) Unseen spec: refused, fail closed, structured event.
    assert spec_trust_status(spec) == "untrusted"
    assert not is_trusted(spec)
    with caplog.at_level("WARNING", logger="fluid.copilot.missions.trust"):
        with pytest.raises(MissionTrustError, match="fluid mission trust") as excinfo:
            require_trusted(spec)
    assert excinfo.value.status == "untrusted"
    assert excinfo.value.spec_path == spec.source_path
    assert any(r.message == "mission_untrusted_spec_refused" for r in caplog.records)

    # 2) Explicit approval pins the content hash.
    record = trust_spec(spec)
    assert record["status"] == "pinned"
    assert record["sha256"] == spec.content_sha256
    assert spec_trust_status(spec) == "pinned"
    assert require_trusted(spec) == "pinned"
    stored = json.loads(trust_file_path().read_text(encoding="utf-8"))
    assert stored["trusted"][str(spec.source_path)]["sha256"] == spec.content_sha256

    # 3) A changed file requires re-approval (direnv semantics).
    changed = _workspace_spec(isolated, SPEC + "plan_hint: [tweak]\n")
    assert spec_trust_status(changed) == "changed"
    with pytest.raises(MissionTrustError, match="CHANGED") as excinfo:
        require_trusted(changed)
    assert excinfo.value.status == "changed"

    # 4) Re-trusting the new content restores access.
    trust_spec(changed)
    assert require_trusted(changed) == "pinned"


def test_arbitrary_path_outside_workspace_needs_pinning(isolated, tmp_path):
    path = tmp_path / "elsewhere.yaml"
    path.write_text(SPEC, encoding="utf-8")
    spec = load_mission_spec_from_path(path)
    assert spec_trust_status(spec) == "untrusted"
    trust_spec(spec)
    assert spec_trust_status(spec) == "pinned"


def test_malformed_trust_db_trusts_nothing(isolated):
    spec = _workspace_spec(isolated)
    trust_spec(spec)
    trust_file_path().write_text("not json", encoding="utf-8")
    assert spec_trust_status(spec) == "untrusted"
    with pytest.raises(MissionTrustError):
        require_trusted(spec)


def test_trust_db_written_atomically_with_tight_perms(isolated):
    spec = _workspace_spec(isolated)
    trust_spec(spec)
    db_path = trust_file_path()
    assert db_path.is_file()
    mode = db_path.stat().st_mode & 0o777
    assert mode == 0o600
    # No temp-file droppings left behind.
    leftovers = [p for p in db_path.parent.iterdir() if p.name.startswith(".mission_trust-")]
    assert leftovers == []
