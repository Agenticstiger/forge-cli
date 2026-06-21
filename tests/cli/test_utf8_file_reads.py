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

"""Regression test for the file-READ half of the Windows UTF-8 fix (xsdOYJ6E).

The write-side crash (the Unicode banner) was fixed in #263. This pins the
read/write-side: on Windows, ``open()`` / ``Path.read_text`` / ``Path.write_text``
without an explicit ``encoding=`` decode/encode using the locale default
(cp1252), so processing a contract with non-ASCII content (accented names,
emoji in descriptions) raised ``UnicodeDecodeError`` / ``UnicodeEncodeError``.
Every text I/O site now passes ``encoding="utf-8"``.

``PYTHONIOENCODING=cp1252`` forces a Windows-style locale even on POSIX, so
this runs on every CI platform — not just the Windows job. ``fluid plan``
reads the contract + bundled schema and writes ``plan.json`` with the
non-ASCII contract embedded, exercising both the read and write paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# A valid hello-world contract with accented text + an emoji in free-text
# fields (name/description/owner/SQL/column description) — all unencodable in
# cp1252, so they exercise the read (YAML/schema) and write (plan.json) paths.
_NON_ASCII_CONTRACT = """\
fluidVersion: "0.7.2"
kind: "DataProduct"
id: "example.cafe_munchen_v1"
name: "Café München — naïve façade 📊"
description: "Données clients résumé — für Geschäftsanalyse 🧑"
domain: "example"
metadata:
  layer: Bronze
  owner:
    team: "équipe-josé"
    email: "team@example.com"

builds:
  - id: "hello_transformation"
    pattern: "embedded-logic"
    engine: "sql"
    properties:
      sql: |
        SELECT 'Café — résumé 📊' as message

exposes:
  - exposeId: "hello_output"
    kind: "table"
    binding:
      platform: "local"
      format: "csv"
      location:
        path: "runtime/out/cafe-munchen-v1.csv"
    contract:
      schema:
        - name: "message"
          type: "string"
          description: "Le résumé du café (José) 🧑"
"""


def test_plan_on_non_ascii_contract_under_cp1252(tmp_path):
    """``fluid plan`` on a contract with non-ASCII content must exit 0 under a
    cp1252 locale — reading the contract/schema and writing plan.json no longer
    crash with a codec error."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(_NON_ASCII_CONTRACT, encoding="utf-8")
    out = tmp_path / "plan.json"

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "--provider",
            "local",
            "plan",
            str(contract),
            "--out",
            str(out),
        ],
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
    )

    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, stderr[:800]
    assert "codec can't" not in stderr and "UnicodeDecodeError" not in stderr
    # plan.json embeds the non-ASCII contract and must be valid UTF-8 on disk.
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan, "plan.json is empty"
