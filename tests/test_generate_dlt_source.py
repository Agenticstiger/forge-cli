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

"""Tests for the ``generate_dlt_source`` copilot tool (Phase 1.7).

The tool ships an LLM-native path for SDP custom sources: when the
user describes an external API that no off-the-shelf dlt source
covers, the copilot calls ``generate_dlt_source`` to drop a
``sources/<name>.py`` module that uses the dlt framework. The
contract's build block then references the module via
``builds[].properties.source.connection.module``.

This file pins:

1. **Generated module is import-clean** — `ast.parse` must succeed.
2. **Generated module declares a `@dlt.source` function** with the
   expected name (`<name>_source`).
3. **Auth secret is referenced via env var** — never inline.
4. **Module path stays under the workspace root** — path-escape
   attempts are rejected with a typed error.
5. **Invalid name / api_url surface typed errors** — never silent
   failures.
6. **Each ``auth_kind`` variant produces the right header** — bearer,
   api_key, basic, none.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fluid_build.cli.forge_copilot_tools import _dispatch_generate_dlt_source

# ---------------------------------------------------------------------------
# Behaviour 1+2 — generated module is valid Python with a dlt source
# ---------------------------------------------------------------------------


def _generate(
    tmp_path: Path,
    *,
    name: str = "stripe_prices",
    api_url: str = "https://api.stripe.com/v1/prices",
    auth_kind: str = "bearer",
    description: str = "Daily Stripe pricing snapshots.",
):
    """Run the dispatcher; return (result_dict, written_file_path).

    ``_dispatch_generate_dlt_source`` is a ``ForgeTool`` instance after
    the ``@forge_tool`` decoration; we exercise it through ``dispatch``
    so the args-validation path is in scope.
    """
    arguments = {
        "name": name,
        "api_url": api_url,
        "description": description,
        "auth_kind": auth_kind,
    }
    dispatch_result = _dispatch_generate_dlt_source.dispatch(arguments, workspace_root=tmp_path)
    if not dispatch_result.ok:
        # Surface the dispatcher's typed failure as a dict that mirrors
        # what the legacy callers expect — keeps the tests readable.
        return (
            {"error": dispatch_result.error_type, "message": dispatch_result.error_message},
            tmp_path / "sources" / f"{name}.py",
        )
    return dispatch_result.value, tmp_path / "sources" / f"{name}.py"


def test_generated_module_parses_as_valid_python(tmp_path):
    result, written = _generate(tmp_path)

    assert "error" not in result
    assert written.exists()

    body = written.read_text(encoding="utf-8")
    # Python parsers fail loudly on malformed code; pin import-cleanliness.
    ast.parse(body)


def test_generated_module_declares_dlt_source_function(tmp_path):
    result, written = _generate(tmp_path, name="github_events")

    assert result["function_name"] == "github_events_source"
    body = written.read_text(encoding="utf-8")
    # The dlt framework recognises @dlt.source-decorated functions.
    assert "@dlt.source" in body
    assert "def github_events_source" in body


# ---------------------------------------------------------------------------
# Behaviour 3 — auth secret never inlined
# ---------------------------------------------------------------------------


def test_auth_secret_is_env_var_reference_only(tmp_path):
    result, written = _generate(tmp_path, name="my_api", auth_kind="bearer")

    assert result["auth_env_var"] == "MY_API_TOKEN"
    body = written.read_text(encoding="utf-8")
    # The env var name appears; the actual secret value does not.
    assert "MY_API_TOKEN" in body
    assert "os.environ.get" in body
    # Negative: the body must NOT carry inline secret material like a
    # bare token string. Anything resembling "Bearer <hex>" would be a
    # critical regression.
    assert "Bearer abc" not in body
    assert "Bearer 1234" not in body


# ---------------------------------------------------------------------------
# Behaviour 4 — path-escape rejection
# ---------------------------------------------------------------------------


def test_path_escape_attempt_is_rejected(tmp_path):
    """A name that resolves outside ``<workspace>/sources/`` must not
    write anywhere. Names containing path separators are sanitised to
    underscores by the dispatcher, so they CAN'T escape."""
    result, _written = _generate(
        tmp_path,
        name="../../etc/passwd",
        api_url="https://api.example.com",
        auth_kind="none",
    )

    # Either the dispatcher sanitises the name (preferred) or rejects
    # with PathEscape. Both are safe — neither writes outside the
    # workspace.
    if "error" in result:
        assert result["error"] in ("PathEscape", "InvalidName")
    else:
        # Sanitisation kicked in; ensure the file landed under tmp_path.
        rel = result["module_path"]
        target = (tmp_path / rel).resolve()
        target.relative_to(tmp_path.resolve())  # raises if escape


# ---------------------------------------------------------------------------
# Behaviour 5 — typed errors for invalid input
# ---------------------------------------------------------------------------


def test_empty_name_returns_typed_error(tmp_path):
    result, _written = _generate(tmp_path, name="!@#$%", api_url="https://x.com")
    assert result.get("error") == "InvalidName"


def test_non_https_api_url_returns_typed_error(tmp_path):
    result, _written = _generate(tmp_path, name="ok", api_url="ftp://files.example.com")
    assert result.get("error") == "InvalidApiUrl"


# ---------------------------------------------------------------------------
# Behaviour 6 — each auth_kind variant emits the right header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth_kind, expected_header_marker",
    [
        ("bearer", "Bearer"),
        ("api_key", "X-API-Key"),
        ("basic", "Basic"),
    ],
)
def test_auth_kind_emits_correct_header(tmp_path, auth_kind, expected_header_marker):
    _result, written = _generate(tmp_path, name=f"src_{auth_kind}", auth_kind=auth_kind)
    body = written.read_text(encoding="utf-8")
    assert (
        expected_header_marker in body
    ), f"auth_kind={auth_kind!r} body missing {expected_header_marker!r}: {body[:300]}"


def test_auth_kind_none_omits_authorization_header(tmp_path):
    _result, written = _generate(tmp_path, name="public_src", auth_kind="none")
    body = written.read_text(encoding="utf-8")
    # No auth means no Authorization / X-API-Key headers.
    assert "Authorization" not in body
    assert "X-API-Key" not in body


def test_unknown_auth_kind_falls_back_to_bearer(tmp_path):
    """Defensive: an LLM-emitted typo shouldn't write an unsupported
    auth shape; the dispatcher falls back to bearer (the safest default)."""
    _result, written = _generate(tmp_path, name="weird_src", auth_kind="custom_alien_protocol")
    body = written.read_text(encoding="utf-8")
    assert "Bearer" in body


# ---------------------------------------------------------------------------
# Behaviour 7 — preview returned to the caller
# ---------------------------------------------------------------------------


def test_preview_is_capped_at_a_sensible_length(tmp_path):
    """The dispatcher returns a ``preview`` string for the LLM; it
    must be capped so a 60K-line module doesn't blow the prompt
    budget."""
    result, _written = _generate(tmp_path)
    assert "preview" in result
    # 1500 chars is the stated cap; allow a small overshoot for the
    # truncation marker.
    assert len(result["preview"]) <= 1700


def test_module_path_is_relative_to_workspace_root(tmp_path):
    result, _written = _generate(tmp_path, name="orders_api")
    assert result["module_path"] == "sources/orders_api.py"
