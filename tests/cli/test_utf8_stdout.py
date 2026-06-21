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

"""Regression tests for the Windows non-UTF-8 stdout crash (xsdOYJ6E).

Windows defaults non-TTY stdout/stderr to cp1252, which cannot encode the
CLI's emoji / box-drawing / pointer banner — so every command used to crash on
startup with a ``'charmap' codec can't encode characters`` ``UnicodeEncodeError``
before argparse even ran. ``_force_utf8_streams`` reconfigures the streams to
UTF-8 (errors="replace") before any output is produced.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

from fluid_build.cli import _force_utf8_streams

# A slice of the banner that cp1252 cannot encode (emoji + pointer + box-draw).
_UNENCODABLE_ON_CP1252 = "✅ ⏭️ ━━━ ⚡"


class TestForceUtf8Streams:
    def test_reconfigures_non_utf8_stream(self, monkeypatch):
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)

        _force_utf8_streams()

        assert out.encoding.lower().replace("-", "") == "utf8"
        assert err.encoding.lower().replace("-", "") == "utf8"
        assert out.errors == "replace"
        # The exact characters that used to crash now encode without raising.
        out.write(_UNENCODABLE_ON_CP1252)

    def test_leaves_utf8_stream_untouched(self, monkeypatch):
        """POSIX (already UTF-8) must be a complete no-op — neither encoding nor
        the errors mode is touched, so we don't silently relax 'strict'."""
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        prior_errors = out.errors  # default 'strict'
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))

        _force_utf8_streams()

        assert out.encoding.lower().replace("-", "") == "utf8"
        assert out.errors == prior_errors

    def test_skips_stream_without_reconfigure(self, monkeypatch):
        # io.StringIO has no reconfigure() — must be skipped, not crash.
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        _force_utf8_streams()  # no exception

    def test_swallows_reconfigure_failure(self, monkeypatch):
        """A stream that refuses reconfigure (e.g. detached buffer) must never
        be what aborts CLI startup."""

        class _Boom:
            encoding = "cp1252"

            def reconfigure(self, **kwargs):
                raise OSError("detached buffer")

        monkeypatch.setattr(sys, "stdout", _Boom())
        monkeypatch.setattr(sys, "stderr", _Boom())
        _force_utf8_streams()  # OSError swallowed


def test_cli_survives_cp1252_stdout_pipe():
    """Faithful cross-platform repro of the Windows crash.

    ``PYTHONIOENCODING=cp1252`` forces a Windows-style non-UTF-8 stdout even on
    POSIX, and ``capture_output`` makes it a non-TTY pipe. Before the fix this
    exited 2 with a ``'charmap' codec`` error before argparse ran; now it must
    exit 0 and emit valid UTF-8.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "--help"],
        capture_output=True,
        env=env,
        cwd=repo_root,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:500]
    assert b"charmap" not in proc.stderr
    # The fix reconfigured the stream, so output is valid UTF-8.
    proc.stdout.decode("utf-8")
    assert proc.stdout.strip(), "expected help output"
