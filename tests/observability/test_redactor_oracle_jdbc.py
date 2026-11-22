# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A credential in a shape the URL-userinfo rule does not read: an Oracle JDBC URL
(``jdbc:oracle:thin:user/password@host``), a Slack incoming webhook, a Google API key glued to
a word.

The two redaction layers must mask the same shapes (CLAUDE.md: extend both), so each case runs
through the global layer and through the Snowflake twin, and the twin is asserted to share the
Oracle rule's regex object.

Every credential-shaped value is assembled at run time, so no source line holds a literal a
secret scanner would take for one.
"""

from __future__ import annotations

import time

import pytest

from fluid_build.observability import secret_redactor as global_layer
from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.providers.snowflake.util import logging as snowflake_layer
from fluid_build.providers.snowflake.util.logging import redact_string

PHRASE = "hunter" + "2phrase"
MASK = "***REDACTED***"


def thin(secret: str = PHRASE, rest: str = "dbhost:1521/ORCL", *, kind: str = "thin") -> str:
    return f"jdbc:oracle:{kind}:scott/{secret}@{rest}"


def twin(text: str) -> str:
    """The Snowflake layer's rendering, in the global layer's placeholder."""
    return redact_string(text).replace("[REDACTED]", MASK)


# --- an Oracle JDBC URL ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, masked",
    [
        (thin(), f"jdbc:oracle:thin:scott/{MASK}@dbhost:1521/ORCL"),
        (thin(rest="//dbhost:1521/ORCL"), f"jdbc:oracle:thin:scott/{MASK}@//dbhost:1521/ORCL"),
        (thin(rest="dbhost:1521:ORCL"), f"jdbc:oracle:thin:scott/{MASK}@dbhost:1521:ORCL"),
        (thin(kind="oci8", rest="ORCL"), f"jdbc:oracle:oci8:scott/{MASK}@ORCL"),
        (thin(kind="oci", rest="//h/S"), f"jdbc:oracle:oci:scott/{MASK}@//h/S"),
        (thin(kind="kprb", rest=""), f"jdbc:oracle:kprb:scott/{MASK}@"),
        (
            "JDBC:ORACLE:THIN:SCOTT/" + PHRASE.upper() + "@H:1521/S",
            f"JDBC:ORACLE:THIN:SCOTT/{MASK}@H:1521/S",
        ),
        # a user name in double quotes (Oracle's case-sensitive name), with the password quoted or not
        (
            'jdbc:oracle:thin:"SCOTT"/' + PHRASE + "@dbhost:1521/ORCL",
            f'jdbc:oracle:thin:"SCOTT"/{MASK}@dbhost:1521/ORCL',
        ),
        (
            'jdbc:oracle:thin:"SCOTT"/"p@' + PHRASE + '"@h:1521/S',
            f'jdbc:oracle:thin:"SCOTT"/{MASK}@h:1521/S',
        ),
        (
            'jdbc:oracle:thin:\\"SCOTT\\"/' + PHRASE + "@h:1521/S",
            f'jdbc:oracle:thin:\\"SCOTT\\"/{MASK}@h:1521/S',
        ),
        # a password that holds an @: quoted, quoted with escaped quotes, and not quoted at all
        (
            'jdbc:oracle:thin:scott/"p@' + PHRASE + '"@h:1521/S',
            f"jdbc:oracle:thin:scott/{MASK}@h:1521/S",
        ),
        (
            'jdbc:oracle:thin:scott/\\"p@' + PHRASE + '\\"@h:1521/S',
            f"jdbc:oracle:thin:scott/{MASK}@h:1521/S",
        ),
        (
            "jdbc:oracle:thin:scott/p@" + PHRASE + "@h:1521/S",
            f"jdbc:oracle:thin:scott/{MASK}@h:1521/S",
        ),
        # a password that holds a /
        (
            "jdbc:oracle:thin:scott/pa/" + PHRASE + "@h:1521/S",
            f"jdbc:oracle:thin:scott/{MASK}@h:1521/S",
        ),
        # inside a quoted string, and in a line of other text
        (
            '"' + thin() + '"',
            f'"jdbc:oracle:thin:scott/{MASK}@dbhost:1521/ORCL"',
        ),
        (
            "connect to " + thin() + " now",
            f"connect to jdbc:oracle:thin:scott/{MASK}@dbhost:1521/ORCL now",
        ),
    ],
)
def test_the_password_of_an_oracle_jdbc_url_is_masked_and_the_rest_kept(text, masked):
    assert redact_secret_text(text) == masked
    assert twin(text) == masked
    assert PHRASE.lower() not in redact_secret_text(text).lower()


def test_two_urls_in_one_line_are_both_masked():
    both = thin(PHRASE + "1") + " " + thin(PHRASE + "2", "other:1521/X")
    for out in (redact_secret_text(both), twin(both)):
        assert PHRASE not in out and out.count(MASK) == 2


@pytest.mark.parametrize(
    "text",
    [
        "jdbc:oracle:thin:@//h:1521/S",  # no login
        "jdbc:oracle:thin:@h:1521:SID",
        "jdbc:oracle:thin:scott@//h:1521/S",  # a user and no password
        "jdbc:oracle:thin:/@//h:1521/S",  # an external login: no user, no password
        "jdbc:oracle:thin:scott/tiger",  # no @: not a URL with a login
        "jdbc:postgresql://h:5432/db",
        "jdbc:oracle:thin",  # a prefix
        "oracle:thin:scott/tiger@h",  # not a JDBC URL
        "scott/tiger@h",  # an Oracle client's word is the importer's, not this rule's
    ],
)
def test_a_url_with_no_password_in_it_is_left_as_written(text):
    assert redact_secret_text(text) == text
    assert redact_string(text) == text


def test_the_twin_shares_the_rule_and_renders_it_as_a_prefix_and_a_suffix():
    assert global_layer._ORACLE_JDBC_RE in snowflake_layer.SENSITIVE_PATTERNS
    assert global_layer._ORACLE_JDBC_RE.groups == 2  # the twin's two-group branch renders it


def test_a_long_hostile_line_is_scanned_in_linear_time():
    for hostile in (
        "jdbc:oracle:thin:a/" * 10_000,
        "jdbc:oracle:thin:a/" + "x" * 200_000,
        'jdbc:oracle:thin:a/"' * 10_000,
        "jdbc:oracle:thin:" + "u" * 100_000 + "/p@h",
    ):
        for scan in (redact_secret_text, redact_string):
            started = time.perf_counter()
            scan(hostile)
            assert time.perf_counter() - started < 5  # it takes a fraction of a second


# --- every logging layer ---------------------------------------------------------------


@pytest.mark.parametrize("layer", ["snowflake", "local", "gcp"])
def test_each_provider_logging_layer_masks_the_three_shapes(layer):
    import importlib

    module = importlib.import_module(f"fluid_build.providers.{layer}.util.logging")
    for text in (
        thin(),
        'jdbc:oracle:thin:"SCOTT"/' + PHRASE + "@h",
        slack_webhook(),
        "k" + GOOGLE_KEY,
    ):
        out = module.redact_string(f"connect {text} now")
        assert PHRASE not in out and "abcdEFGH" not in out and GOOGLE_BODY[:8] not in out, (
            layer,
            text,
        )
        assert "[REDACTED]" in out
    assert module.redact_string(thin()) == "jdbc:oracle:thin:scott/[REDACTED]@dbhost:1521/ORCL"


# --- a Slack incoming webhook ---------------------------------------------------------


def slack_webhook(kind: str = "services") -> str:
    team, channel, token = "T" + "0" * 2 + "ABCDEF2", "B" + "0" * 2 + "GHIJKL3", "abcdEFGH" * 3
    return "https://hooks." + "slack.com/" + f"{kind}/{team}/{channel}/{token}"


@pytest.mark.parametrize("kind", ["services", "workflows", "triggers"])
def test_a_slack_webhook_is_masked_whole(kind):
    hook = slack_webhook(kind)
    for out in (
        redact_secret_text(f"notify {hook} done"),
        twin(f"notify {hook} done"),
        redact_secret_text(f'{{"url": "{hook}"}}'),
    ):
        assert "abcdEFGH" not in out and "hooks" not in out and MASK in out
    assert redact_secret_text("https://hooks.slack.com/") == "https://hooks.slack.com/"


# --- a Google API key -----------------------------------------------------------------

GOOGLE_BODY = "Sy" + "C" * 33
GOOGLE_KEY = "AIza" + GOOGLE_BODY


@pytest.mark.parametrize(
    "text",
    [
        GOOGLE_KEY,
        "key=" + GOOGLE_KEY,
        "https://maps.example/geocode?address=x&key=" + GOOGLE_KEY,
        "GOOGLE_" + GOOGLE_KEY,  # a word character before the A
        "?key%3D" + GOOGLE_KEY,  # URL-encoded
        "k" + GOOGLE_KEY,
    ],
)
def test_a_google_api_key_is_masked_wherever_it_stands(text):
    for out in (redact_secret_text(text), twin(text)):
        assert GOOGLE_BODY[:8] not in out and MASK in out
