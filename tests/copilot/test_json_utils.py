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

import pytest

from fluid_build.copilot.utils.json import safe_json_parse


def test_safe_json_parse_plain_json():
    assert safe_json_parse('{"hello":"world"}') == {"hello": "world"}


def test_safe_json_parse_markdown_fence():
    payload = """```json
{"hello":"world"}
```"""
    assert safe_json_parse(payload) == {"hello": "world"}


def test_safe_json_parse_extracts_balanced_object():
    text = 'leading text\n{"hello":"world"}\ntrailing text'
    assert safe_json_parse(text) == {"hello": "world"}


def test_safe_json_parse_raises_when_no_json():
    with pytest.raises(Exception):
        safe_json_parse("not json at all")
