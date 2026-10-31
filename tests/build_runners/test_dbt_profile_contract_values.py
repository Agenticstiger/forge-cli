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

"""The dbt runner's own profile never lets a contract choose where the
environment's credential goes (Trello Gy3LwiTB, the #758 class).

``_build_generated_dbt_profile`` pairs DATABRICKS_TOKEN / REDSHIFT_PASSWORD /
PGPASSWORD with the profile's host, so the host comes from the environment
only.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from fluid_build.build_runners.dbt import profiles
from fluid_build.build_runners.dbt.profiles import (
    DbtProfileError,
    _build_generated_dbt_profile,
    _create_temp_dbt_profiles_dir,
)

pytestmark = pytest.mark.unit

#: Fake credentials. Each test asserts they never leave the environment.
SECRET = "SHOULD-NOT-LEAVE-7f3a"  # pragma: allowlist secret

#: platform -> (host variable, credential variable, the profile key carrying it)
PLATFORMS = {
    "databricks": ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "token"),
    "redshift": ("REDSHIFT_HOST", "REDSHIFT_PASSWORD", "password"),
    "aws": ("REDSHIFT_HOST", "REDSHIFT_PASSWORD", "password"),
    "postgres": ("PGHOST", "PGPASSWORD", "password"),
    "postgresql": ("PGHOST", "PGPASSWORD", "password"),
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("DATABRICKS_", "REDSHIFT_", "PG", "DBT_", "AWS_", "DBX_")):
            monkeypatch.delenv(name, raising=False)


def _build(platform: str, **resources: Any) -> Dict[str, Any]:
    return {
        "id": "transform",
        "engine": "dbt",
        "execution": {
            "trigger": {"type": "manual"},
            "runtime": {"platform": platform, "resources": resources},
        },
    }


def _output(profile: Dict[str, Any]) -> Dict[str, Any]:
    (only,) = profile.values()
    (output,) = only["outputs"].values()
    return output


def _shown(exc: DbtProfileError) -> str:
    """Everything a refusal shows: its text, its parts and its JSON form."""
    return " ".join([str(exc), exc.what, exc.why, exc.fix, exc.as_json()])


class TestContractHost:
    @pytest.mark.parametrize("platform", sorted(PLATFORMS))
    def test_a_contract_host_never_meets_the_environment_credential(
        self, platform: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_env, credential_env, _ = PLATFORMS[platform]
        monkeypatch.setenv(host_env, "operator.example.com")
        monkeypatch.setenv(credential_env, SECRET)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build(platform, host="evil.example.com"), {})

        exc = caught.value
        assert exc.code == "DbtProfileError"
        assert "build 'transform'" in exc.what
        assert "execution.runtime.resources.host" in exc.what
        assert host_env in exc.why and host_env in exc.fix
        assert exc.extras == {"field": "execution.runtime.resources.host", "variable": host_env}
        assert SECRET not in _shown(exc)

    @pytest.mark.parametrize("platform", sorted(PLATFORMS))
    def test_the_host_comes_from_the_environment(
        self, platform: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host_env, credential_env, key = PLATFORMS[platform]
        monkeypatch.setenv(host_env, "operator.example.com")
        monkeypatch.setenv(credential_env, SECRET)

        output = _output(_build_generated_dbt_profile(_build(platform, schema="analytics"), {}))

        assert (output["host"], output[key]) == ("operator.example.com", SECRET)

    @pytest.mark.parametrize(
        "host",
        [
            # Checked as written: a placeholder is a host the contract chose,
            # even one naming the operator's own variable.
            "{{ env.DATABRICKS_HOST }}",
            "{{ env.SOME_OTHER_HOST }}",
            "https://evil.example.com",
            42,
        ],
    )
    def test_every_contract_host_is_refused(
        self, host: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABRICKS_HOST", "operator.example.com")
        monkeypatch.setenv("SOME_OTHER_HOST", "evil.example.com")
        monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)

        with pytest.raises(DbtProfileError):
            _build_generated_dbt_profile(_build("databricks", host=host), {})

    @pytest.mark.parametrize("host", [None, "", "   "])
    def test_an_empty_host_is_not_a_contract_host(
        self, host: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PGHOST", "operator.example.com")

        output = _output(_build_generated_dbt_profile(_build("postgres", host=host), {}))

        assert output["host"] == "operator.example.com"

    def test_redshift_iam_credentials_get_no_contract_host_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDSHIFT_USE_IAM", "1")

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(
                _build("redshift", host="evil.example.com", cluster_id="prod"), {}
            )

        assert "IAM credentials" in caught.value.why
        assert "Redshift Serverless" in caught.value.why

    def test_the_serverless_endpoint_is_still_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No REDSHIFT_HOST: the endpoint of the contract's own workgroup, always
        # under the AWS zone, as before.
        monkeypatch.setenv("REDSHIFT_USE_IAM", "1")
        build = _build("redshift", workgroup="wg", account_id="123456789012", region="eu-west-1")

        output = _output(_build_generated_dbt_profile(build, {}))

        assert output["host"] == "wg.123456789012.eu-west-1.redshift-serverless.amazonaws.com"

    def test_a_refused_profile_is_never_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)

        def _tempdir(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a refused profile must not reach the disk")

        monkeypatch.setattr(profiles.tempfile, "TemporaryDirectory", _tempdir)

        with pytest.raises(DbtProfileError):
            _create_temp_dbt_profiles_dir(_build("databricks", host="evil.example.com"), {})


class TestRunner:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_the_build_is_refused_before_dbt_starts(
        self,
        dry_run: bool,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from fluid_build.build_runners.dbt import runner

        (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n", encoding="utf-8")
        monkeypatch.setenv("DATABRICKS_HOST", "operator.example.com")
        monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)

        def _must_not_run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("dbt must not be prepared or run")

        monkeypatch.setattr(runner, "build_dbt_command", _must_not_run)
        monkeypatch.setattr(runner.subprocess, "run", _must_not_run)

        result = runner.execute_dbt_build(
            _build("databricks", host="evil.example.com"), tmp_path, tmp_path, dry_run=dry_run
        )

        shown = capsys.readouterr()
        assert result == 1
        assert "names its own Databricks host" in shown.out
        assert "set DATABRICKS_HOST" in shown.out
        assert "dbt_profile_refused build_id=transform code=DbtProfileError" in caplog.text
        assert SECRET not in shown.out + shown.err + caplog.text
