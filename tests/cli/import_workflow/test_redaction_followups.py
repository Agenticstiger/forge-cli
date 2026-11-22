# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A credential is read whole before anything is cut, and a bare Oracle login is masked.

``safe_args`` used to read an argument up to 2,000 characters and then cut what it showed at 200:
masking a long token before an Oracle login shrinks the text so that a login the cut had split (its
``@host`` is past the cut, so it did not look like one) came within the 200 that are shown. An
argument is now read whole (the code folds no string longer than 4,096 characters), and when a text
is cut all the same, its end is not shown.
"""

from __future__ import annotations

import pytest

from fluid_build.cli.import_workflow import _redact as R

SECRET = "SEKRT9"
LOGIN = "jdbc:oracle:thin:scott/" + SECRET + "@dbhost:1521/ORCL"
MASK = "***REDACTED***"


def test_an_argument_a_little_over_the_old_limit_is_read_whole():
    arg = "Bearer " + "A" * 1963 + " " + LOGIN
    assert 2_000 < len(arg) <= R._MAX_ARG
    shown = " ".join(R.safe_args(["--conf-json", arg], limit=10_000))
    assert SECRET not in shown and MASK in shown


def test_a_login_the_cut_splits_is_not_shown_though_masking_shrinks_the_text():
    prefix = "jdbc:oracle:thin:scott/SEK"  # the cut falls inside the password: no @host after it
    arg = "Bearer " + "A" * (R._MAX_ARG - 8 - len(prefix)) + " " + prefix + "RT9@dbhost:1521/ORCL"
    assert arg.index("RT9") == R._MAX_ARG
    shown = " ".join(R.safe_args(["--conf-json", arg], limit=10_000))
    assert "SEK" not in shown and "RT9" not in shown


def test_the_end_of_an_argument_that_was_cut_is_not_shown():
    arg = "x" * (R._MAX_ARG + 5_000)
    (shown,) = R.safe_args([arg], limit=100_000)
    assert len(shown) == R._MAX_ARG - R._CUT_TAIL


def test_an_argument_that_was_not_cut_is_shown_to_its_end():
    arg = "x" * R._MAX_ARG
    (shown,) = R.safe_args([arg], limit=100_000)
    assert shown == arg


def test_the_end_of_a_command_that_was_cut_is_not_shown():
    lead = "curl -H 'Authorization: Bearer "
    prefix = "echo jdbc:oracle:thin:scott/SEK"  # the cut falls inside the password
    fill = R._MAX_COMMAND_TEXT - len(lead) - len("' ") - len(prefix)
    command = lead + "A" * fill + "' " + prefix + "RT9@dbhost:1521/ORCL"
    assert command.index("RT9") == R._MAX_COMMAND_TEXT
    shown = R.safe_command(command, limit=100_000)
    assert "SEK" not in shown and "RT9" not in shown


def test_a_command_that_was_not_cut_keeps_its_end():
    command = "echo " + "x" * (R._MAX_COMMAND_TEXT - 5)  # as long as it can be
    assert len(command) == R._MAX_COMMAND_TEXT
    assert R.safe_command(command, limit=100_000) == command


def long_credential_split_by_the_cut(limit: int) -> str:
    """Plain words, then a JDBC login whose 500-character password the cut at ``limit`` splits."""
    password = "P" * 500  # the JDBC rule reads up to 512
    login = "jdbc:oracle:thin:scott/" + password + "@dbhost:1521/ORCL"
    lead_length = limit - len("jdbc:oracle:thin:scott/") - 450
    words, rest = divmod(lead_length - 4, 3)
    text = "x" * rest + "run " + "ok " * words + login
    assert text[limit - 1] == text[limit] == "P"  # the cut falls inside the password
    return text


def test_the_end_of_a_long_text_that_was_cut_holds_none_of_a_credential_it_split():
    arg = long_credential_split_by_the_cut(R._MAX_ARG)
    (shown,) = R.safe_args([arg], limit=100_000)
    assert len(shown) > 3_000 and "PPPP" not in shown
    command = long_credential_split_by_the_cut(R._MAX_COMMAND_TEXT)
    shown = R.safe_command(command, limit=100_000)
    assert len(shown) > 10_000 and "PPPP" not in shown


# ── a bare Oracle Easy Connect login ──────────────────────────────────


@pytest.mark.parametrize(
    "arg, expected",
    [
        ("scott/tiger@db.example.com:1521/ORCL", f"scott/{MASK}@db.example.com:1521/ORCL"),
        ("scott/tiger@db:1521", f"scott/{MASK}@db:1521"),
        (
            "app_user/p4ss$word@db-01.internal:1522/svc.example",
            f"app_user/{MASK}@db-01.internal:1522/svc.example",
        ),
        ("scott/tiger@//db.example.com/ORCL", f"scott/{MASK}@//db.example.com/ORCL"),
        ("scott/tiger@//db.example.com:1521/ORCL", f"scott/{MASK}@//db.example.com:1521/ORCL"),
        ('scott/"p@ss:w/rd"@db:1521/ORCL', f"scott/{MASK}@db:1521/ORCL"),
        ("scott/ti/ger@db:1521", f"scott/{MASK}@db:1521"),
        ("spark.jdbc.login=scott/tiger@db:1521/s", f"spark.jdbc.login=scott/{MASK}@db:1521/s"),
        ("--dsn=scott/tiger@db:1521/s", f"--dsn=scott/{MASK}@db:1521/s"),
    ],
)
def test_a_login_given_as_a_plain_value_has_its_password_masked(arg, expected):
    (shown,) = R.safe_args([arg])
    assert shown == expected


def test_a_login_after_a_flag_is_masked_whatever_the_flag_is_called():
    assert R.safe_args(["--db", "scott/tiger@db:1521/s"]) == ["--db", f"scott/{MASK}@db:1521/s"]


def test_a_login_in_a_command_is_masked():
    text = R.safe_command("run-report --dsn scott/tiger@db:1521/ORCL --out /tmp/x")
    assert "tiger" not in text and f"scott/{MASK}@db:1521/ORCL" in text and "--out /tmp/x" in text


@pytest.mark.parametrize(
    "arg",
    [
        "a/b@c.com",  # no port: an address or a path
        "scott/tiger@db",  # no port
        "s3://bucket/key@2:80",  # a URL: the user part follows a slash
        "/data/scott/tiger@db:1521",  # a path
        "user@host:22",
        "10/20@12:30",  # a user starts with a letter
        "scott/tiger@//",  # no host
        "scott/tiger@db:1",  # a port has two digits at least
    ],
)
def test_what_is_not_a_login_is_left_alone(arg):
    assert R.safe_args([arg]) == [arg]


def test_a_url_with_user_information_is_still_the_url_rules_business():
    (shown,) = R.safe_args(["https://user:pw@host:443/x"])
    assert "pw" not in shown and shown.startswith("https://user:")
