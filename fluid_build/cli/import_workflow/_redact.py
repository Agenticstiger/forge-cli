# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Text from the scanned code, made safe to print and to write into a contract.

Control and bidirectional characters are removed (:func:`safe_text`), a credential in a command
line or an argument list is masked word by word while what is not a secret stays as written
(:func:`safe_command`, :func:`safe_args`), and a data path or a host loses the credential in it
(:func:`scrub_path`, :func:`scrub_host`). The masking is the repository's own
(:mod:`fluid_build.observability.secret_redactor`) plus the shapes a command line has that a
free-text scan does not see: ``NAME=value``, a credential flag and its value, ``-u user:pass``,
``mysql -pSECRET``, a header in a quoted word, a command inside ``bash -c``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, List, Optional, Sequence, Union

from ._pysource import clean_chars


def safe_text(text: Any, limit: int = 200) -> str:
    """Text from the scanned code made safe to print and to write into a contract: control,
    line-separator and bidirectional characters are gone (an escape sequence, a new line), a
    credential-shaped value is masked, and it is cut at ``limit``."""
    from fluid_build.observability.secret_redactor import redact_secret_text

    cleaned = redact_secret_text(" ".join(clean_chars(text).split()))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


_REDACTED = "***REDACTED***"
#: A shell word: unquoted characters that are not an operator, escapes and quoted parts, as
#: written. An operator (``;``, ``&``, ``|``, ``(``, ``)``, ``<``, ``>``) ends a word and stays as
#: written, so masking a value never removes the operator beside it.
_SHELL_WORD = re.compile(r"""(?:[^\s'"\\;&|()<>`]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""")
#: Where one command ends and the next begins.
_COMMAND_BOUNDARY = re.compile(r"[;&|()\n]")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*=")
#: A Jinja field stays one word (it holds white space), and is a reference, not a value.
_JINJA = re.compile(r"\{\{.{0,1000}?\}\}|\{%.{0,1000}?%\}|\{#.{0,1000}?#\}", re.DOTALL)
_JINJA_TOKEN = re.compile(r"__fluidjinja(\d+)x(\d+)__")
#: Words that make a name a credential's: the redactor's own list covers ``password``, ``secret``,
#: ``token`` and the like inside a name; these are the spellings it does not (``--passwd``,
#: ``--pw``, ``--dbpass``), as whole words, and the compound ones, anywhere in the name.
_SECRET_WORDS = frozenset(
    {"pass", "passwd", "pswd", "pwd", "pw", "dbpass", "dbpwd", "cred", "creds", "sas", "signature"}
)
_SECRET_SUBSTRINGS = ("passwd", "passphrase", "accesskey", "secretkey", "apikey", "privatekey")
#: ``key`` alone is a column in a data job (``--partition-key``, ``--primary-key``); it is a
#: credential next to one of these (``--access-key``, ``--ssh-key``, ``--api-key``).
_KEY_QUALIFIERS = frozenset(
    {
        "access", "secret", "private", "api", "auth", "ssh", "account", "storage", "signing",
        "encryption", "master", "license", "aws", "client", "service",
    }
)  # fmt: skip
_NAME_PARTS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
#: A name that ends in one of these holds a reference to a secret, not the secret
#: (``--password-file``, ``--token-url``, ``--secret-name``): its value is left to the free-text
#: scan, which still finds a password inside a URL.
_REFERENCE_SUFFIXES = frozenset(
    {
        "file", "path", "dir", "directory", "id", "name", "provider", "class", "type", "env",
        "var", "ref", "url", "uri", "endpoint", "host", "port", "location", "store",
    }
)  # fmt: skip
_PASSWORD_SHORT_FLAGS = frozenset({"-p", "-P", "-w"})
#: ``-u user:password`` (and ``--user``): the part after the colon is the password.
_USER_FLAGS = frozenset({"-u", "--user", "--proxy-user"})
#: Commands whose ``-p``, ``-P`` or ``-w`` is a mode, a port or a directory, never a password.
_SHORT_FLAG_IS_NOT_A_PASSWORD = frozenset(
    {
        "mkdir", "cp", "mv", "ls", "tar", "ssh", "scp", "sftp", "rsync", "sort", "install", "rm",
        "ln", "ps", "nc", "ncat", "docker", "podman", "kubectl", "git", "pip", "hdfs", "hadoop",
        "xargs", "find", "grep", "egrep", "fgrep", "sed", "awk", "chmod", "chown", "du", "env",
        "java", "wget", "curl",
    }
)  # fmt: skip
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
_MAX_COMMAND_TEXT = 20_000
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]{0,40}://)[^/?#]{0,512}@")
#: How many levels of quoting inside quoting (``bash -c "bash -c ..."``) are looked into.
_MAX_DEPTH = 5
#: ``Name: value`` in one quoted word is a header (``-H "X-Api-Key: ..."``).
_HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(\S.*)$", re.DOTALL)


def sensitive_name(name: str) -> bool:
    """Whether a flag, an environment variable or a configuration key holds a credential."""
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    name = name.strip().lstrip("-")
    if not name:
        return False
    listed = [part.lower() for part in _NAME_PARTS.split(name) if part]
    if listed and listed[-1] in _REFERENCE_SUFFIXES:
        return False  # ``--password-file``: a path to a secret is not the secret
    if is_sensitive_key_name(name.replace("-", "_")):
        return True
    joined = re.sub(r"[^a-z0-9]", "", name.lower())
    if any(part in joined for part in _SECRET_SUBSTRINGS):
        return True
    words = set(listed)
    if words & _SECRET_WORDS:
        return True
    return bool(words & {"key", "keys"}) and bool(words & _KEY_QUALIFIERS)


def _mask_user(text: str) -> str:
    """``user:password`` as ``user:***REDACTED***``; a user name alone is not a secret."""
    user, colon, _secret = text.partition(":")
    return f"{user}:{_REDACTED}" if colon else text


def _path_or_port(value: str) -> bool:
    return value.isdigit() or value.startswith(("/", "./", "../", "~"))


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
        self._at_start = False

    def boundary(self) -> None:
        """A new command begins."""
        self.command, self._at_start = None, True

    def _start_of(self, value: str) -> None:
        if self._at_start and not _ASSIGNMENT.match(value) and value not in _COMMAND_PREFIX:
            self.command, self._at_start = value.rsplit("/", 1)[-1], False

    def show(self, value: str) -> str:
        from fluid_build.observability.secret_redactor import redact_secret_text

        self._start_of(value)
        if _JINJA_TOKEN.fullmatch(value):
            # a template reference, not a value: nothing to mask, and a flag before it is spent
            self._mask_next, self._setting_next, self._user_next = False, None, False
            return value
        if self._mask_next:
            short, self._mask_next = self._mask_next == "short", False
            if short:
                if not value.startswith("-") and not _path_or_port(value):
                    return _REDACTED
            elif not value.startswith("--"):
                return _REDACTED
        if self._setting_next is not None:
            flag, self._setting_next = self._setting_next, None
            return self._setting(value, flag)
        if self._user_next:
            self._user_next = False
            if ":" in value:
                return _mask_user(value)
        flag, equals, rest = value.partition("=")
        if value.startswith("-") and len(value) > 1 and not any(c.isspace() for c in flag):
            return self._flag(value, flag, equals, rest)
        if _ASSIGNMENT.match(value):
            name = value.partition("=")[0]
            if sensitive_name(name):
                return f"{name}={_REDACTED}"
        header = _HEADER.match(value)
        if header and sensitive_name(header.group(1)):
            return f"{header.group(1)}: {_REDACTED}"  # ``-H "X-Api-Key: ..."``
        if self.depth < _MAX_DEPTH and any(c.isspace() for c in value):
            return _redact_command(value, self.depth + 1)  # ``--extra "--password x"``
        return redact_secret_text(value)

    def _flag(self, value: str, flag: str, equals: str, rest: str) -> str:
        from fluid_build.observability.secret_redactor import redact_secret_text

        if flag in {"--conf", "-c"}:
            if equals:
                return f"{flag}={self._setting(rest, flag)}"
            self._setting_next = flag
            return value
        if flag in _USER_FLAGS:
            if equals:
                return f"{flag}={_mask_user(rest)}"
            self._user_next = True
            return value
        if value.startswith("-u") and not value.startswith("--") and ":" in value[2:]:
            return "-u" + _mask_user(value[2:])  # ``curl -uadmin:pw``
        if (
            self.command in _GLUED_PASSWORD_COMMANDS
            and value.startswith("-p")
            and not value.startswith("--")
            and len(value) > 2
            and not value[2:].isdigit()
        ):
            return "-p" + _REDACTED  # ``mysql -phunter2``
        short = flag in _PASSWORD_SHORT_FLAGS
        if short and self.command in _SHORT_FLAG_IS_NOT_A_PASSWORD:
            return redact_secret_text(value)
        if short or sensitive_name(flag):
            if equals:
                return f"{flag}={_REDACTED}"
            self._mask_next = "short" if short else True
            return value
        return redact_secret_text(value)

    def _setting(self, value: str, flag: str) -> str:
        """The value after ``--conf`` or ``-c``: ``key=value``, or, after ``-c`` when what comes
        before the ``=`` is not a dotted configuration key (``bash -c "PGPASSWORD=x run"``), a
        command line."""
        from fluid_build.observability.secret_redactor import redact_secret_text

        key, equals, _rest = value.partition("=")
        is_setting = (
            equals and not any(c.isspace() for c in key) and (flag == "--conf" or "." in key)
        )
        if is_setting:
            return f"{key}={_REDACTED}" if sensitive_name(key) else redact_secret_text(value)
        if self.depth < _MAX_DEPTH and any(c.isspace() for c in value):
            return _redact_command(value, self.depth + 1)
        return redact_secret_text(value)


def _unquote(raw: str) -> str:
    if raw.startswith(("$'", '$"')):
        raw = raw[1:]  # ``$'...'`` (ANSI-C quoting): the quoted text is the word
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


def _gap(text: str, *, edge: bool) -> str:
    """The text between two words: a line continuation is a space, a new line between two
    commands that no operator separates is ``;`` (at the start or end of the command it is
    nothing), and an operator is kept as written."""
    if "\n" not in text:
        return text
    if edge:
        return " "
    joined = text.replace("\\\r\n", " ").replace("\\\n", " ")
    if "\n" not in joined:
        return joined
    if any(char in joined for char in ";&|("):
        return joined.replace("\r", "").replace("\n", " ")
    return "; "


def _redact_command(command: str, depth: int = 0) -> str:
    """``command`` with the values of credentials masked, word by word, keeping what is not
    one exactly as written (quoting, operators, ``$VAR``, ``{{ ds }}``)."""
    fields: List[str] = []

    def hide(match: "re.Match[str]") -> str:
        fields.append(match.group(0))
        return f"__fluidjinja{depth}x{len(fields) - 1}__"

    def back(match: "re.Match[str]") -> str:
        index = int(match.group(2))
        if int(match.group(1)) == depth and index < len(fields):
            return fields[index]
        return match.group(0)

    text = _JINJA.sub(hide, command.replace("\r\n", "\n"))
    words = _Words(depth)
    words.boundary()
    out: List[str] = []
    position = 0
    first = True
    for match in _SHELL_WORD.finditer(text):
        gap = text[position : match.start()]
        if not first and _COMMAND_BOUNDARY.search(gap):
            words.boundary()
        out.append(_gap(gap, edge=first))
        first = False
        raw = match.group(0)
        value = _unquote(raw)
        shown = words.show(value)
        if shown == value:
            out.append(raw)
        else:
            out.append(shlex.quote(shown) if any(c.isspace() for c in shown) else shown)
        position = match.end()
    out.append(_gap(text[position:], edge=True))
    result = "".join(out)
    return _JINJA_TOKEN.sub(back, result) if fields else result


def safe_args(args: Sequence[str], limit: int = 200) -> List[str]:
    """Arguments of a submitted job, safe to write: a credential-shaped value is masked, and so is
    the value after a flag whose name is one (``--password hunter2``, ``-p hunter2``, ``--token=abc``,
    ``--conf spark.hadoop.fs.s3a.secret.key=abc``) and the value of ``NAME=value`` when the name is.
    """
    words = _Words()
    return [safe_text(words.show(str(arg)), limit) for arg in args]


def _parses(text: str) -> bool:
    try:
        shlex.split(text)
    except ValueError:
        return False
    return True


def safe_command(command: str, limit: int = 500) -> str:
    """A command line, safe to write: each word is looked at as :func:`safe_args` does, and the
    words that are nothing to hide stay as written, operators and quoting included. New lines
    between commands become ``;``. The text is then scanned as a whole, which finds what spans
    words (``Bearer <token>``), unless that would unbalance its quotes."""
    from fluid_build.observability.secret_redactor import redact_secret_text

    masked = " ".join(clean_chars(_redact_command(command[:_MAX_COMMAND_TEXT])).split())
    whole = redact_secret_text(masked)
    same_quotes = whole.count("'") == masked.count("'") and whole.count('"') == masked.count('"')
    text = whole if same_quotes and (_parses(whole) or not _parses(masked)) else masked
    return text if len(text) <= limit else text[: limit - 3] + "..."


def scrub_path(path: str) -> str:
    """A data path with no credential in it: the userinfo of a URL-style path (what stands
    between ``://`` and the ``@`` before the bucket or host) is dropped, and a ``key=value`` segment whose key is a credential's (``token=...``) keeps its
    key and loses its value. A path is an identity: this runs where it is read from the code, so
    nothing made from it (an id, a file name, a binding) can carry the credential."""
    path = _USERINFO.sub(r"\1", path)
    if "=" not in path:
        return path
    segments = path.split("/")
    for index, segment in enumerate(segments):
        key, equals, _value = segment.partition("=")
        if equals and sensitive_name(key):
            segments[index] = f"{key}=REDACTED"
    return "/".join(segments)


def scrub_host(host: str) -> str:
    """A ``host:port`` list with no ``user:password@`` in it."""
    return ",".join(part.rsplit("@", 1)[-1] for part in host.split(","))
