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

"""The federation policy variables come from the operator, not the checkout.

``FLUID_FEDERATION_SECRET_REF_ALLOWLIST`` decides where a credential may be
sent and ``FLUID_FEDERATION_HOST_ALLOWLIST`` decides which private hosts may
be fetched. ``fluid apply`` hydrates ``os.environ`` from the project's
dotenv files (and from ``FLUID_SECRETS_FILE``) before the federation gate
runs, and those files sit in the same checkout as
``federation/upstreams.yaml``. A value that a project dotenv file supplies
is therefore ignored, with a warning that names the file, and the request
is refused exactly as if the variable were unset.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from fluid_build.cli._common import hydrate_dotenv
from fluid_build.forge import federation
from fluid_build.forge.federation import validate_federated_consumes

pytestmark = pytest.mark.unit

_PUBLIC = "fluid_build.forge.federation._hostname_is_private"
_REF_POLICY = "FLUID_FEDERATION_SECRET_REF_ALLOWLIST"
_HOST_POLICY = "FLUID_FEDERATION_HOST_ALLOWLIST"
#: Test value only. Long and distinctive so a substring check is meaningful.
_TOKEN = "fedtest-1111-not-a-real-credential-c0ffee"
#: Name of an environment variable, not a value.
_VAR = "REGISTRY_TOKEN"
_HOST = "collector.example"

_HYDRATED_KEYS = (
    _REF_POLICY,
    _HOST_POLICY,
    "FLUID_SECRETS_FILE",
    "FLUID_ENV",
    _VAR,
    "FA4_UNRELATED",
)


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    """``hydrate_dotenv`` writes ``os.environ`` directly. Registering each key
    with monkeypatch first makes teardown restore it, whatever the test sets.
    """
    for key in _HYDRATED_KEYS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)


def _project(tmp_path: Path, *, endpoint: str = f"https://{_HOST}") -> Path:
    root = tmp_path / "checkout"
    (root / "federation").mkdir(parents=True)
    (root / "federation" / "upstreams.yaml").write_text(
        "workspaces:\n"
        "  - id: ext\n"
        "    kind: http_registry\n"
        f"    endpoint: {endpoint}\n"
        "    auth:\n"
        "      mode: bearer\n"
        f"      secret_ref: {_VAR}\n",
        encoding="utf-8",
    )
    return root


_CONTRACT = {
    "consumes": [{"productId": "p", "upstreamWorkspace": "ext", "upstreamDigest": "sha256:ok"}]
}


#: How hydration treats a policy variable a project dotenv file sets: older
#: hydration copies it into ``os.environ``; hydration that protects operator
#: variables leaves it out. The refusal must name the project file either way.
_HYDRATION_MODES = ("hydrated", "protected")


def _apply_gate(root: Path, monkeypatch, *, private: bool = False, hydration: str = "hydrated"):
    """What ``fluid apply`` does: hydrate from the checkout, then run the gate."""
    monkeypatch.chdir(root)
    hydrate_dotenv(Path.cwd())
    if hydration == "protected":
        monkeypatch.delenv(_REF_POLICY, raising=False)
        monkeypatch.delenv(_HOST_POLICY, raising=False)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="sha256:ok"))
        with patch(_PUBLIC, return_value=private):
            rows = validate_federated_consumes(_CONTRACT, workspace_root=root)
    return rows, route


class TestSecretRefPolicyFromTheOperator:
    @pytest.mark.parametrize("hydration", _HYDRATION_MODES)
    @pytest.mark.parametrize("filename", [".env", ".env.dev", ".env.local"])
    def test_project_dotenv_cannot_widen_the_secret_ref_policy(
        self, filename: str, hydration: str, tmp_path: Path, monkeypatch, caplog
    ):
        root = _project(tmp_path)
        (root / filename).write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)

        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            rows, route = _apply_gate(root, monkeypatch, hydration=hydration)

        assert not route.called, "the credential must not leave the process"
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert _REF_POLICY in rows[0].reason
        assert filename in rows[0].reason, "the refusal names the file that was ignored"
        federation_log = "\n".join(
            r.getMessage() for r in caplog.records if r.name == "fluid.forge.federation"
        )
        assert filename in federation_log
        assert _TOKEN not in rows[0].reason and _TOKEN not in caplog.text

    def test_interpolated_project_value_is_ignored_too(self, tmp_path: Path, monkeypatch):
        """Split across files with ``${...}``, the value seen at hydration and
        the value seen later can differ; the key alone decides."""
        root = _project(tmp_path)
        (root / ".env").write_text(f"FA4_UNRELATED=@{_HOST}\n", encoding="utf-8")
        (root / ".env.local").write_text(
            f"{_REF_POLICY}={_VAR}${{FA4_UNRELATED}}\n", encoding="utf-8"
        )
        monkeypatch.setenv(_VAR, _TOKEN)

        rows, route = _apply_gate(root, monkeypatch)

        assert not route.called
        assert len(rows) == 1 and rows[0].kind == "unreachable"

    def test_project_dotenv_cannot_point_the_secrets_file_at_a_policy(
        self, tmp_path: Path, monkeypatch
    ):
        root = _project(tmp_path)
        (root / "federation" / "policy.txt").write_text(
            f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8"
        )
        (root / ".env").write_text("FLUID_SECRETS_FILE=federation/policy.txt\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)

        rows, route = _apply_gate(root, monkeypatch)

        assert not route.called
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert _REF_POLICY in rows[0].reason

    def test_operator_environment_still_configures_the_policy(self, tmp_path: Path, monkeypatch):
        root = _project(tmp_path)
        # A project .env that sets unrelated keys, and a template that lists
        # the policy variable with no value, do not disturb the policy.
        (root / ".env").write_text("FA4_UNRELATED=1\n", encoding="utf-8")
        (root / ".env.example").write_text(f"{_REF_POLICY}=\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)
        monkeypatch.setenv(_REF_POLICY, f"{_VAR}@{_HOST}")

        rows, route = _apply_gate(root, monkeypatch)

        assert rows == []
        assert route.calls[0].request.headers["Authorization"] == f"Bearer {_TOKEN}"

    def test_operator_secrets_file_outside_the_checkout_configures_the_policy(
        self, tmp_path: Path, monkeypatch
    ):
        root = _project(tmp_path)
        secrets = tmp_path / "operator" / "secrets.env"
        secrets.parent.mkdir()
        secrets.write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")
        monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
        monkeypatch.setenv(_VAR, _TOKEN)

        rows, route = _apply_gate(root, monkeypatch)

        assert rows == []
        assert route.called


class TestHostPolicyFromTheOperator:
    @pytest.mark.parametrize("hydration", _HYDRATION_MODES)
    def test_project_dotenv_cannot_widen_the_private_host_policy(
        self, hydration: str, tmp_path: Path, monkeypatch, caplog
    ):
        root = _project(tmp_path, endpoint="https://registry.internal.example")
        (root / "federation" / "upstreams.yaml").write_text(
            "workspaces:\n"
            "  - id: ext\n"
            "    kind: http_registry\n"
            "    endpoint: https://registry.internal.example\n",
            encoding="utf-8",
        )
        (root / ".env").write_text(f"{_HOST_POLICY}=internal.example\n", encoding="utf-8")

        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            rows, route = _apply_gate(root, monkeypatch, private=True, hydration=hydration)

        assert not route.called, "a private host may only be opened by the operator"
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        blocked = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("federation_http_fetch_ssrf_blocked:")
        ]
        assert len(blocked) == 1
        assert (
            f"{_HOST_POLICY} in project file(s) .env is ignored" in blocked[0]
        ), "the refusal names the file that was ignored"

    def test_operator_host_policy_still_opens_a_private_host(self, monkeypatch):
        monkeypatch.setenv(_HOST_POLICY, "internal.example")
        assert federation._federation_host_on_allowlist("registry.internal.example")


def _filesystem_ignores_case(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


class TestProjectFilesAreTheFilesTheStoreOpens:
    """Which files count is decided by the paths the dotenv store opens, so a
    file counts exactly when hydration would have read it."""

    def test_path_list_is_the_dotenv_store_load_order(self, tmp_path: Path, monkeypatch):
        from fluid_build.credentials import dotenv_store

        for name in (".env", ".env.staging", ".env.local"):
            (tmp_path / name).write_text("", encoding="utf-8")
        opened: list[Path] = []

        def _record(path, *args, **kwargs):
            opened.append(Path(path))
            return {}

        monkeypatch.setattr(dotenv_store, "dotenv_values", _record)
        dotenv_store.DotEnvCredentialStore(project_root=tmp_path, environment="staging").load()

        assert opened == list(federation._dotenv_store_paths(tmp_path, "staging"))

    def test_any_environment_file_counts_and_look_alikes_do_not(self, tmp_path: Path):
        for name in (".env.prod", ".envrc", ".env-old", "env.local"):
            (tmp_path / name).write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")

        with federation._policy_project_root(tmp_path):
            hits = federation._project_files_setting(_REF_POLICY)

        assert [p.name for p in hits] == [".env.prod"]

    def test_one_file_reached_by_two_spellings_is_reported_once(self, tmp_path: Path):
        (tmp_path / ".env.local").write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")

        with federation._policy_project_root(tmp_path):
            hits = federation._project_files_setting(_REF_POLICY)

        assert len(hits) == 1

    @pytest.mark.parametrize("filename", [".Env", ".ENV.local"])
    def test_differently_cased_project_file_cannot_set_the_policy(
        self, filename: str, tmp_path: Path, monkeypatch
    ):
        if not _filesystem_ignores_case(tmp_path):
            pytest.skip("needs a case-insensitive filesystem (the macOS default)")
        root = _project(tmp_path)
        (root / filename).write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)

        rows, route = _apply_gate(root, monkeypatch)

        assert not route.called, "the credential must not leave the process"
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert _REF_POLICY in rows[0].reason


class TestWhenTheIgnoredPolicyIsLogged:
    """docs/FEDERATION.md: a refused fetch names the project file, and
    ``federation_policy_env_ignored`` is logged only when the variable is also
    set in the operator's environment. Hydration that protects operator
    variables never copies a project-only value into the environment, so
    federation reads it as unset and logs nothing about it."""

    @staticmethod
    def _ignored_lines(caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("federation_policy_env_ignored:")
        ]

    def test_project_only_value_is_named_by_the_refusal_not_logged(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        root = _project(tmp_path)
        (root / ".env").write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)

        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            rows, route = _apply_gate(root, monkeypatch, hydration="protected")

        assert not route.called, "the credential must not leave the process"
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert f"{_REF_POLICY} in project file(s) .env is ignored" in rows[0].reason
        assert self._ignored_lines(caplog) == []

    def test_operator_value_beside_a_project_value_is_ignored_and_logged(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        root = _project(tmp_path)
        (root / ".env").write_text(f"{_REF_POLICY}={_VAR}@{_HOST}\n", encoding="utf-8")
        monkeypatch.setenv(_VAR, _TOKEN)
        monkeypatch.setenv(_REF_POLICY, f"{_VAR}@{_HOST}")

        with caplog.at_level("WARNING", logger="fluid.forge.federation"):
            rows, route = _apply_gate(root, monkeypatch)

        assert not route.called, "the operator's value is ignored too"
        assert len(rows) == 1 and rows[0].kind == "unreachable"
        assert f"{_REF_POLICY} in project file(s) .env is ignored" in rows[0].reason
        logged = self._ignored_lines(caplog)
        assert len(logged) == 1
        assert _REF_POLICY in logged[0] and str(root.resolve() / ".env") in logged[0]
        assert _TOKEN not in caplog.text

    def test_the_doc_says_the_log_line_needs_the_operator_value(self):
        root = Path(federation.__file__).resolve().parents[2]
        doc = re.sub(r"\s+", " ", (root / "docs" / "FEDERATION.md").read_text(encoding="utf-8"))
        assert "A refused fetch names the project file whose value was ignored" in doc
        assert (
            "logs `federation_policy_env_ignored` naming the file when it reads the "
            "variable, but only if the variable is also set in your own environment"
        ) in doc
        # The earlier wording promised the line for every project value.
        assert "the run logs `federation_policy_env_ignored` naming the file" not in doc
