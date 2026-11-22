# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Small credential-hygiene and path-scope gaps, one case per gap.

Redaction shapes are pinned on BOTH layers (``secret_redactor`` and the Snowflake provider's
``redact_string`` / ``redact_dict``), which must stay symmetric. Credential-shaped literals are
built from parts so no file holds a whole one.
"""

from __future__ import annotations

import io
import logging
import textwrap
from typing import Any
from unittest.mock import MagicMock

import pytest

from fluid_build.observability import secret_redactor as sr
from fluid_build.observability.secret_redactor import redact_secret_text, redact_value
from fluid_build.providers.snowflake.util.logging import redact_dict, redact_string

pytestmark = pytest.mark.unit

TOKEN = "0f8fad5b" + "d9cb469f" + "a1657086" + "7728950e"
GH = "gh" + "p_" + "A1b2C3d4" * 5
PW = "Pa" + "ss-w0rd-X"


@pytest.fixture()
def clean_registry():
    sr.forget_known_secrets()
    yield
    sr.forget_known_secrets()


# ── the redaction layers ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key", ["passwd", "pwd", "key", "signingKey", "signing_key", "hmac_key", "hmacKey", "sig"]
)
def test_the_short_and_signing_key_names_are_masked_by_both_layers(key: str) -> None:
    payload = {key: PW, "region": "eu"}
    for redacted in (redact_value(payload), redact_dict(payload)):
        assert PW not in str(redacted) and redacted["region"] == "eu", key


@pytest.mark.parametrize("name", ["monkey", "primary_key", "signature", "keyspace", "sigma"])
def test_a_name_that_only_holds_key_or_sig_is_not_masked(name: str) -> None:
    payload = {name: "value-kept"}
    assert redact_value(payload) == payload and redact_dict(payload) == payload


@pytest.mark.parametrize(
    "line", [f"passwd={PW}", f"hmac_key={PW}", f"signing-key={PW}", f"app_passwd: {PW}"]
)
def test_the_assignment_rule_knows_the_same_names_in_both_layers(line: str) -> None:
    assert PW not in redact_secret_text(line)
    assert PW not in redact_string(line)


def test_a_token_as_the_only_userinfo_is_masked_by_both_layers() -> None:
    line = f"git clone https://{TOKEN}@github.com/org/repo.git"
    assert TOKEN not in redact_secret_text(line)
    assert TOKEN not in redact_string(line)
    assert redact_secret_text(line).endswith("@github.com/org/repo.git")


@pytest.mark.parametrize(
    "url",
    [
        "https://alice@host.example/x",  # a user name is no secret
        "https://first.last@host.example/x",
        "https://long-service-account-name@host.example/x",  # no digit: a name
        "https://host.example/a@" + TOKEN,  # not userinfo
    ],
)
def test_a_user_name_in_a_url_stays(url: str) -> None:
    assert redact_secret_text(url) == url
    assert redact_string(url) == url


def test_a_credential_under_a_credential_key_is_masked_at_any_length(clean_registry) -> None:
    """The key vouches for the value: 1 to 5 characters are masked as 6 or more are."""
    payload = {"password": "ab", "token": "x", "api_key": "12345", "mode": "ab"}
    assert sr.register_secrets_from_payload(payload) == 3
    for out, mask in (
        (redact_secret_text("pw ab, tok x; key 12345"), "***REDACTED***"),
        (redact_string("pw ab, tok x; key 12345"), "[REDACTED]"),
    ):
        assert out == f"pw {mask}, tok {mask}; key {mask}"


def test_a_short_credential_is_masked_whole_word_and_never_inside_a_word(clean_registry) -> None:
    assert sr.register_secret("ab", credential_key=True)
    assert redact_secret_text("ab a-ab (ab) abc cab ab_c") == (
        "***REDACTED*** a-***REDACTED*** (***REDACTED***) abc cab ab_c"
    )
    assert redact_string("ab (ab) abc cab") == "[REDACTED] ([REDACTED]) abc cab"


def test_a_short_value_with_no_credential_key_stays_visible(clean_registry) -> None:
    """The length rule is for shape-based detection: no key, no mask."""
    assert sr.register_secret("ab") is False  # no credential key to vouch for it
    assert sr.collect_secret_values({"mode": "ab", "region": "eu", "key": "id-1"}) == ()
    assert redact_value({"mode": "ab"}) == {"mode": "ab"}
    assert redact_dict({"mode": "ab"}) == {"mode": "ab"}
    line = "mode=ab region=eu"
    assert redact_secret_text(line) == line and redact_string(line) == line


def test_a_short_credential_in_the_env_is_registered(clean_registry) -> None:
    assert sr.register_secrets_from_environ({"DB_PASSWORD": "pw", "REGION": "eu"}) == 1
    assert redact_secret_text("login pw ok in eu") == "login ***REDACTED*** ok in eu"
    assert redact_string("login pw ok in eu") == "login [REDACTED] ok in eu"


def test_extra_secrets_of_one_call_may_be_short() -> None:
    assert redact_secret_text("pw ab here", extra_secrets=["ab"]) == "pw ***REDACTED*** here"


def test_a_short_credential_that_is_a_key_word_is_still_refused(clean_registry) -> None:
    """``key`` as a literal would rewrite ``api-key`` in every line."""
    assert sr.register_secret("key", credential_key=True) is False
    assert redact_secret_text("api-key=value") == "api-key=***REDACTED***"


# ── the alert route's option keys ───────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "passwd",
        "pwd",
        "key",
        "signingKey",
        "hmac_key",
        "sig",
        "apiKey",
        "routing-key",
        "accesstoken",
    ],
)
def test_an_option_key_that_names_a_credential_is_refused(key: str) -> None:
    from fluid_build.alerts.routes import secret_option_key

    assert secret_option_key(key), key


@pytest.mark.parametrize(
    "key", ["max_tokens", "tokenizer", "author", "authority", "dedup_key", "monkey", "secretary"]
)
def test_an_option_key_that_only_holds_a_part_is_not(key: str) -> None:
    from fluid_build.alerts.routes import secret_option_key

    assert not secret_option_key(key), key


def test_an_alert_route_refuses_the_credential_option_and_keeps_the_harmless_one() -> None:
    from fluid_build.alerts.routes import _option_problems

    problems = _option_problems({"signingKey": "x", "max_tokens": 5, "author": "me"}, "options")
    assert len(problems) == 1 and "signingKey" in problems[0]


# ── a catalog uri's user and password ───────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "https://svc:Pa#ss@lk.example.com/catalog",
        "https://svc:Pa?ss@lk.example.com/catalog",
        "https://svc:Pa\uff20lk.example.com/catalog",  # a fullwidth @
        "\uff48ttps://svc:Pa\uff20lk.example.com/catalog",
    ],
)
def test_a_catalog_uri_whose_password_holds_a_delimiter_is_not_written(uri: str) -> None:
    from fluid_build.providers.odcs.mappers.servers import _without_userinfo

    assert _without_userinfo(uri) == ""


def test_an_at_sign_after_the_authority_is_still_kept() -> None:
    from fluid_build.providers.odcs.mappers.servers import _without_userinfo

    url = "https://lk.example.com/p?owner=a@b.com"
    assert _without_userinfo(url) == url


# ── the import's command lines, paths and Spark settings ────────────────


@pytest.mark.parametrize(
    "command, shown",
    [
        ("tool --login scott/tiger run", "tool --login scott/***REDACTED*** run"),
        ("tool --login=scott/tiger run", "tool --login=scott/***REDACTED*** run"),
        (
            "spark-submit --conf spark.hadoop.fs.s3a.secret.key hunter2value job.py",
            "spark-submit --conf spark.hadoop.fs.s3a.secret.key ***REDACTED*** job.py",
        ),
        ('sqlplus "scott"/"pa ss"@db', 'sqlplus "scott"/***REDACTED***@db'),
        ('sqlplus scott/"pa ss"@db @run.sql', "sqlplus scott/***REDACTED***@db @run.sql"),
        ('sqlplus "scott"/"pa ss"', 'sqlplus "scott"/***REDACTED***'),
    ],
)
def test_the_command_line_shapes_that_leaked_are_masked(command: str, shown: str) -> None:
    from fluid_build.cli.import_workflow._redact import safe_command

    assert safe_command(command) == shown


@pytest.mark.parametrize(
    "command",
    [
        "bash --login ./run.sh",
        "bash --login -c 'make all'",
        "spark-submit --conf spark.sql.shuffle.partitions 200 job.py",
        "spark-submit --conf spark.executor.memory 4g job.py",
        'sqlplus -S "scott"',
    ],
)
def test_the_commands_that_are_no_login_or_secret_stay_as_written(command: str) -> None:
    from fluid_build.cli.import_workflow._redact import safe_command

    assert safe_command(command) == command


@pytest.mark.parametrize(
    "path",
    [
        "s3://lake/" + GH + "/x",
        "s3://lake/AKIA" + "ABCDEFGH23456789" + "/x",
        "gs://b/x/sk_live_" + "a1B2c3D4e5F6g7H8i9/y",
    ],
)
def test_a_path_segment_that_is_a_provider_token_is_masked(path: str) -> None:
    from fluid_build.cli.import_workflow._redact import scrub_path

    shown = scrub_path(path)
    assert "REDACTED" in shown and shown.split("/")[-1] in ("x", "y")
    assert GH not in shown and "ABCDEFGH23456789" not in shown and "a1B2c3D4e5" not in shown


def test_an_ordinary_path_is_left_as_it_is() -> None:
    from fluid_build.cli.import_workflow._redact import scrub_path

    assert scrub_path("s3://lake/dt=2020-01-01/part-0000.parquet") == (
        "s3://lake/dt=2020-01-01/part-0000.parquet"
    )


def _job(source: str) -> Any:
    from fluid_build.cli.import_workflow._fold import Folder
    from fluid_build.cli.import_workflow._pysource import parse_module
    from fluid_build.cli.import_workflow._spark_io import read_job

    module = parse_module("job.py", textwrap.dedent(source).encode())
    return read_job(module, Folder(module))


@pytest.mark.parametrize(
    "settings",
    [
        'SparkConf().setAll([("spark.sql.streaming.checkpointLocation", "s3a://b/ckpt")])',
        'SparkSession.builder.config(key="spark.sql.streaming.checkpointLocation", '
        'value="s3a://b/ckpt")',
        'SparkSession.builder.config("spark.sql.streaming.checkpointLocation", "s3a://b/ckpt")',
    ],
)
def test_the_spark_settings_shapes_are_read_for_the_default_checkpoint(settings: str) -> None:
    facts = _job(
        f"""
        from pyspark import SparkConf
        from pyspark.sql import SparkSession
        conf = {settings}
        spark = SparkSession.builder.getOrCreate()
        spark.readStream.table("a.b").writeStream.toTable("a.c")
        """
    )
    assert [w.checkpoint for w in facts.writes] == ["s3a://b/ckpt"]


def test_a_credential_setting_is_never_kept() -> None:
    facts = _job(
        """
        from pyspark import SparkConf
        conf = SparkConf().setAll([("spark.hadoop.fs.s3a.secret.key", "HUNTER2VALUE")])
        spark2 = SparkSession.builder.config(key="spark.hadoop.fs.s3a.secret.key",
                                             value="HUNTER2VALUE").getOrCreate()
        spark2.readStream.table("a.b").writeStream.toTable("a.c")
        """
    )
    assert "HUNTER2VALUE" not in repr(facts.__dict__)
    assert [w.checkpoint for w in facts.writes] == [None]


# ── the Data Mesh Manager provider ──────────────────────────────────────


def _dmm(api_key: str = TOKEN):
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

    provider = DataMeshManagerProvider(api_key=api_key, api_url="https://api.example.test")
    provider._session_instance = MagicMock()
    return provider


def test_an_error_body_that_echoes_the_key_does_not_show_it() -> None:
    from fluid_build.providers.base import ProviderError

    provider = _dmm()
    provider._session_instance.request.return_value = MagicMock(
        status_code=401, text=f"bad credentials: {TOKEN} is not valid"
    )
    with pytest.raises(ProviderError) as caught:
        provider._request("GET", "/api/teams")
    assert TOKEN not in str(caught.value) and "REDACTED" in str(caught.value)


def test_a_key_that_straddles_the_cut_of_an_error_body_is_not_shown_in_part() -> None:
    provider = _dmm()
    body = "x" * 480 + TOKEN
    shown = provider._redact_error_body(body)
    assert TOKEN[:10] not in shown and len(shown) <= 500


@pytest.mark.parametrize("bad", [".", "..", " .. ", "%2e%2e"])
def test_a_dot_segment_is_not_a_product_id(bad: str) -> None:
    from fluid_build.providers.base import ProviderError
    from fluid_build.providers.datamesh_manager._ids import dmm_product_id

    if bad.strip() in (".", ".."):
        with pytest.raises(ProviderError):
            dmm_product_id(bad)
    provider = _dmm()
    with pytest.raises(ProviderError):
        provider.delete(bad.strip())
    provider._session_instance.request.assert_not_called()


def test_an_ordinary_id_still_reaches_the_request() -> None:
    provider = _dmm()
    provider._session_instance.request.return_value = MagicMock(status_code=204, text="")
    assert provider.delete("silver.orders") is True
    assert provider._session_instance.request.call_args.args[1].endswith(
        "/api/dataproducts/silver.orders"
    )


# ── a table's location path ─────────────────────────────────────────────


def _contract_with_path(path: str, platform: str = "aws") -> dict:
    return {
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": platform,
                    "format": "iceberg",
                    "location": {"bucket": "lake", "path": path},
                },
            }
        ]
    }


@pytest.mark.parametrize("path", ["wh/x/..", "wh/./x", "../x", "wh\\..\\x", "wh/x/.. "])
@pytest.mark.parametrize("platform", ["aws", "gcp"])
def test_a_dot_segment_in_a_binding_location_path_is_refused(path: str, platform: str) -> None:
    from fluid_build.maintenance.spec import location_dot_segment_errors

    (error,) = location_dot_segment_errors(_contract_with_path(path, platform))
    assert "exposes[orders]" in error and "'.' or '..'" in error


@pytest.mark.parametrize("path", ["wh/x/", "wh/..x/y", "wh/x.y/z", "wh/a..b"])
def test_a_name_that_holds_dots_is_not_a_dot_segment(path: str) -> None:
    from fluid_build.maintenance.spec import location_dot_segment_errors

    assert location_dot_segment_errors(_contract_with_path(path)) == []


@pytest.mark.parametrize("path", ["./out/rows.parquet", "../data/rows.parquet"])
def test_a_local_files_relative_path_is_not_refused(path: str) -> None:
    from fluid_build.maintenance.spec import location_dot_segment_errors

    contract = {
        "exposes": [
            {
                "exposeId": "rows",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": path},
                },
            }
        ]
    }
    assert location_dot_segment_errors(contract) == []


def test_validate_reports_the_dot_segment_as_an_error() -> None:
    from fluid_build.cli.validate import _check_location_dot_segments
    from fluid_build.schema_manager import ValidationResult

    result = ValidationResult(True, [], [])
    _check_location_dot_segments(result, _contract_with_path("wh/x/.."))
    assert any("'.' or '..'" in e for e in result.errors)
    clean = ValidationResult(True, [], [])
    _check_location_dot_segments(clean, _contract_with_path("wh/x/"))
    assert not clean.errors


# ── a maintenance action's options ──────────────────────────────────────


@pytest.mark.parametrize(
    "key", ["password", "apiKey", "routing_key", "signingKey", "access_token", "Secret"]
)
def test_a_maintenance_option_named_like_a_secret_is_refused_whatever_its_value(key: str) -> None:
    from fluid_build.maintenance.spec import option_problems

    problems = option_problems({key: "plain-value", "mode": "fast"}, "options")
    assert len(problems) == 1 and key in problems[0] and "named like a secret" in problems[0]
    assert "plain-value" not in " ".join(problems)


def test_a_nested_maintenance_option_named_like_a_secret_is_refused() -> None:
    from fluid_build.maintenance.spec import option_problems

    problems = option_problems({"outer": [{"password": "x"}]}, "options")
    assert len(problems) == 1 and "options.outer[0].password" in problems[0]


def test_a_harmless_maintenance_option_still_passes() -> None:
    from fluid_build.maintenance.spec import option_problems

    options = {"max_tokens": 5, "author": "retail", "retain": "30d", "dedup_key": "a"}
    assert option_problems(options, "options") == []


def test_a_contract_with_a_secret_named_action_option_does_not_validate() -> None:
    from fluid_build.maintenance.spec import read_maintenance
    from tests.maintenance.conftest import maintained, spark_block

    action = {"name": "erase", "kind": "erase-rows", "options": {"password": TOKEN}}
    _tables, errors = read_maintenance(maintained(spark_block(actions=[action])))
    assert any("actions[0].options.password is named like a secret" in e for e in errors)
    assert TOKEN not in " ".join(errors)


# ── the alert routes in plan and apply ──────────────────────────────────


def _alerting_contract(routes: list) -> dict:
    import copy

    from tests.cli.test_apply_build_ids_before_tofu import _CONTRACT

    contract = copy.deepcopy(_CONTRACT)
    contract["fluidVersion"] = "0.7.7"
    contract["alerts"] = {"routes": routes}
    return contract


def _gate(contract: dict, command: str = "plan"):
    from fluid_build.cli._common import CLIError
    from fluid_build.cli.plan import _gate_contract_for_plan_or_apply

    sink = io.StringIO()
    log = logging.getLogger(f"test.wave_end_lows.{id(contract)}")
    log.addHandler(logging.StreamHandler(sink))
    log.setLevel(logging.DEBUG)
    try:
        _gate_contract_for_plan_or_apply(contract, log, command=command)
    except CLIError as exc:
        return exc, sink.getvalue()
    return None, sink.getvalue()


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_a_route_that_names_a_secret_is_refused_by_plan_and_apply(command: str) -> None:
    route = {"name": "x", "kind": "slack", "options": {"password": PW}}
    refusal, logged = _gate(_alerting_contract([route]), command)
    assert refusal is not None
    assert PW not in logged and PW not in repr(refusal.__dict__)


def test_a_clean_route_plans() -> None:
    refusal, _logged = _gate(_alerting_contract([{"name": "x", "kind": "slack"}]))
    assert refusal is None


@pytest.mark.parametrize(
    "route",
    [
        {"name": "x", "kind": "email", "to": ["ops:" + PW + "@mail.example.org"]},
        {"name": "x", "kind": "webhook", "connection": "https://hooks.example.org/" + PW},
    ],
)
def test_the_schema_errors_under_alerts_are_not_logged_with_the_value(route: dict) -> None:
    refusal, logged = _gate(_alerting_contract([route]))
    assert refusal is not None
    assert PW not in logged and PW not in repr(refusal.__dict__)


# ── a log argument that is an object ────────────────────────────────────

SIGV4 = (
    "AWS4-HMAC-SHA256 Credential=AKIA" + "ABCDEFGH23456789"
    "/20261122/eu-west-1/s3/aws4_request, "
    "SignedHeaders=host;x-amz-date, Signature=" + "ab12cd34" * 8
)


class _Request:
    """An object whose own text prints a credential, as a prepared HTTP request does."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text

    __str__ = __repr__


def _record(msg: str, args: Any) -> logging.LogRecord:
    record = logging.LogRecord("x", logging.DEBUG, "p", 1, msg, None, None)
    record.args = args
    return record


@pytest.mark.parametrize(
    "text",
    [
        "<AWSPreparedRequest url=https://s3.example.test, "
        f"headers={{'Authorization': b'{SIGV4}'}}>",
        f"<Request password={PW} method=GET>",
    ],
)
def test_the_filter_masks_an_object_argument_whose_text_holds_a_secret(text: str) -> None:
    record = _record("Sending http request: %s", (_Request(text),))
    sr.SecretRedactingFilter().filter(record)
    shown = record.getMessage()
    assert PW not in shown and "ab12cd34" not in shown and "ABCDEFGH23456789" not in shown
    assert shown.startswith("Sending http request: ")


def test_the_filter_masks_a_named_object_argument() -> None:
    record = _record("sent %(req)s", {"req": _Request(f"<Request password={PW}>")})
    sr.SecretRedactingFilter().filter(record)
    assert PW not in record.getMessage()


def test_the_filter_leaves_an_object_with_nothing_to_mask_and_numbers_alone() -> None:
    request = _Request("<Request method=GET url=https://s3.example.test/key>")
    record = _record("%s took %d ms (%.1f)", (request, 12, 0.5))
    sr.SecretRedactingFilter().filter(record)
    assert record.args == (request, 12, 0.5)
    assert record.getMessage() == (
        "<Request method=GET url=https://s3.example.test/key> took 12 ms (0.5)"
    )


def test_the_snowflake_layer_masks_an_object_value_whose_text_holds_a_secret() -> None:
    request = _Request(f"<AWSPreparedRequest headers={{'Authorization': b'{SIGV4}'}}>")
    for redacted in (redact_dict({"request": request}), redact_dict({"r": [request]})["r"][0]):
        assert "ab12cd34" not in str(redacted) and "ABCDEFGH23456789" not in str(redacted)
    untouched = _Request("<Request method=GET>")
    assert redact_dict({"request": untouched})["request"] is untouched


def test_a_sigv4_authorization_value_is_masked_by_both_layers() -> None:
    line = f"Authorization: {SIGV4}"
    for out in (redact_secret_text(line), redact_string(line)):
        assert "ab12cd34" not in out and "ABCDEFGH23456789" not in out


# ── the camelCase key names and the chat webhooks ───────────────────────


@pytest.mark.parametrize("key", ["routingKey", "integrationKey", "genieKey", "GenieKey"])
def test_the_camel_case_alert_key_names_are_masked_by_both_layers(key: str) -> None:
    payload = {key: TOKEN, "summary": "kept"}
    for redacted in (redact_value(payload), redact_dict(payload)):
        assert TOKEN not in str(redacted) and redacted["summary"] == "kept", key
    for out in (redact_secret_text(f"{key}={TOKEN}"), redact_string(f"{key}={TOKEN}")):
        assert TOKEN not in out


WEBHOOKS = {
    "teams workflows": "https://contoso.webhook.office.com/webhookb2/"
    f"{TOKEN}@{TOKEN}/IncomingWebhook/{TOKEN}",
    "teams connector": f"https://outlook.office.com/webhook/{TOKEN}@{TOKEN}/IncomingWebhook/{TOKEN}",
    "discord": "https://discord.com/api/webhooks/123456789012345678/" + "AbCd_" * 12,
    "discord app": "https://discordapp.com/api/v10/webhooks/123456789012345678/" + "AbCd_" * 12,
}


@pytest.mark.parametrize("name", sorted(WEBHOOKS))
def test_a_chat_webhook_url_is_masked_by_both_layers(name: str) -> None:
    url = WEBHOOKS[name]
    line = f"posting to {url} (end)"
    secret = url.rsplit("/", 1)[-1]
    for out in (redact_secret_text(line), redact_string(line)):
        assert secret not in out and out.endswith("(end)"), name


def test_an_ordinary_office_or_discord_url_stays() -> None:
    line = "see https://learn.microsoft.com/office and https://discord.com/channels/1/2"
    assert redact_secret_text(line) == line and redact_string(line) == line
