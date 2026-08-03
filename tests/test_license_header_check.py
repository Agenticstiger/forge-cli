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

"""Tests for the CI license-header checker."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

# The complete Apache-2.0 boilerplate the checker requires. Kept literal
# (rather than imported from the writer) so this file stays a black-box
# test of check_license_headers.py.
_COMPLETE_HEADER = (
    "# Copyright 2024-2026 Agentics Transformation Ltd\n"
    "#\n"
    '# Licensed under the Apache License, Version 2.0 (the "License");\n'
    "# you may not use this file except in compliance with the License.\n"
    "# You may obtain a copy of the License at\n"
    "#\n"
    "#     http://www.apache.org/licenses/LICENSE-2.0\n"
    "#\n"
    "# Unless required by applicable law or agreed to in writing, software\n"
    '# distributed under the License is distributed on an "AS IS" BASIS,\n'
    "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
    "# See the License for the specific language governing permissions and\n"
    "# limitations under the License.\n"
    "\n"
)


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "check_license_headers.py"
    spec = spec_from_file_location("check_license_headers", module_path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_files_requiring_headers_include_scripts_and_tools_but_exclude_examples(tmp_path):
    module = _load_module()
    paths = [
        tmp_path / "fluid_build" / "cli.py",
        tmp_path / "tests" / "test_cli.py",
        tmp_path / "scripts" / "check.py",
        tmp_path / "tools" / "bootstrap.py",
        tmp_path / "examples" / "demo.py",
    ]
    required = [
        path.relative_to(tmp_path).as_posix()
        for path in module.files_requiring_headers(paths, tmp_path)
    ]
    assert required == [
        "fluid_build/cli.py",
        "tests/test_cli.py",
        "scripts/check.py",
        "tools/bootstrap.py",
    ]


def test_tracked_python_files_uses_git_ls_files(monkeypatch, tmp_path):
    module = _load_module()

    class _CompletedProcess:
        stdout = "scripts/check.py\nexamples/demo.py\n"

    def _fake_run(command, cwd, capture_output, text, check):
        assert command == ["git", "ls-files", "*.py"]
        assert cwd == tmp_path
        assert capture_output is True
        assert text is True
        assert check is True
        return _CompletedProcess()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    tracked = module.tracked_python_files(tmp_path)
    assert [path.relative_to(tmp_path).as_posix() for path in tracked] == [
        "scripts/check.py",
        "examples/demo.py",
    ]


def test_find_missing_headers_returns_repo_relative_paths(tmp_path):
    module = _load_module()
    good = tmp_path / "scripts" / "good.py"
    bad = tmp_path / "tools" / "bad.py"
    good.parent.mkdir(parents=True, exist_ok=True)
    bad.parent.mkdir(parents=True, exist_ok=True)
    # A bare copyright line is NOT a valid header any more — the checker
    # validates the whole Apache-2.0 boilerplate (that is what let 346
    # truncated headers pass unnoticed). This test is about the shape of
    # the returned paths, so give the "good" file a real header.
    good.write_text(_COMPLETE_HEADER + "print('ok')\n")
    bad.write_text("print('missing')\n")

    missing = module.find_missing_headers([good, bad], tmp_path)
    assert missing == ["tools/bad.py"]
