# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
