# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Importing the package must not pull the HTTP or AWS SDKs onto the ``--help`` path."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    return result.stdout.strip()


def test_package_import_does_not_import_httpx_or_boto3():
    out = _run(
        "import sys, fluid_build.schema_registry as s; s.RegistryRef; "
        "print('httpx' in sys.modules, 'boto3' in sys.modules)"
    )
    assert out == "False False"


@pytest.mark.parametrize(
    "module",
    ["reference", "schemas", "compare", "policy", "produced", "verify", "importer"],
)
def test_submodule_import_stays_light(module):
    out = _run(
        f"import sys, fluid_build.schema_registry.{module}; "
        "print('httpx' in sys.modules, 'boto3' in sys.modules, 'litellm' in sys.modules)"
    )
    assert out == "False False False"


def test_clients_module_import_is_light_until_a_call():
    out = _run(
        "import sys; from fluid_build.schema_registry.clients import ConfluentRegistry, "
        "GlueRegistry; ConfluentRegistry('https://x.example'); GlueRegistry(client=object()); "
        "print('httpx' in sys.modules, 'boto3' in sys.modules)"
    )
    assert out == "False False"


def test_public_names_resolve_lazily():
    out = _run(
        "import fluid_build.schema_registry as s\n"
        "names = [n for n in s.__all__]\n"
        "print(all(getattr(s, n) is not None for n in names), len(names) >= 15)"
    )
    assert out == "True True"


def test_unknown_attribute_raises_attribute_error():
    import fluid_build.schema_registry as s

    with pytest.raises(AttributeError):
        s.does_not_exist  # noqa: B018
    assert "RegistryRef" in dir(s)
