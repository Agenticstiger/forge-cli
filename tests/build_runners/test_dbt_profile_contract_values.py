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
PGPASSWORD with the profile's host, and dbt renders every profile value as
Jinja. So the host comes from the environment only, and a Databricks catalog
or schema from the contract must be a name, never a way to read a credential.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any, Dict

import pytest
import yaml

from fluid_build.build_runners.dbt import profiles
from fluid_build.build_runners.dbt.profiles import (
    DbtProfileError,
    _build_generated_dbt_profile,
    _create_temp_dbt_profiles_dir,
)

pytestmark = pytest.mark.unit

#: Fake credentials. Each test asserts they never leave the environment.
SECRET = "SHOULD-NOT-LEAVE-7f3a"  # pragma: allowlist secret
#: Identifier-shaped, so a check that read only the emitted value would pass it.
IDENT_SECRET = "dapiSHOULDNOTLEAVE"  # pragma: allowlist secret

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
    # The credential-named ones too: the value check scans the whole environment.
    for name in list(os.environ):
        if name.startswith(
            ("DATABRICKS_", "REDSHIFT_", "PG", "DBT_", "AWS_", "DBX_")
        ) or profiles._is_credential_name(name):
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


#: (contract value, environment, what the profile carries, what dbt reads it as
#: with DBX_CATALOG=lake and DBX_UNSET_CATALOG unset)
ALLOWED_NAMES = [
    ("main", {}, "main", "main"),
    ("{{ env.DBX_CATALOG }}", {"DBX_CATALOG": "lake"}, "lake", "lake"),
    ("lake_{{ env.STAGE }}", {"STAGE": "dev"}, "lake_dev", "lake_dev"),
    ("{{ env_var('DBX_CATALOG') }}", {}, "{{ env_var('DBX_CATALOG') }}", "lake"),
    (
        "{{ env_var('DBX_CATALOG', 'main') }}",
        {},
        "{{ env_var('DBX_CATALOG', 'main') }}",
        "lake",
    ),
    (
        "{{ env_var('DBX_UNSET_CATALOG', 'main') }}",
        {},
        "{{ env_var('DBX_UNSET_CATALOG', 'main') }}",
        "main",
    ),
    ("{{env_var('DBX_CATALOG')}}", {}, "{{env_var('DBX_CATALOG')}}", "lake"),
]

#: (contract value, environment); every one is refused.
REFUSED_NAMES = [
    # The contract loader leaves a credential-shaped placeholder literal, and the
    # runner used to resolve it into the profile anyway.
    ("{{ env.DATABRICKS_TOKEN }}", {}),
    ("{{ env_var('DATABRICKS_TOKEN') }}", {}),
    ("{{ env_var('DB_PASSWORD') }}", {}),
    # Known bypass 1 (Phase 1 review): a placeholder inside an env_var default.
    ("{{ env_var('DATABRICKS_SCHEMA', '{{ env.DATABRICKS_TOKEN }}') }}", {}),
    # Known bypass 2: Jinja reads '\' as an escaped quote, so the call runs on
    # past where a pattern would end it, and reads the token.
    ("{{ env_var('DATABRICKS_SCHEMA', 'x\\') }}') ~ env_var('DATABRICKS_TOKEN') ~ ('') }}", {}),
    ("{{ env_var('DBX_CATALOG', '$DATABRICKS_TOKEN') }}", {}),
    ("{{ env_var('DATABRICKS_TOKEN', 'a{{ env.UNSET_PART }}b') }}", {}),
    # A placeholder may name the variable env_var reads.
    ("{{ env_var('{{ env.WHICH }}') }}", {"WHICH": "DATABRICKS_TOKEN"}),
    # Credentials whose names the redactor's rule misses; the dotenv policy's
    # suffix rule (_PAT, _KEY, _PASS ...) names them.
    ("{{ env_var('DATABRICKS_PAT') }}", {"DATABRICKS_PAT": SECRET}),
    ("{{ env_var('FLUID_ENCRYPTION_KEY') }}", {"FLUID_ENCRYPTION_KEY": SECRET}),
    ("{{ env.DB_PASS }}", {"DB_PASS": IDENT_SECRET}),
    # Credentials no name rule knows: env_var may read only a variable named
    # for a catalog or schema, even one holding an identifier-shaped password.
    (
        "{{ env_var('GCP_SERVICE_ACCOUNT_JSON') }}",
        {"GCP_SERVICE_ACCOUNT_JSON": '{"private_key": "' + SECRET + '"}'},
    ),
    ("{{ env_var('DATABASE_URL') }}", {"DATABASE_URL": f"postgres://u:{SECRET}@db.invalid/app"}),
    ("{{ env_var('PGPASS') }}", {"PGPASS": IDENT_SECRET}),
    ("{{ env_var('STAGE', 'main') }}", {}),
    # A catalog or schema variable must hold a name.
    ("{{ env_var('DBX_CATALOG') }}", {"DBX_CATALOG": "my-catalog"}),
    ("{{ env_var('LEAKY_CATALOG') }}", {"LEAKY_CATALOG": '{"private_key": "' + SECRET + '"}'}),
    # Resolved before the runner saw it (a plan): no name left, only the value.
    (IDENT_SECRET, {"DATABRICKS_PAT": IDENT_SECRET}),
    # A containerized dbt that is not passed DBX_CATALOG falls back to the default.
    (
        "{{ env_var('DBX_CATALOG', '" + IDENT_SECRET + "') }}",
        {"DBX_CATALOG": "lake", "MY_PAT": IDENT_SECRET},
    ),
    # A variable may resolve to Jinja of its own (a CI branch name).
    ("{{ env.BRANCH }}", {"BRANCH": "{{env_var('DATABRICKS_TOKEN')}}"}),
    ("{{ env.BRANCH }}", {"BRANCH": "main') }}{{ env_var('DATABRICKS_TOKEN') }}"}),
    # Any other Jinja, or a value that is not a plain identifier.
    ("${DATABRICKS_TOKEN}", {}),
    ('{{ env_var("DATABRICKS_" ~ "TOKEN") }}', {}),
    ('{{ env_var ("DATABRICKS_TOKEN") }}', {}),
    ('{% set n = "DATABRICKS_TOKEN" %}{{ env_var(n) }}', {}),
    ("{# a comment #}main", {}),
    ("main_{{ env_var('DBX_CATALOG') }}", {}),
    ("{{ env_var('DBX_CATALOG') }}{{ env_var('DATABRICKS_TOKEN') }}", {}),
    ("my-catalog", {}),
    ("main; DROP SCHEMA x", {}),
    ("1st_catalog", {}),
]


class TestDatabricksNames:
    @pytest.mark.parametrize("key", ["catalog", "schema"])
    @pytest.mark.parametrize("value,env,emitted", [row[:3] for row in ALLOWED_NAMES])
    def test_the_allowed_forms_reach_the_profile(
        self,
        key: str,
        value: str,
        env: Dict[str, str],
        emitted: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name, setting in env.items():
            monkeypatch.setenv(name, setting)

        output = _output(_build_generated_dbt_profile(_build("databricks", **{key: value}), {}))

        assert output[key] == emitted

    @pytest.mark.parametrize("key", ["catalog", "schema"])
    @pytest.mark.parametrize("value,env", REFUSED_NAMES)
    def test_a_credential_never_becomes_the_catalog_or_schema(
        self, key: str, value: str, env: Dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)
        monkeypatch.setenv("DB_PASSWORD", SECRET)
        for name, setting in env.items():
            monkeypatch.setenv(name, setting)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", **{key: value}), {})

        exc = caught.value
        assert f"execution.runtime.resources.{key}" in exc.what
        assert f"DATABRICKS_{key.upper()}" in exc.fix
        assert SECRET not in _shown(exc)
        assert IDENT_SECRET not in _shown(exc)

    def test_the_written_value_is_checked_not_only_the_emitted_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Resolved, the default is an identifier-shaped token and the emitted
        # value is a well-formed env_var call. The token is shorter than the
        # floor the value check looks for, so only the placeholder as written
        # shows which variable it read.
        short = "tok5x"  # pragma: allowlist secret
        monkeypatch.setenv("DATABRICKS_TOKEN", short)
        value = "{{ env_var('DBX_SCHEMA', '{{ env.DATABRICKS_TOKEN }}') }}"

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", schema=value), {})

        assert "DATABRICKS_TOKEN" in caught.value.what
        assert caught.value.extras["variables"] == ["DATABRICKS_TOKEN"]
        assert short not in _shown(caught.value)

    def test_a_name_resolved_before_the_runner_is_checked_by_its_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``fluid apply plan.json`` resolves ``{{ env.DATABRICKS_PAT }}`` before
        # the runner runs: what arrives is the token, a valid identifier.
        monkeypatch.setenv("DATABRICKS_PAT", IDENT_SECRET)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", catalog=IDENT_SECRET), {})

        exc = caught.value
        assert exc.extras["variables"] == ["DATABRICKS_PAT"]
        assert "holds the value of DATABRICKS_PAT" in exc.what
        assert exc.fix.startswith("Read the catalog from a variable named for it")
        assert IDENT_SECRET not in _shown(exc)

    @pytest.mark.parametrize("value", ["{{ env.AUTH_SCHEMA }}", "{{ env_var('AUTH_SCHEMA') }}"])
    def test_a_name_refused_as_a_credential_is_told_to_rename_it(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The redactor's rule (and so the contract loader's) reads ``auth`` as a
        # credential. AUTH_SCHEMA already ends in _SCHEMA, so the way out is a
        # rename, not a variable "named for it".
        monkeypatch.setenv("AUTH_SCHEMA", "auth_raw")

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", schema=value), {})

        exc = caught.value
        assert "reads AUTH_SCHEMA, which looks like a credential" in exc.what
        assert "rename it outside the password/secret/token/key/auth family" in exc.fix
        assert exc.extras["variables"] == ["AUTH_SCHEMA"]

    @pytest.mark.parametrize("padding", ["\n", " ", "\r\n", "\t"])
    def test_a_padded_credential_is_matched_stripped(
        self, padding: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A secret mounted from a file keeps its newline in the environment; the
        # plan loader strips what it resolves, so the name holds it without one.
        monkeypatch.setenv("DATABRICKS_PAT", IDENT_SECRET + padding)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", catalog=IDENT_SECRET), {})

        assert caught.value.extras["variables"] == ["DATABRICKS_PAT"]

    @pytest.mark.parametrize(
        "guesses",
        [
            # Separated guesses are no name: refused before any value is looked for.
            "hunter2pw Summer2026",
            "hunter2pw,Summer2026",
            # Run together they are one, past Unity Catalog's 255 characters.
            "a" * 250 + "Summer2026",
            # Or packed into the name of the variable an env_var reads, which the
            # scan reads with the rest of the call.
            "{{ env_var('" + "g" * 250 + "_Summer2026_CATALOG') }}",
            "{{ env_var('DBX_CATALOG', '" + "g" * 250 + "Summer2026') }}",
        ],
    )
    def test_a_string_of_guesses_learns_nothing_from_the_refusal(
        self, guesses: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDSHIFT_PASSWORD", "Summer2026")

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", catalog=guesses), {})

        assert "is not a Databricks name" in caught.value.what
        assert "REDSHIFT_PASSWORD" not in _shown(caught.value)

    def test_a_name_may_be_as_long_as_unity_catalog_allows(self) -> None:
        longest = "c" * 255

        output = _output(_build_generated_dbt_profile(_build("databricks", catalog=longest), {}))

        assert output["catalog"] == longest
        with pytest.raises(DbtProfileError):
            _build_generated_dbt_profile(_build("databricks", catalog=longest + "c"), {})
        default = "{{ env_var('DBX_CATALOG', '" + longest + "c') }}"
        with pytest.raises(DbtProfileError):
            _build_generated_dbt_profile(_build("databricks", catalog=default), {})

    def test_env_var_reads_only_a_catalog_or_schema_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No name rule says PGPASS holds a password, and its value is a valid
        # identifier: only the variable's name keeps it out.
        monkeypatch.setenv("PGPASS", IDENT_SECRET)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", schema="{{ env_var('PGPASS') }}"), {})

        exc = caught.value
        assert "reads PGPASS, which is not a catalog or schema variable" in exc.what
        assert "_CATALOG" in exc.fix and "DATABRICKS_SCHEMA" in exc.fix
        assert exc.extras == {
            "field": "execution.runtime.resources.schema",
            "variables": ["PGPASS"],
        }
        assert IDENT_SECRET not in _shown(exc)

    def test_a_suffix_named_credential_is_refused_by_name_when_too_short_to_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The redactor's rule misses DB_PASS, and a value under the floor is
        # not looked for, so only the suffix rule on the name refuses it.
        short = "pw123"  # pragma: allowlist secret
        monkeypatch.setenv("DB_PASS", short)

        with pytest.raises(DbtProfileError) as caught:
            _build_generated_dbt_profile(_build("databricks", schema="{{ env.DB_PASS }}"), {})

        assert caught.value.extras["variables"] == ["DB_PASS"]
        assert short not in _shown(caught.value)

    @pytest.mark.parametrize(
        "variable,value,name",
        [
            # Common in CI and developer shells; the redactor's rule reads
            # ``auth`` inside them, which is no reason to refuse a schema.
            ("GIT_AUTHOR_NAME", "airflow", "airflow_staging"),
            ("AUTHOR", "analytics_team", "analytics_team"),
            ("OAUTH_PROVIDER", "github", "github_raw"),
        ],
    )
    def test_an_ordinary_variable_holding_part_of_a_name_does_not_refuse_it(
        self, variable: str, value: str, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(variable, value)

        output = _output(_build_generated_dbt_profile(_build("databricks", schema=name), {}))

        assert output["schema"] == name

    def test_a_short_credential_value_does_not_refuse_an_ordinary_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Below the redactor's exact-match floor a value occurs inside words.
        monkeypatch.setenv("SOME_KEY", "main")

        output = _output(_build_generated_dbt_profile(_build("databricks", catalog="main"), {}))

        assert output["catalog"] == "main"

    def test_no_value_keeps_the_environment_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_CATALOG", "operator_catalog")
        # An unset placeholder resolves to nothing, as before.
        build = _build("databricks", catalog="{{ env.UNSET_CATALOG }}", schema="")

        output = _output(_build_generated_dbt_profile(build, {}))

        assert (output["catalog"], output["schema"]) == ("operator_catalog", "default")

    @pytest.mark.parametrize("value,env,rendered", [(v, e, r) for v, e, _, r in ALLOWED_NAMES])
    def test_dbt_reads_an_allowed_form_as_the_name_it_says(
        self,
        value: str,
        env: Dict[str, str],
        rendered: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The profile as dbt itself reads it: the name the contract meant, and
        the token in ``token`` only."""
        renderer = pytest.importorskip("dbt.config.renderer")
        context = pytest.importorskip("dbt_common.context")
        monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)
        monkeypatch.setenv("DBX_CATALOG", "lake")
        monkeypatch.delenv("DBX_UNSET_CATALOG", raising=False)
        for name, setting in env.items():
            monkeypatch.setenv(name, setting)

        profiles_dir, temp_dir = _create_temp_dbt_profiles_dir(
            _build("databricks", catalog=value, schema=value), {}
        )
        try:
            written = yaml.safe_load((profiles_dir / "profiles.yml").read_text(encoding="utf-8"))
        finally:
            temp_dir.cleanup()

        def _render() -> Dict[str, Any]:
            # dbt's env_var() reads its invocation context; set it in a copy of
            # this context, so no later test runs with it.
            context.set_invocation_context(os.environ)
            return renderer.ProfileRenderer({}).render_data(written)

        output = _output(contextvars.copy_context().run(_render))

        assert (output["catalog"], output["schema"]) == (rendered, rendered)
        assert output["token"] == SECRET


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
