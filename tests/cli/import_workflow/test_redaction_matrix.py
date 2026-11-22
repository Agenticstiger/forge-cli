# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A credential in a command is never shown, whatever the command is wrapped in.

Each credential shape (a flag, a glued short option, an environment assignment, a header, a URL,
an Oracle login, a JSON value, ``aws configure set <name> <value>``) is put inside each wrapper
(a quoted ``bash -c``, ``eval``, ``ssh``, ``sudo -u``, ``timeout``, ``xargs``, a here-document, a
comment, a line continuation, a subshell, a second line, ``docker run <image>``...), as a command
line and as one argument, and the secret must not be in what the redactor returns.
"""

from __future__ import annotations

import shlex

import pytest

from fluid_build.cli.import_workflow import _redact as R

SECRET = "QZXV7391k"  # pragma: allowlist secret

SHAPES = {
    "pw-flag": "run.sh --password {S}",
    "pw-eq": "run.sh --password={S}",
    "pw-quoted-dq": 'run.sh --password "{S}"',
    "pw-quoted-sq": "run.sh --password '{S}'",
    "mysql-p": "mysql -u root -p{S} db",
    "pgpassword": "PGPASSWORD={S} psql -h h db",  # pragma: allowlist secret
    "export-token": "export API_TOKEN={S}",
    "bearer": "curl -H 'Authorization: Bearer {S}' https://h/x",
    "url-userinfo": "curl https://user:{S}@host/path",
    "jdbc-oracle": "run.sh --jdbc-url jdbc:oracle:thin:scott/{S}@h:1521/svc",
    "sqlplus": "sqlplus scott/{S}@h:1521/svc",
    "s3a-secret": "run.sh --conf spark.hadoop.fs.s3a.secret.key={S}",  # pragma: allowlist secret
    "token-flag": "run.sh --token {S}",
    "api-key": "run.sh --api-key {S}",
    "json-password": 'run.sh --config \'{{"password": "{S}"}}\'',  # pragma: allowlist secret
    "aws-configure": "aws configure set aws_secret_access_key {S}",
    "secret-eq": "run.sh --secret={S} --user bob",
    "x-api-key": 'run.sh -H "X-Api-Key: {S}"',
    "psql-url": 'psql "postgresql://u:{S}@h/db"',
    "docker-login": "docker login -u u -p {S}",
    "client-secret": "run.sh --client-secret {S}",  # pragma: allowlist secret
    "env-assign": "DB_PASSWORD={S} ./run.sh",
}

WRAPPERS = {
    "plain": lambda c: c,
    "bash-c": lambda c: f"bash -c {shlex.quote(c)}",
    "bash-lc": lambda c: f"bash -lc {shlex.quote(c)}",
    "sh-c": lambda c: f"sh -c {shlex.quote(c)}",
    "eval": lambda c: f"eval {shlex.quote(c)}",
    "ssh": lambda c: f"ssh host {shlex.quote(c)}",
    "sudo": lambda c: f"sudo -u app {c}",
    "timeout": lambda c: f"timeout 10 {c}",
    "env": lambda c: f"env A=1 {c}",
    "nohup": lambda c: f"nohup {c} &",
    "trailing-comment": lambda c: f"{c} # note",
    "in-comment": lambda c: f"# {c}",
    "heredoc": lambda c: f"cat <<EOF\n{c}\nEOF",
    "lines": lambda c: f"echo start\n{c}\necho end",
    "and": lambda c: f"cd /x && {c}",
    "subshell": lambda c: f"$({c})",
    "backtick": lambda c: f"`{c}`",
    "continuation": lambda c: f"{c} \\\n  --extra x",
    "docker": lambda c: f"docker run img {c}",
    "bash-c-nested": lambda c: f"bash -c {shlex.quote('ssh host ' + shlex.quote(c))}",
    "xargs": lambda c: f"echo x | xargs {c}",
    "pipe": lambda c: f"{c} | tee out.log",
    "semicolon": lambda c: f"true; {c}; true",
}

#: What a shell would not make of the combination either: ``docker login`` inside ``docker run``
#: is the ``-p`` of ``run`` (a port), so it is not a password.
NOT_A_COMMAND = {("docker", "docker-login")}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_a_credential_is_not_shown_in_a_command_or_an_argument(wrapper, shape):
    if (wrapper, shape) in NOT_A_COMMAND:
        pytest.skip("not a command a shell would run")
    command = WRAPPERS[wrapper](SHAPES[shape].replace("{S}", SECRET))
    assert SECRET not in R.safe_command(command), R.safe_command(command)
    assert SECRET not in R.safe_args([command])[0], R.safe_args([command])[0]


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u app mysql -u root -pQZXV7391k db",
        "timeout 10 mysql -pQZXV7391k",
        "xargs mysqldump -pQZXV7391k db",
        "docker run img mysql -pQZXV7391k",
        "ssh host mysql -pQZXV7391k",
        "sudo -u app sqlplus scott/QZXV7391k@h:1521/svc",
        "xargs docker login -u u -p QZXV7391k",
        "aws configure set aws_secret_access_key QZXV7391k",
        "aws configure set --profile p aws_secret_access_key QZXV7391k",
        "tool set password QZXV7391k",
    ],
)
def test_a_client_after_a_wrapper_and_a_name_set_to_a_value_are_masked(command):
    assert "QZXV7391k" not in R.safe_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u app ls -p /tmp",
        "timeout 10 mkdir -p /tmp/x",
        "echo mysql is up",
        "aws configure set region eu-west-1",
        "aws configure set output json",
        "git config user.name bob",
    ],
)
def test_a_wrapper_and_set_do_not_hide_what_is_no_credential(command):
    assert R.safe_command(command) == command
