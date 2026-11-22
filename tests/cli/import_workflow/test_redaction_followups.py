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

SECRET = "SEKRT9"  # pragma: allowlist secret
LOGIN = "jdbc:oracle:thin:scott/" + SECRET + "@dbhost:1521/ORCL"
MASK = "***REDACTED***"


def test_an_argument_a_little_over_the_old_limit_is_read_whole():
    arg = "Bearer " + "A" * 1963 + " " + LOGIN
    assert 2_000 < len(arg) <= R._MAX_ARG
    shown = " ".join(R.safe_args(["--conf-json", arg], limit=10_000))
    assert SECRET not in shown and MASK in shown


def test_an_argument_too_long_to_read_whole_shows_only_its_size():
    # a text cut where it is too long can split a credential: the part of it that is left would be
    # shown, so none of what could not be read is
    prefix = "jdbc:oracle:thin:scott/SEK"
    arg = "Bearer " + "A" * (R._MAX_ARG - 8 - len(prefix)) + " " + prefix + "RT9@dbhost:1521/ORCL"
    assert len(arg) > R._MAX_ARG
    (shown,) = R.safe_args([arg], limit=10_000)
    assert shown == f"<an argument of {len(arg):,} characters, too long to show>"


def test_the_start_of_a_credential_longer_than_the_limit_is_not_shown():
    password = "P" * 4_500
    (shown,) = R.safe_args(["https://user:QZX" + password + "@host/x"], limit=10_000)
    assert "QZX" not in shown and "user" not in shown


def test_an_argument_made_short_by_collapsing_its_white_space_is_read_whole():
    arg = "--password" + " " * 6_000 + "hunter2"
    assert len(arg) > R._MAX_ARG
    assert R.safe_args([arg], limit=10_000) == ["--password ***REDACTED***"]


def test_a_flag_before_an_argument_that_was_too_long_does_not_mask_the_one_after_it():
    got = R.safe_args(["--password", "x" * 5_000, "orders"], limit=10_000)
    assert got[1:] == [R._too_long("an argument", 5_000), "orders"]


def test_a_command_too_long_to_read_shows_only_its_size():
    command = "echo " + "x" * R._MAX_COMMAND_TEXT
    assert (
        R.safe_command(command) == f"<a command of {len(command):,} characters, too long to show>"
    )


def test_a_command_that_is_as_long_as_it_can_be_is_shown_to_its_end():
    command = "echo " + "x" * (R._MAX_COMMAND_TEXT - 5)  # as long as it can be
    assert len(command) == R._MAX_COMMAND_TEXT
    assert R.safe_command(command, limit=100_000) == command


def test_a_zero_width_character_does_not_hide_a_credential_from_the_rules():
    zw = "\u200b"
    for text, secret in [
        (f"tool scott/QZXV7391@{zw}db:1521", "QZXV7391"),
        (f"tool scott/QZXV7391@db:{zw}1521", "QZXV7391"),
        (f"tool pass{zw}word=QZXV7391", "QZXV7391"),
        (f"mysql --pass{zw}word QZXV7391", "QZXV7391"),
    ]:
        assert secret not in R.safe_command(text), text
        assert secret not in " ".join(R.safe_args(text.split(" ")[1:])), text


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
        ("scott/pa:ss@db:1521", f"scott/{MASK}@db:1521"),
        ("scott/it's@db:1521", f"scott/{MASK}@db:1521"),
        ("scott/p@ss@db:1521", f"scott/{MASK}@db:1521"),
        ("jörg/secret@db:1521", f"jörg/{MASK}@db:1521"),
        ("scott/secret@[::1]:1521", f"scott/{MASK}@[::1]:1521"),
        ("scott/secret@//[::1]:1521/x", f"scott/{MASK}@//[::1]:1521/x"),
        ('"scott"/secret@db:1521', f'"scott"/{MASK}@db:1521'),
        ("scott/secret@10.0.0.5:1521/ORCL", f"scott/{MASK}@10.0.0.5:1521/ORCL"),
        ("APP$OWNER/s3cr3t#1@db-01.corp:1521", f"APP$OWNER/{MASK}@db-01.corp:1521"),
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
        "1user/secret@db:1521",  # Oracle takes no unquoted user that starts with a digit
        "gcr.io/proj/img@sha256:1234abcdef0123",  # an image digest is not a port
        "docker.io/library/ubuntu@sha256:45b23dee08af5e43a7fea6c4cf9c25ccf269ee113168c19722f87876677c5cb2",
        "--conf=spark.kubernetes.container.image=myrepo/spark@sha256:99aabbcc",
        "repo/img@sha256:" + "1" * 64,
        "data/2024/01/01@12:30",  # a time, not a host and a port
        "daily/job@10:30",
        "user.name/secret@db:1521",  # a dot is not in an unquoted Oracle user
    ],
)
def test_what_is_not_a_login_is_left_alone(arg):
    assert R.safe_args([arg]) == [arg]


def test_a_url_with_user_information_is_still_the_url_rules_business():
    (shown,) = R.safe_args(["https://user:pw@host:443/x"])
    assert "pw" not in shown and shown.startswith("https://user:")


# ── what a review of the first version found ─────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "# don't log this\nmysql --password QZXV7391 -e 'select 1'",
        "# don't log this\ncurl -H 'X-Api-Key: QZXV7391' https://x",
        "echo hi # it's done\nmysql --password QZXV7391",
        "# it's\n# can't\nmysql --password QZXV7391\n# won't",
    ],
)
def test_an_apostrophe_in_a_comment_is_no_quote(text):
    assert "QZXV7391" not in R.safe_command(text)


def test_an_apostrophe_in_a_here_document_is_no_quote():
    text = "cat <<EOF\nit's here\n--password QZXV7391\nok's\nEOF\nmysql --password QZXV7391"
    out = R.safe_command(text)
    assert "QZXV7391" not in out and "it's here" in out and out.endswith(f"{MASK}")
    quoted = "cat <<'END'\nit's here\npassword: QZXV7391\nEND\n"
    assert "QZXV7391" not in R.safe_command(quoted)
    dash = "cat <<-EOF\n\tit's --token QZXV7391\n\tEOF\n"
    assert "QZXV7391" not in R.safe_command(dash)


def test_a_comment_and_a_here_document_are_otherwise_shown_as_written():
    text = "echo a # a note, it's fine\nls $#\ncat <<EOF\nhello's world\nEOF\necho ${#x}\n"
    assert R.safe_command(text) == text.strip()


def test_a_script_in_a_quoted_word_is_read_line_by_line():
    # an odd quote inside the word does not stop a later line from being read
    word = "echo it's\nmysql --password QZXV7391\necho done"
    assert "QZXV7391" not in R.safe_command(f'bash -c "{word}"')
    assert "QZXV7391" not in R.safe_args([word])[0]


@pytest.mark.parametrize("size", [100, 300, 600, 3_000])
@pytest.mark.parametrize("compact", [False, True])
def test_a_long_json_secret_is_masked_whole(size, compact):
    secret = "QZX" + "ab1" * (size // 3)
    sep = '":"' if compact else '": "'
    text = '{"password' + sep + secret + '"}'
    for shown in (R.safe_text(text, 10_000), R.safe_args([text], limit=10_000)[0]):
        assert "QZX" not in shown and MASK in shown


@pytest.mark.parametrize(
    "command",
    [
        'run scott/"p@QZXV7391"@db:1521',
        'run scott/"my QZXV7391"@db:1521',
        "bash -c 'run scott/\"p@QZXV7391\"@db:1521'",
        "run scott/p@QZXV7391@db:1521/ORCL --out x",
    ],
)
def test_a_quoted_password_in_a_command_is_masked(command):
    assert "QZXV7391" not in R.safe_command(command)


def test_a_credential_nested_deeper_than_the_words_are_read_is_still_hidden():
    import shlex

    text = "mysql --password QZXV7391"
    for depth in range(1, 9):
        text = "bash -c " + shlex.quote(text)
        assert "QZXV7391" not in R.safe_command(text), depth


def test_the_new_rules_cost_in_proportion_to_the_text():
    import time

    hostile = [
        "a/" * 2_000,
        "@" * 4_000,
        ("a/" * 60 + "@") * 60,
        "a/" + "9" * 4_000,
        "a/b@" + "c:" * 2_000,
        "a/b@c:" + "1" * 4_000,
        '"' * 2_000 + "a/" + "\\" * 1_000,
        "x/" + "@:1" * 1_300,
        "cat <<EOF\n" * 1_500,  # a here-document that never ends, over and over
        "".join(f"cat <<D{index}\n" for index in range(1_500)),
        "# it's a note\n" * 1_000,
    ]
    started = time.perf_counter()
    for text in hostile:
        R.safe_args([text])
        R.safe_command(text)
        R.safe_text(text)
    assert time.perf_counter() - started < 10


def test_a_setting_too_deep_to_read_is_hidden_when_it_names_a_credential():
    deep = R._Words(R._MAX_DEPTH)
    assert deep._setting("run --password QZXV7391", "-c") == MASK
    assert deep._setting("run --table orders", "-c") == "run --table orders"


def test_a_word_too_deep_to_read_is_hidden_whole_when_it_names_a_credential():
    deep = R._Words(R._MAX_DEPTH)
    assert deep._multi_word("mysql --password QZXV7391 -e select") == MASK
    assert deep._multi_word("mysql --table orders -e select") == "mysql --table orders -e select"
