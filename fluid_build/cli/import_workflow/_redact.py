# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Text from the scanned code, made safe to print and to write into a contract.

Control and bidirectional characters are removed (:func:`safe_text`), a credential in a command
line or an argument list is masked word by word while what is not a secret stays as written
(:func:`safe_command`, :func:`safe_args`), and a data path or a host loses the credential in it
(:func:`scrub_path`, :func:`scrub_host`). The masking is the repository's own
(:mod:`fluid_build.observability.secret_redactor`) plus the shapes a command line has that a
free-text scan does not see: ``NAME=value``, a credential flag and its value, ``-u user:pass``,
``mysql -pSECRET``, a header or a JSON value in a quoted word, ``key=value`` pairs in a
connection string or a query string, a command inside ``bash -c``.

What is not a secret stays: a name that ends in ``file``, ``path``, ``id``, ``url`` and the like
holds a reference, a ``true``, a small number or a path is no secret, a command keeps its lines,
its operators and its quoting, and ``mkdir -p``, ``docker run -u 1000:1000`` and ``git log
--author`` come out as they went in.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, List, Optional, Sequence, Union

from ._pysource import clean_chars

_REDACTED = "***REDACTED***"


def _free_text(text: str) -> str:
    """The repository's own free-text redaction of ``text``."""
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(text)


def safe_text(text: Any, limit: int = 200) -> str:
    """Text from the scanned code made safe to print and to write into a contract: control,
    line-separator and bidirectional characters are gone (an escape sequence, a new line), a
    credential-shaped value is masked, and it is cut at ``limit``."""
    cleaned = _scan(" ".join(clean_chars(text).split()))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


#: A shell word: unquoted characters that are not an operator, escapes and quoted parts, as
#: written. An operator (``;``, ``&``, ``|``, ``(``, ``)``, ``<``, ``>``, a backtick) ends a word
#: and stays as written, so masking a value never removes the operator beside it.
_SHELL_WORD = re.compile(r"""(?:[^\s'"\\;&|()<>`]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""")
#: Where one command ends and the next begins.
_COMMAND_BOUNDARY = re.compile(r"[;&|()\n]")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*=")
#: A Jinja field stays one word (it holds white space), and is a reference, not a value.
_JINJA = re.compile(r"\{\{.{0,1000}?\}\}|\{%.{0,1000}?%\}|\{#.{0,1000}?#\}", re.DOTALL)
_JINJA_TOKEN = re.compile(r"__fluidjinja(\d+)x(\d+)__")
#: ``Name: value`` in one quoted word is a header (``-H "X-Api-Key: ..."``).
_HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(\S.*)$", re.DOTALL)
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
#: ``key=value`` pairs, separated by ``;``, ``&``, ``,``, ``?`` or white space (a connection
#: string, a query string).
_PAIR = re.compile(r"(^|[;&,?\s])([A-Za-z_][A-Za-z0-9_.\-]{0,64})=([^;&,\s]*)")
#: ``"key": "value"`` in JSON, as ``json.dumps`` writes it (a space after the colon) or compact,
#: and ``["KEY"] = "value"`` in code.
_JSON_PAIR = re.compile(
    r"""(["'])([A-Za-z_][A-Za-z0-9_.\-]{0,64})\1(\]?\s*[:=]\s*)(["'])([^"']{0,512})\4"""
)
#: What makes a masked word need quotes again: white space, an operator, a quote, a backslash.
_NEEDS_QUOTES = re.compile(r"""[\s;&|()<>`$\\'"]""")
_USERINFO = re.compile(
    r"^((?:[A-Za-z][A-Za-z0-9+.\-]{0,40}:)*[A-Za-z][A-Za-z0-9+.\-]{0,40}://)([^/?#]{0,512})@"
)
#: An ``s3a`` URL that names an access key and its secret before the ``@`` (Hadoop takes the secret
#: as it is, and an AWS secret holds ``/``):
#: the user information of these runs to the ``@``, not to the first ``/``.
_S3_USERINFO = re.compile(
    r"^((?:[A-Za-z][A-Za-z0-9+.\-]{0,40}:)*s3[an]?://)([^/:@?#]{0,512}:[^@?#]{0,512})@", re.I
)
_S3_SECRET = re.compile(r"(s3[an]?://[^/:@\s?#'\"]{0,512}:)[^@\s?#'\"]{1,512}@", re.I)
_MAX_COMMAND_TEXT = 20_000
#: Characters of one argument read for credentials (an argument is shown at 200).
_MAX_ARG = 2_000
#: How many levels of quoting inside quoting (``bash -c "bash -c ..."``) are looked into.
_MAX_DEPTH = 5

#: Words that make a name a credential's: the redactor's own list covers ``password``, ``secret``,
#: ``token`` and the like inside a name; these are the spellings it does not (``--passwd``,
#: ``--pw``, ``--dbpass``), as whole words, and the compound ones, anywhere in the name.
_SECRET_WORDS = frozenset(
    {"pass", "passwd", "pswd", "pwd", "pw", "dbpass", "dbpwd", "cred", "creds", "sas", "sig"}
    | {"signature", "cookie", "cookies"}
)
#: In a URL's query string these are credentials, although ``key`` and ``code`` alone are only
#: names in a path or a job's arguments (``--partition-key``, ``dt=...``).
_QUERY_SECRETS = frozenset({"key", "code", "sid", "session", "sessionid", "auth", "jwt"})
#: A header whose name holds one of these words carries a credential, ``key`` included
#: (``X-Api-Key``, ``Ocp-Apim-Subscription-Key``).
_HEADER_WORDS = frozenset(
    {"cookie", "authorization", "auth", "key", "keys", "session", "token", "secret", "password"}
    | {"signature", "credential", "credentials"}
)
#: The word between ``Authorization:`` and the credential.
_AUTH_SCHEMES = frozenset({"bearer", "basic", "token", "digest", "negotiate", "bot"})
_SECRET_SUBSTRINGS = ("passwd", "passphrase", "accesskey", "secretkey", "apikey", "privatekey")
#: ``key`` alone is a column in a data job (``--partition-key``, ``--primary-key``); it is a
#: credential next to one of these (``--access-key``, ``--ssh-key``, ``--api-key``).
_KEY_QUALIFIERS = frozenset(
    {
        "access", "secret", "private", "api", "auth", "ssh", "account", "storage", "signing",
        "encryption", "master", "license", "aws", "client", "service", "sse",
    }
)  # fmt: skip
_NAME_PARTS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
#: Words that contain a credential's name and are not one (``--author``, ``--tokenizer``,
#: ``spark.authenticate``): they are left out before a name is judged.
_NOT_CREDENTIAL_WORDS = frozenset(
    {
        "author", "authors", "authored", "authority", "authorities", "authorized", "authorised",
        "authenticate", "authenticated", "authentication", "tokenizer", "tokenizers", "tokenize",
        "tokenized", "secretary", "secretaries",
    }
)  # fmt: skip
#: A name that ends in one of these holds a reference to a secret, or a setting about it, not the
#: secret (``--password-file``, ``--token-url``, ``--secret-name``, ``--auth-type``): its value is
#: left to the free-text scan, which still finds a password inside a URL.
_REFERENCE_SUFFIXES = frozenset(
    {
        "file", "path", "dir", "directory", "id", "name", "provider", "class", "type", "env",
        "var", "ref", "url", "uri", "endpoint", "host", "port", "location", "store", "arn",
        "version", "stdin", "count", "limit", "size", "length", "ttl", "timeout", "expiry", "jar",
        "expiration", "interval", "enabled", "mode", "algorithm", "format", "strategy", "scheme",
        "method", "kind",
    }
)  # fmt: skip
#: A name that starts with one of these is a switch (``--skip-auth``, ``--no-password``): it takes
#: no value, and the word after it is not one.
_SWITCH_PREFIXES = frozenset(
    {
        "skip", "no", "disable", "disabled", "enable", "enabled", "use", "uses", "is", "has",
        "with", "without", "allow", "require", "verify", "ignore", "show", "print", "dry", "check",
    }
)  # fmt: skip
#: Values that are no secret: a flag's state, a small number (a count, a port), a path.
_PLAIN_WORDS = frozenset({"true", "false", "yes", "no", "on", "off", "none", "null", "nil"})

_PASSWORD_SHORT_FLAGS = frozenset({"-p", "-P", "-w"})
#: ``-u user:password`` (and ``--user``): the part after the colon is the password.
_USER_FLAGS = frozenset({"-u", "--user", "--proxy-user"})
#: Commands whose ``-u`` is a user, a group or a JDBC url, not ``user:password``.
_USER_FLAG_NOT_CREDENTIALS = frozenset(
    {
        "docker", "podman", "nerdctl", "crictl", "kubectl", "beeline", "hive", "su", "sudo", "ssh",
        "scp", "git", "ansible", "ansible-playbook",
    }
)  # fmt: skip
#: Commands whose ``-p``, ``-P`` or ``-w`` is a mode, a port, a width or a directory, never a
#: password.
_SHORT_FLAG_IS_NOT_A_PASSWORD = frozenset(
    {
        "mkdir", "cp", "mv", "ls", "tar", "ssh", "scp", "sftp", "rsync", "sort", "install", "rm",
        "ln", "ps", "nc", "ncat", "docker", "podman", "kubectl", "git", "pip", "hdfs", "hadoop",
        "xargs", "find", "grep", "egrep", "fgrep", "sed", "awk", "chmod", "chown", "du", "env",
        "java", "wget", "curl", "wc", "diff", "cut", "tr", "date", "less", "head", "tail",
        "watch", "top", "df", "free", "kill", "pgrep", "pkill",
    }
)  # fmt: skip
#: Flags that carry a password in one command and mean something else in most.
_SHORT_PASSWORD_BY_COMMAND = {
    "redis-cli": frozenset({"-a"}),
    "openssl": frozenset({"-k", "-pass", "-passin", "-passout"}),
    "sshpass": frozenset({"-p"}),
    "curl": frozenset({"-b"}),  # a cookie
}
#: ``docker login -p``: the one use of ``-p`` in these commands that is a password.
_LOGIN_COMMANDS = frozenset({("docker", "login"), ("podman", "login"), ("nerdctl", "login")})
#: Oracle clients take ``user/password@service`` as a word (``sqlplus app/pw@db``) or as the value
#: of ``userid=``.
_ORACLE_COMMANDS = frozenset({"sqlplus", "sqlldr", "expdp", "impdp", "rman", "sqlcl"})
_ORACLE_LOGIN = re.compile(r"([^/\s@=]+)/([^@\s/]+)(@\S*)?")
#: Commands whose ``-P<value>``, with no space, is the password.
_GLUED_UPPER_P_COMMANDS = frozenset({"bcp", "sqlcmd"})
#: Commands whose ``-p<value>``, with no space, is the password.
_GLUED_PASSWORD_COMMANDS = frozenset(
    {
        "mysql", "mysqldump", "mysqladmin", "mysqlimport", "mysqlcheck", "mariadb",
        "mariadb-dump", "mariadb-admin", "mysqlsh",
    }
)  # fmt: skip
#: Words that come before the command in a command line.
_COMMAND_PREFIX = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time", "exec", "sudo", "env"}
    | {"nohup", "nice"}
)
#: Schemes whose ``container@account`` is a place, not a ``user:password`` (Azure storage).
_CONTAINER_SCHEMES = frozenset({"abfs", "abfss", "wasb", "wasbs", "adl"})


def sensitive_name(name: str) -> bool:
    """Whether a flag, an environment variable or a configuration key holds a credential."""
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    name = name.strip().lstrip("-")
    if not name:
        return False
    listed = [part.lower() for part in _NAME_PARTS.split(name) if part]
    if not listed or listed[-1] in _REFERENCE_SUFFIXES or listed[0] in _SWITCH_PREFIXES:
        return False  # ``--password-file``: a path to a secret is not the secret
    kept = [part for part in listed if part not in _NOT_CREDENTIAL_WORDS]
    if not kept:
        return False
    if is_sensitive_key_name("_".join(kept)):
        return True
    if any(part in "".join(kept) for part in _SECRET_SUBSTRINGS):
        return True
    words = set(kept)
    if words & _SECRET_WORDS:
        return True
    return bool(words & {"key", "keys"}) and bool(words & _KEY_QUALIFIERS)


def _plain_value(value: str) -> bool:
    """A value that is no secret: a flag's state, a number of up to four digits, a path."""
    text = value.strip().strip("'\"")
    return (
        text.lower() in _PLAIN_WORDS
        or _JINJA_TOKEN.fullmatch(text) is not None  # a template field is a reference
        or (text.isdigit() and len(text) <= 4)
        or text.startswith(("/", "./", "../", "~"))
    )


def _path_or_port(value: str) -> bool:
    return value.isdigit() or value.startswith(("/", "./", "../", "~"))


def _is_user_credential(value: str, command: Optional[str]) -> bool:
    """Whether ``value`` after ``-u`` is ``user:password``: not a numeric ``uid:gid``, not a URL
    (``beeline -u jdbc:hive2://...``), and not the ``-u`` of a command that has no passwords."""
    if command in _USER_FLAG_NOT_CREDENTIALS:
        return False
    user, colon, secret = value.partition(":")
    return bool(colon and secret) and not user.isdigit() and "//" not in value and "/" not in user


def _mask_user(text: str) -> str:
    """``user:password`` as ``user:***REDACTED***``; a user name alone is not a secret."""
    user, colon, _secret = text.partition(":")
    return f"{user}:{_REDACTED}" if colon else text


def sensitive_header(name: str) -> bool:
    """Whether an HTTP header of this name carries a credential (``Cookie``, ``X-Api-Key``,
    ``Ocp-Apim-Subscription-Key``, ``Authorization``)."""
    if sensitive_name(name):
        return True
    words = {part.lower() for part in _NAME_PARTS.split(name.strip()) if part}
    return bool(words & _HEADER_WORDS)


def _mask_json(text: str) -> str:
    """The value of each ``"key": "value"`` pair (JSON) and ``["KEY"] = "value"`` (code) of
    ``text`` whose key is a credential's, masked."""
    if (":" not in text and "=" not in text) or ('"' not in text and "'" not in text):
        return text

    def mask(match: "re.Match[str]") -> str:
        if sensitive_name(match.group(2)) and not _plain_value(match.group(5)):
            quote, key, colon, inner = match.group(1, 2, 3, 4)
            return f"{quote}{key}{quote}{colon}{inner}{_REDACTED}{inner}"
        return match.group(0)

    return _JSON_PAIR.sub(mask, text)


def _mask_kv(text: str) -> str:
    """The value of each ``key=value`` pair of ``text`` (a connection string, a query string)
    whose key is a credential's, masked. After a ``?`` the keys of a query string that name a
    credential in a URL (``key``, ``code``) count too."""
    if "=" not in text:
        return text

    def mask(match: "re.Match[str]") -> str:
        name = match.group(2)
        in_query = "?" in text[: match.end(1)]
        named = sensitive_name(name) or (in_query and name.lower() in _QUERY_SECRETS)
        if named and not _plain_value(match.group(3)):
            return f"{match.group(1)}{name}={_REDACTED}"
        return match.group(0)

    return _PAIR.sub(mask, text)


def _mask_pairs(text: str) -> str:
    """JSON and ``key=value`` pairs of ``text`` whose key is a credential's, masked."""
    return _mask_kv(_mask_json(text))


def _oracle_jdbc(text: str) -> str:
    """``jdbc:oracle:thin:user/password@host`` (``oci``, ``oci8``, ``kprb``, any case) with the
    password masked: the repository's own rule, which the free-text scan below also applies."""
    from fluid_build.observability.secret_redactor import _ORACLE_JDBC_RE

    return _ORACLE_JDBC_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}{m.group(2)}", text)


def _scan(text: str) -> str:
    """One word, or one value, read for credentials: the free-text scan of the repository, the
    secret in the user information of an ``s3a`` URL (it holds ``/``, which the scan stops at), the
    password of an Oracle JDBC URL (``user/password@host``, no ``://``), and the pairs a
    connection string, a query string or JSON holds."""
    if "@" in text:
        text = _S3_SECRET.sub(lambda m: f"{m.group(1)}{_REDACTED}@", text)
        text = _oracle_jdbc(text)
    return _mask_pairs(_free_text(text))


class _Words:
    """Decides, word by word, what a command line or an argument list may show. A word that
    is nothing to hide is returned as it is, so a command keeps the quoting it was written in."""

    def __init__(self, depth: int = 0) -> None:
        self.depth = depth
        #: After a credential flag: ``True`` (mask the next word) or ``"short"`` (``-p``, ``-w``:
        #: mask it unless it is a path or a port, as in ``mkdir -p /tmp/x`` and ``ssh -p 22``).
        self._mask_next: Union[bool, str] = False
        self._setting_next: Optional[str] = None
        self._user_next = False
        #: The command the word belongs to (its first word, without a path); ``None`` in an
        #: argument list, which has no command.
        self.command: Optional[str] = None
        #: The word after the command, when it is not a flag (``docker login``).
        self.subcommand: Optional[str] = None
        self._at_start = False
        self._expect_subcommand = False

    def boundary(self) -> None:
        """A new command begins."""
        self.command, self.subcommand, self._at_start = None, None, True
        self._expect_subcommand = False

    def _start_of(self, value: str) -> None:
        if self._at_start and not _ASSIGNMENT.match(value) and value not in _COMMAND_PREFIX:
            self.command, self._at_start = value.rsplit("/", 1)[-1], False
            self._expect_subcommand = True
        elif self._expect_subcommand:
            self._expect_subcommand = False
            if not value.startswith("-"):
                self.subcommand = value

    def show(self, value: str) -> str:
        self._start_of(value)
        if _JINJA_TOKEN.fullmatch(value):
            # a template reference, not a value: nothing to mask, and a flag before it is spent
            self._mask_next, self._setting_next, self._user_next = False, None, False
            return value
        if self._mask_next == "header":
            # ``Authorization: Bearer <credential>`` as separate words: the scheme stays
            if value.lower() in _AUTH_SCHEMES:
                return value
            self._mask_next = False
            return _REDACTED
        if self._mask_next:
            short, self._mask_next = self._mask_next == "short", False
            if short:
                if not value.startswith("-") and not _path_or_port(value):
                    return _REDACTED
            elif not value.startswith("--") and not _plain_value(value):
                return _REDACTED
        if self._setting_next is not None:
            flag, self._setting_next = self._setting_next, None
            return self._setting(value, flag)
        if self._user_next:
            self._user_next = False
            if _is_user_credential(value, self.command):
                return _mask_user(value)
        if self.command in _ORACLE_COMMANDS:
            login = self._oracle_login(value)
            if login is not None:
                return login
        if value.startswith("-H") and len(value) > 2 and not value.startswith("--"):
            glued = _HEADER.match(value[2:])  # ``curl -H"Authorization: token ..."``
            if glued:
                return f"-H{self._header(glued)}"
        name, equals, rest = value.partition("=")
        if value.startswith("-") and len(value) > 1 and not any(c.isspace() for c in name):
            return self._flag(value, name, equals, rest)
        if _ASSIGNMENT.match(value) and sensitive_name(name):
            return value if _plain_value(rest) else self._secret_value(name + "=", rest)
        header = _HEADER.match(value)
        if header:
            shown = self._header(header)
            if shown != value:
                return shown
        elif (
            value.endswith(":")
            and sensitive_header(value[:-1])
            and _HEADER_NAME.fullmatch(value[:-1])
        ):
            self._mask_next = "header"  # ``Authorization: Bearer <credential>`` as words
            return value
        if any(c.isspace() for c in value):
            return self._multi_word(value)
        return _scan(value)

    @staticmethod
    def _oracle_login(value: str) -> Optional[str]:
        """``user/password@service`` (or ``userid=user/password@service``) with the password
        masked, or ``None`` when the word is not one."""
        head, equals, rest = value.partition("=")
        if equals and head.lower() == "userid":
            masked = _Words._oracle_login(rest)
            return f"{head}={masked}" if masked is not None else None
        found = _ORACLE_LOGIN.fullmatch(value) if not value.startswith("-") else None
        if found is None:
            return None
        return f"{found.group(1)}/{_REDACTED}{found.group(3) or ''}"

    def _header(self, match: "re.Match[str]") -> str:
        """``Name: value`` with the value masked when the header carries a credential."""
        name = match.group(1)
        return f"{name}: {_REDACTED}" if sensitive_header(name) else match.group(0)

    def _secret_value(self, head: str, rest: str) -> str:
        """``head`` and the masked value ``rest``. A value that is a quoted word with a flag list
        after the first word (``-Dpassword=x -Dfoo=bar``) keeps the flags."""
        found = re.match(r"\S*\s+(.*)", rest, re.DOTALL)
        tail = found.group(1) if found else ""
        if tail.lstrip().startswith("-") and self.depth < _MAX_DEPTH:
            return f"{head}{_REDACTED} {_redact_command(tail.lstrip(), self.depth + 1)}"
        return f"{head}{_REDACTED}"

    def _multi_word(self, value: str) -> str:
        """A word that holds white space: JSON, a header, SQL, or a command line in quotes. The
        free-text scan reads the whole of it (it knows ``"password": "x"`` and ``Bearer x``); one
        that starts like a command line (a flag, ``NAME=value``) is then read word by word."""
        masked = _scan(value)
        stripped = masked.lstrip()
        if self.depth < _MAX_DEPTH and (stripped.startswith("-") or _ASSIGNMENT.match(stripped)):
            return _redact_command(masked, self.depth + 1)
        return masked

    def _flag(self, value: str, flag: str, equals: str, rest: str) -> str:
        if flag in {"--conf", "-c"}:
            if equals:
                return f"{flag}={self._setting(rest, flag)}"
            self._setting_next = flag
            return value
        if flag in _USER_FLAGS:
            if equals:
                return f"{flag}={_mask_user(rest) if _is_user_credential(rest, self.command) else rest}"
            self._user_next = True
            return value
        if (
            value.startswith("-u")
            and not value.startswith("--")
            and _is_user_credential(value[2:], self.command)
        ):
            return "-u" + _mask_user(value[2:])  # ``curl -uadmin:pw``
        if (
            self.command in _GLUED_PASSWORD_COMMANDS
            and value.startswith("-p")
            and not value.startswith("--")
            and len(value) > 2
            and not value[2:].isdigit()
        ):
            return "-p" + _REDACTED  # ``mysql -phunter2``
        if (
            self.command in _GLUED_UPPER_P_COMMANDS
            and value.startswith("-P")
            and not value.startswith("--")
            and len(value) > 2
        ):
            return "-P" + _REDACTED  # ``bcp ... -Phunter2``
        short = flag in _PASSWORD_SHORT_FLAGS
        logs_in = (self.command, self.subcommand) in _LOGIN_COMMANDS
        if short and self.command in _SHORT_FLAG_IS_NOT_A_PASSWORD and not logs_in:
            return _scan(value)
        by_command = flag in _SHORT_PASSWORD_BY_COMMAND.get(self.command or "", ())
        judged = flag[2:] if re.match(r"-D[A-Za-z_]", flag) else flag  # ``-Dpwd=x`` (a JVM option)
        if short or by_command or sensitive_name(judged):
            if equals:
                return value if _plain_value(rest) else self._secret_value(flag + "=", rest)
            self._mask_next = "short" if short else True
            return value
        return _scan(value)

    def _setting(self, value: str, flag: str) -> str:
        """The value after ``--conf`` or ``-c``: ``key=value``, or, after ``-c`` when what comes
        before the ``=`` is not a dotted configuration key (``bash -c "PGPASSWORD=x run"``), a
        command line. A value that holds options (``-Dpassword=x -Dy=z``) is read as a command
        line too."""
        key, equals, rest = value.partition("=")
        is_setting = (
            equals and not any(c.isspace() for c in key) and (flag == "--conf" or "." in key)
        )
        if is_setting:
            if sensitive_name(key):
                return value if _plain_value(rest) else f"{key}={_REDACTED}"
            if self.depth < _MAX_DEPTH and (rest.lstrip().startswith("-") or " " in rest.strip()):
                return f"{key}={_redact_command(rest, self.depth + 1)}"
            return _scan(value)
        if self.depth < _MAX_DEPTH and any(c.isspace() for c in value):
            if value.lstrip().startswith(("{", "[")):
                return self._multi_word(value)  # JSON is data, not a command line
            return _redact_command(value, self.depth + 1)
        return _scan(value)


def _unquote(raw: str) -> str:
    if raw.startswith(("$'", '$"')):
        raw = raw[1:]  # ``$'...'`` (ANSI-C quoting): the quoted text is the word
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


def _redact_command(command: str, depth: int = 0) -> str:
    """``command`` with the values of credentials masked, word by word, keeping what is not
    one exactly as written (lines, quoting, operators, ``$VAR``, ``{{ ds }}``)."""
    fields: List[str] = []

    def hide(match: "re.Match[str]") -> str:
        fields.append(match.group(0))
        return f"__fluidjinja{depth}x{len(fields) - 1}__"

    def back(match: "re.Match[str]") -> str:
        index = int(match.group(2))
        if int(match.group(1)) == depth and index < len(fields):
            return fields[index]
        return match.group(0)

    text = _mask_json(_JINJA.sub(hide, command.replace("\r\n", "\n")))
    words = _Words(depth)
    words.boundary()
    out: List[str] = []
    position = 0
    first = True
    for match in _SHELL_WORD.finditer(text):
        gap = text[position : match.start()]
        if not first and _COMMAND_BOUNDARY.search(gap):
            words.boundary()
        out.append(gap)
        first = False
        raw = match.group(0)
        value = _unquote(raw)
        shown = words.show(value)
        if shown == value:
            out.append(raw)
        else:
            quote = _NEEDS_QUOTES.search(shown.replace(_REDACTED, ""))
            out.append(shlex.quote(shown) if quote else shown)
        position = match.end()
    out.append(text[position:])
    result = "".join(out)
    return _JINJA_TOKEN.sub(back, result) if fields else result


def safe_args(args: Sequence[str], limit: int = 200) -> List[str]:
    """Arguments of a submitted job, safe to write: a credential-shaped value is masked, and so is
    the value after a flag whose name is one (``--password hunter2``, ``-p hunter2``, ``--token=abc``,
    ``--conf spark.hadoop.fs.s3a.secret.key=abc``), the value of ``NAME=value`` when the name is,
    and a secret in a JSON value or a connection string."""
    words = _Words()
    return [safe_text(words.show(str(arg)[:_MAX_ARG]), limit) for arg in args]


def safe_command(command: str, limit: int = 2000) -> str:
    """A command line, safe to write: each word is looked at as :func:`safe_args` does (a word
    that holds white space, a header or a JSON value in quotes, is read whole), and the words
    that are nothing to hide stay as written, lines, operators and quoting included."""
    text = clean_chars(_redact_command(command[:_MAX_COMMAND_TEXT]), keep="\n\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def scrub_path(path: str) -> str:
    """A data path with no credential in it. The userinfo of a URL-style path (what stands between
    ``://`` and the ``@`` before the bucket or host) is dropped, except where it is a place
    (``abfss://container@account...``, Azure storage, where only ``container:password@`` is a
    credential). A ``key=value`` segment of the path, and a pair of its query string, whose key is
    a credential's (``token=...``, ``sig=...``, and in a query string ``key=...`` and ``code=...``)
    keeps its key and loses its value. The user information of an ``s3`` URL runs to the ``@``
    (an AWS secret holds ``/``). A path is an
    identity: this runs where it is read from the code, so nothing made from it (an id, a file
    name, a binding) can carry the credential."""
    if "@" in path:
        path = _oracle_jdbc(path)
    found = _S3_USERINFO.match(path) or _USERINFO.match(path)
    if found:
        scheme = found.group(1)[:-3].rsplit(":", 1)[-1].lower()
        if scheme not in _CONTAINER_SCHEMES or ":" in found.group(2):
            path = found.group(1) + path[found.end() :]
    path, question, query = path.partition("?")
    if query:
        query = "&".join(_scrub_pair(pair, query=True) for pair in query.split("&"))
    if "=" in path:
        path = "/".join(_scrub_pair(segment) for segment in path.split("/"))
    return path + question + query


def _scrub_pair(pair: str, *, query: bool = False) -> str:
    key, equals, _value = pair.partition("=")
    named = sensitive_name(key) or (query and key.lower() in _QUERY_SECRETS)
    return f"{key}=REDACTED" if equals and named else pair


def scrub_host(host: str) -> str:
    """A ``host:port`` list with no ``user:password@`` in it."""
    return ",".join(part.rsplit("@", 1)[-1] for part in host.split(","))
