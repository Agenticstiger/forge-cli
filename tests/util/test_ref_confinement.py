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

"""Unit tests for the shared ``$ref`` confinement primitive."""

from __future__ import annotations

import pytest

from fluid_build.util.ref_confinement import (
    RefConfinementError,
    confine_ref,
    format_pointer,
    iter_external_refs,
)


@pytest.mark.parametrize(
    "parts, expected",
    [
        ((), ""),
        (("a", 0, "b"), "/a/0/b"),
        (("a/b", "m~n"), "/a~1b/m~0n"),
    ],
)
def test_format_pointer_is_rfc6901(parts, expected):
    assert format_pointer(parts) == expected


def test_iter_external_refs_skips_same_document_and_non_string_refs():
    doc = {
        "a": {"$ref": "#/x"},
        "b": [{"$ref": "./f.yaml"}, {"$ref": 3}],
        "c": {"d": {"$ref": "http://h/x", "extra": 1}},
    }
    assert sorted(iter_external_refs(doc)) == [("/b/0", "./f.yaml"), ("/c/d", "http://h/x")]


def test_inside_root_returns_resolved_target(tmp_path):
    (tmp_path / "sub").mkdir()
    target = confine_ref("sub/x.yaml", "sub/x.yaml", base_dir=tmp_path, root=tmp_path)
    assert target == (tmp_path / "sub" / "x.yaml").resolve()


def test_root_itself_is_inside(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    assert confine_ref("..", "..", base_dir=sub, root=tmp_path) == tmp_path.resolve()


def test_prefix_sibling_is_not_inside(tmp_path):
    """``/x/proj-evil`` shares a string prefix with ``/x/proj`` but is not
    inside it — the check is path-wise, not ``str.startswith``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        confine_ref("../proj-evil/x.yaml", "../proj-evil/x.yaml", base_dir=proj, root=proj)


def test_nul_byte_is_refused_as_typed_error(tmp_path):
    with pytest.raises(RefConfinementError):
        confine_ref("a\0b", "a\0b", base_dir=tmp_path, root=tmp_path)


def test_root_hint_is_appended_only_to_escapes(tmp_path):
    with pytest.raises(RefConfinementError, match="HINT"):
        confine_ref("../x", "../x", base_dir=tmp_path, root=tmp_path, root_hint="HINT")
    with pytest.raises(RefConfinementError) as excinfo:
        confine_ref("http://h/x", "http://h/x", base_dir=tmp_path, root=tmp_path, root_hint="HINT")
    assert "HINT" not in str(excinfo.value)
