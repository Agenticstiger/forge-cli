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

from __future__ import annotations

import builtins
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

import pytest

from fluid_build.copilot.store.backends.postgres import PostgresBackend

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_postgres_extra_declares_psycopg_binary() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    postgres_extra = pyproject["project"]["optional-dependencies"]["postgres"]

    assert any(dep.startswith("psycopg[binary]") for dep in postgres_extra)


def test_postgres_backend_missing_driver_error_names_install_extra(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("missing psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError) as exc_info:
        PostgresBackend("postgresql://user:pass@localhost:5432/fluid")

    assert "data-product-forge[postgres]" in str(exc_info.value)
