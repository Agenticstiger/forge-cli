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
from typing import Any, Callable, List, Optional, Sequence, Union

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
_SHELL_WORD = re.compile(
    r"""(?:[^\s'"\\;&|()<>`]|\\.|\$'(?:[^'\\]|\\.)*'|'[^']*'|"(?:[^"\\]|\\.)*")+"""
)
#: A comment (``#`` where a word would begin, to the end of the line) is not shell: an apostrophe in
#: one is no quote, and must not pair with the next one, some lines below.
_SHELL_TOKEN = re.compile(
    r"(?P<comment>(?:^|(?<=[\s;&|()]))\#[^\n]*)|(?P<word>" + _SHELL_WORD.pattern + ")", re.M
)
#: A here-document (``<<EOF``, ``<<-'EOF'``): its body is text, not words, up to the delimiter.
_HEREDOC_START = re.compile(r"""(?<!<)<<(?!<)-?[ \t]*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n""")
#: How many here-documents of one text are set apart: a text that opens more is read as it comes.
_MAX_HEREDOCS = 64
_HEREDOC_TOKEN = re.compile(r"__fluidheredoc(\d+)x(\d+)__")
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
    r"""(["'])([A-Za-z_][A-Za-z0-9_.\-]{0,64})\1(\]?\s*[:=]\s*)(["'])([^"']{0,20000})\4"""
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
#: A command longer than this is not read, and so is not shown (:func:`safe_command`).
_MAX_COMMAND_TEXT = 20_000
#: An argument longer than this, once its white space is collapsed, is not read, and so is not shown
#: (:func:`safe_args`). A text cut where it is too long to read can split a credential (the
#: ``@host`` that ends it, or the ``-----END`` of a key, is past the cut), and what is left of it
#: would be shown; no part of what could not be read is.
_MAX_ARG = 4_096
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
#: ``user/password@alias`` where the alias is a name from ``tnsnames.ora`` (no port, no ``//``),
#: with what follows it (``as sysdba``) kept. The password is in double quotes or has no ``/``,
#: ``@`` or white space. Where a flag or a command says it is a login, the alias may be anything;
#: as a word with nothing around it, the user is a plain name and the alias too (see
#: :func:`_mask_login`).
_LOGIN_WITH_ALIAS = re.compile(
    r"(?P<user>[^/\s@=]{1,64})/"
    r"(?P<password>\"[^\"\r\n]{1,128}\"|[^@\s/\"]{1,128})"
    r"@(?P<alias>\S+?)(?P<rest>\s.*)?",
    re.DOTALL,
)
_PLAIN_USER = re.compile(r"\"?[^\W\d][\w$#]{0,63}\"?")
_PLAIN_ALIAS = re.compile(r"[A-Za-z_][\w.\-]*")
#: What follows ``@`` in ``pkg/mod@v1.2.3``, ``org/repo@main``, ``img/name@latest``: a version or a
#: ref, not a service name.
_REF_ALIASES = frozenset(
    {"main", "master", "latest", "head", "develop", "dev", "stable", "next", "canary", "nightly"}
)
_VERSION_ALIAS = re.compile(r"v?\d|(?:release|rel|rc|r)[-_.]?\d", re.IGNORECASE)
#: Flags whose value is a database login (``--dsn scott/tiger@orcl``, ``-u scott/tiger@orcl``).
_LOGIN_FLAGS = frozenset({"--dsn", "--userid", "-u"})
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
#: Commands whose own options carry a credential. One named after a wrapper (``sudo -u app mysql``,
#: ``timeout 10 mysql``, ``xargs docker login``, ``docker run img mysql``) is the command the words
#: after it belong to.
_CLIENT_COMMANDS = (
    _GLUED_PASSWORD_COMMANDS
    | _GLUED_UPPER_P_COMMANDS
    | _ORACLE_COMMANDS
    | frozenset(_SHORT_PASSWORD_BY_COMMAND)
    | {command for command, _ in _LOGIN_COMMANDS}
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


#: A host in a login: a name or an IPv4 address (not digits alone: ``12:30`` is a time), or an IPv6
#: address in brackets.
_DSN_HOST = r"(?:\[[0-9A-Fa-f:.]{2,45}\]|(?![0-9]+(?:[:/]|$))[A-Za-z0-9_.\-]+)"
#: ``user/password@host:port`` or ``user/password@//host[:port]``, the start of an Oracle Easy Connect
#: login written as a plain word or as the value of an option (``--dsn scott/tiger@db:1521/ORCL``);
#: what follows is kept. The user is a name Oracle takes unquoted (letters, digits, ``_``, ``$``,
#: ``#``, the first one not a digit) or one in double quotes; the password is in double quotes or
#: anything up to the last ``@host:port`` (it may hold ``/``, ``:``, ``@``). The port, or the ``//``,
#: is what makes it one: a ``a/b@c`` is a path or an address, and the port ends the word (an image
#: digest, ``img@sha256:1234abcd``, is not a port). It does not follow a ``/`` (a path, or the middle
#: of a URL).
_ORACLE_DSN = re.compile(
    r"(?:(?<![\w/.\-])|(?<=\s-[A-Za-z])|(?<=^-[A-Za-z]))(\"[^\"\s/]{1,64}\"|[^\W\d][\w$#]{0,63})/"
    r"(\"[^\"\r\n]{1,128}\"|\S{1,128})"
    r"(@(?://" + _DSN_HOST + r"(?::\d{2,5})?|" + _DSN_HOST + r":\d{2,5}(?!\w)))"
)


def _mask_dsn(text: str) -> str:
    """``text`` with the password of each Oracle Easy Connect login masked."""
    if "@" not in text:
        return text
    return _ORACLE_DSN.sub(lambda m: f"{m.group(1)}/{_REDACTED}{m.group(3)}", text)


def _mask_login(value: str, *, named: bool) -> Optional[str]:
    """``user/password@alias`` (a bare Oracle login with a name from ``tnsnames.ora``) with the
    password masked, or ``None`` when ``value`` is not one.

    ``named`` is True where the flag or the command in front says it is a login (``-u``,
    ``--dsn``, ``userid=``, ``sqlplus``): the alias may be anything and ``as sysdba`` may follow.
    A word with nothing around it is one only when it reads like one (a plain user name, an alias
    that is a name and neither a version nor a ref), so ``pkg/mod@v1.2.3`` and ``org/repo@main``
    are left as they are."""
    found = _LOGIN_WITH_ALIAS.fullmatch(value)
    if found is None:
        return None
    if not named:
        alias = found.group("alias")
        if (
            found.group("rest")
            or not _PLAIN_USER.fullmatch(found.group("user"))
            or not _PLAIN_ALIAS.fullmatch(alias)
            or alias.lower() in _REF_ALIASES
            or _VERSION_ALIAS.match(alias)
        ):
            return None
    return f"{found.group('user')}/{_REDACTED}@{found.group('alias')}{found.group('rest') or ''}"


_AUTHORITY_END = re.compile(r"""[ \t\r\n/?#'"]""")
#: The user information the shared URL rule reads has a password of up to this many characters.
_URL_PASSWORD_READ = 4_000


def _mask_long_url_password(text: str) -> str:
    """The password of a URL that is longer than the shared rule reads (``://user:<5,000 characters>@
    host``), masked: a text can be longer than that rule's cap, and what it leaves would be shown.
    """
    if len(text) <= _URL_PASSWORD_READ or "://" not in text:
        return text
    out: List[str] = []
    position = 0
    for start in re.finditer(r"://", text):
        if start.start() < position:
            continue
        begin = start.end()
        found = _AUTHORITY_END.search(text, begin)
        authority = text[begin : found.start() if found else len(text)]
        at = authority.rfind("@")
        colon = authority.find(":")
        if at > colon >= 0 and at - colon - 1 > _URL_PASSWORD_READ:
            out.append(text[position : begin + colon + 1])
            out.append(_REDACTED)
            position = begin + at
    out.append(text[position:])
    return "".join(out)


def _scan(text: str) -> str:
    """One word, or one value, read for credentials: the free-text scan of the repository, the
    secret in the user information of an ``s3a`` URL (it holds ``/``, which the scan stops at), the
    password of an Oracle JDBC URL (``user/password@host``, no ``://``), and the pairs a
    connection string, a query string or JSON holds."""
    if "@" in text:
        text = _mask_long_url_password(text)
        text = _S3_SECRET.sub(lambda m: f"{m.group(1)}{_REDACTED}@", text)
        text = _oracle_jdbc(text)
        text = _mask_dsn(text)
    # the pairs first: the free-text rule masks what follows ``"password":`` up to the first quote
    # and leaves a long value that comes after it
    return _free_text(_mask_pairs(text))


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
        #: After ``-u``, ``--dsn`` or ``--userid``: the next word may be a login.
        self._login_next = False
        #: The command the word belongs to (its first word, without a path); ``None`` in an
        #: argument list, which has no command.
        self.command: Optional[str] = None
        #: The word after the command, when it is not a flag (``docker login``).
        self.subcommand: Optional[str] = None
        self._at_start = False
        self._expect_subcommand = False
        #: A ``set`` came in this command: the name of a credential after it is followed by its
        #: value (``aws configure set --profile p aws_secret_access_key <value>``).
        self._set = False

    def boundary(self) -> None:
        """A new command begins."""
        self.command, self.subcommand, self._at_start = None, None, True
        self._expect_subcommand = False
        self._set = False

    def _start_of(self, value: str) -> None:
        name = value.rsplit("/", 1)[-1]
        if self._at_start and not _ASSIGNMENT.match(value) and value not in _COMMAND_PREFIX:
            self.command, self._at_start = name, False
            self._expect_subcommand = True
        elif (
            name in _CLIENT_COMMANDS
            and self.command is not None
            and name != self.command
            and not value.startswith("-")
        ):
            # a client named after a wrapper: the words that follow are its own
            self.command, self.subcommand, self._expect_subcommand = name, None, True
        elif self._expect_subcommand:
            self._expect_subcommand = False
            if not value.startswith("-"):
                self.subcommand = value

    def show(self, value: str) -> str:
        self._start_of(value)
        if _JINJA_TOKEN.fullmatch(value):
            # a template reference, not a value: nothing to mask, and a flag before it is spent
            self.skip()
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
        if self._login_next:
            self._login_next = False
            login = self._oracle_login(value, named=True)
            if login is not None:
                return login
        if self.command in _ORACLE_COMMANDS or value[:7].lower() == "userid=":
            login = self._oracle_login(value)
            if login is not None:
                return login
        if value.startswith("-H") and len(value) > 2 and not value.startswith("--"):
            glued = _HEADER.match(value[2:])  # ``curl -H"Authorization: token ..."``
            if glued:
                return f"-H{self._header(glued)}"
        name, equals, rest = value.partition("=")
        if self._set and not equals and not value.startswith("-") and sensitive_name(value):
            self._mask_next = True  # ``aws configure set aws_secret_access_key <value>``
            return value
        if value == "set":
            self._set = True
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
        login = _mask_login(value, named=False)  # ``etl/pw@dwh`` as an argument of its own
        if login is not None:
            return login
        return _scan(value)

    @staticmethod
    def _oracle_login(value: str, *, named: bool = False) -> Optional[str]:
        """``user/password@service`` (or ``userid=user/password@service``) with the password
        masked, or ``None`` when the word is not one. ``named``: the word follows a flag that
        takes a login, so a service of any shape, and ``as sysdba`` after it, is one."""
        head, equals, rest = value.partition("=")
        if equals and head.lower() == "userid":
            masked = _Words._oracle_login(rest, named=True)
            return f"{head}={masked}" if masked is not None else None
        if value.startswith("-"):
            return None
        found = None if named else _ORACLE_LOGIN.fullmatch(value)
        if found is not None:
            return f"{found.group(1)}/{_REDACTED}{found.group(3) or ''}"
        return _mask_login(value, named=True)

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
        """A word that holds white space: JSON, a header, SQL, a script, or a command line in
        quotes (``bash -lc "..."``, ``eval "..."``, ``ssh host "..."``). The free-text scan reads
        the whole of it (it knows ``"password": "x"`` and ``Bearer x``); what is not JSON is then
        read word by word as a command line, and one of several lines line by line (a quote that
        opens in one line closes in it, as far as this goes). Too deep for that, a word that
        names a credential is hidden whole."""
        masked = _scan(value)
        if self.depth >= _MAX_DEPTH:
            return _REDACTED if _names_a_credential(masked) else masked
        if masked.lstrip().startswith(("{", "[")):
            return masked  # JSON is data, not a command line
        if "\n" in masked:
            return "\n".join(_redact_command(line, self.depth + 1) for line in masked.split("\n"))
        return _redact_command(masked, self.depth + 1)

    def _flag(self, value: str, flag: str, equals: str, rest: str) -> str:
        if flag in {"--conf", "-c"}:
            if equals:
                return f"{flag}={self._setting(rest, flag)}"
            self._setting_next = flag
            return value
        if flag in _LOGIN_FLAGS and self.command not in _USER_FLAG_NOT_CREDENTIALS:
            if equals:
                login = self._oracle_login(rest, named=True)
                if login is not None:
                    return f"{flag}={login}"
            else:
                self._login_next = True
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
            return self._unread(value)
        if self.depth < _MAX_DEPTH and any(c.isspace() for c in value):
            if value.lstrip().startswith(("{", "[")):
                return self._multi_word(value)  # JSON is data, not a command line
            return _redact_command(value, self.depth + 1)
        return self._unread(value)

    def _unread(self, value: str) -> str:
        """``value`` as the free-text scan leaves it; too deep to read word by word, one that
        holds a word that names a credential is hidden whole."""
        if self.depth >= _MAX_DEPTH and any(c.isspace() for c in value):
            return _REDACTED if _names_a_credential(value) else _scan(value)
        return _scan(value)

    def skip(self) -> None:
        """A word that was not read (too long) is the end of what a flag before it waited for."""
        self._mask_next, self._setting_next, self._user_next = False, None, False
        self._login_next = False


def _names_a_credential(text: str) -> bool:
    """Whether a word of ``text`` is a flag, a name or a setting that holds a credential."""
    return any(
        sensitive_name(word.partition("=")[0]) for word in text.split() if word.lstrip("-")[:1]
    )


def _unquote(raw: str) -> str:
    if raw.startswith(("$'", '$"')):
        raw = raw[1:]  # ``$'...'`` (ANSI-C quoting): the quoted text is the word
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


def _redact_words(text: str, depth: int) -> str:
    """``text``, the words of a command (the comments, the here-documents and the template fields
    already put aside), with the value of each credential masked."""
    words = _Words(depth)
    words.boundary()
    out: List[str] = []
    position = 0
    first = True
    for match in _SHELL_TOKEN.finditer(text):
        gap = text[position : match.start()]
        if not first and _COMMAND_BOUNDARY.search(gap):
            words.boundary()
        out.append(gap)
        first = False
        position = match.end()
        if match.group("comment") is not None:
            # a comment is text, with no quote pairing across lines, but a command that is
            # commented out still holds its credential: its words are read
            out.append("#" + _redact_words(_scan(match.group(0))[1:], depth))
            continue
        raw = match.group("word")
        value = _unquote(raw)
        shown = words.show(value)
        if shown == value:
            out.append(raw)
        else:
            quote = _NEEDS_QUOTES.search(shown.replace(_REDACTED, ""))
            out.append(shlex.quote(shown) if quote else shown)
    out.append(text[position:])
    return "".join(out)


def _redact_command(command: str, depth: int = 0) -> str:
    """``command`` with the values of credentials masked, word by word, keeping what is not
    one exactly as written (lines, quoting, operators, ``$VAR``, ``{{ ds }}``)."""
    fields: List[str] = []
    bodies: List[str] = []

    def hide(match: "re.Match[str]") -> str:
        # the field stays a reference as written, but a default it holds is read like any text
        # (``{{ var.value.get('u', 'jdbc:oracle:thin:user/password@host') }}``)
        fields.append(_scan(match.group(0)))
        return f"__fluidjinja{depth}x{len(fields) - 1}__"

    def back(match: "re.Match[str]") -> str:
        index = int(match.group(2))
        if int(match.group(1)) == depth and index < len(fields):
            return fields[index]
        return match.group(0)

    def keep_body(body: str) -> str:
        # the body of a here-document is text, read apart: an apostrophe in it is no quote
        bodies.append(body)
        return f"__fluidheredoc{depth}x{len(bodies) - 1}__"

    def body_back(match: "re.Match[str]") -> str:
        index = int(match.group(2))
        if int(match.group(1)) != depth or index >= len(bodies):
            return match.group(0)
        lines = _mask_json(_mask_dsn(bodies[index])).split("\n")
        if depth >= _MAX_DEPTH:
            return "\n".join(_scan(_mask_config_line(line)) for line in lines)
        return "\n".join(_redact_command(_mask_config_line(line), depth + 1) for line in lines)

    text = _JINJA.sub(hide, command.replace("\r\n", "\n"))
    if "<<" in text:
        text = _set_heredocs_apart(text, keep_body)
    result = _redact_words(_mask_json(_mask_dsn(text)), depth)
    if bodies:
        result = _HEREDOC_TOKEN.sub(body_back, result)
    return _JINJA_TOKEN.sub(back, result) if fields else result


class _Quotes:
    """Whether a place of a text is in quotes or in a comment, read forward and once: the here-
    documents of one text are asked about in order."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._at = 0
        self._quote = ""
        self._comment = False

    def inside(self, index: int) -> bool:
        text, quote, comment = self._text, self._quote, self._comment
        at = self._at
        while at < index:
            char = text[at]
            if comment:
                comment = char != "\n"
            elif quote == "'":
                quote = "" if char == "'" else quote
            elif char == "\\":
                at += 1  # the next character is escaped (also in double quotes)
            elif quote == '"':
                quote = "" if char == '"' else quote
            elif char in "'\"":
                quote = char
            elif char == "#" and (at == 0 or text[at - 1] in " \t\n;&|()"):
                comment = True
            at += 1
        self._at, self._quote, self._comment = at, quote, comment
        return bool(quote) or comment


def _set_heredocs_apart(text: str, keep: Callable[[str], str]) -> str:
    """``text`` with the body of each here-document (``<<EOF`` ... ``EOF``) replaced by what
    ``keep(body)`` returns. A start in quotes or in a comment is not one (a quoted script is read
    again when it is), a body has no end that is looked for again once one delimiter had none,
    and at most :data:`_MAX_HEREDOCS` are set apart."""
    out: List[str] = []
    position = 0
    missing = set()
    taken = 0
    quotes = _Quotes(text)
    for start in _HEREDOC_START.finditer(text):
        if start.start() < position or taken >= _MAX_HEREDOCS:
            continue
        delimiter = start.group(2)
        if delimiter in missing or quotes.inside(start.start()):
            continue
        ends = re.compile(r"^[ \t]*" + re.escape(delimiter) + r"[ \t]*$", re.M).search(
            text, start.end()
        )
        if ends is None:
            missing.add(delimiter)
            continue
        end = max(start.end(), ends.start() - 1)  # the newline before the terminator is the text's
        out.append(text[position : start.end()])
        out.append(keep(text[start.end() : end]))
        position = end
        taken += 1
    out.append(text[position:])
    return "".join(out)


#: A credential in a file written by a here-document: ``password = x``, ``key: x``, ``password x``
#: (a ``.netrc``), ``PASSWORD 'x'`` and ``IDENTIFIED BY 'x'`` in SQL.
_CONFIG_PAIR = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.\-]{0,64})(\s*[=:]\s*)(\S.*?)\s*$")
_SQL_SECRET = re.compile(r"""(?i)\b(IDENTIFIED\s+BY|PASSWORD)(\s+)('(?:[^']|'')*'|"[^"]*")""")


def _mask_config_line(line: str) -> str:
    """A line of the body of a here-document with the secret of a configuration or SQL line
    masked."""
    line = _SQL_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", line)
    found = _CONFIG_PAIR.match(line)
    if found and sensitive_name(found.group(2)) and not _plain_value(found.group(4)):
        return f"{found.group(1)}{found.group(2)}{found.group(3)}{_REDACTED}"
    words = line.split()
    for index in range(len(words) - 1):
        word = words[index]
        if sensitive_name(word) and not word.endswith(("=", ":")) and "=" not in word:
            if not _plain_value(words[index + 1]):
                return re.sub(
                    r"(\b" + re.escape(word) + r"\s+)\S+",
                    lambda m: m.group(1) + _REDACTED,
                    line,
                    count=1,
                )
    return line


def _too_long(what: str, size: int) -> str:
    return f"<{what} of {size:,} characters, too long to show>"


def safe_args(args: Sequence[str], limit: int = 200) -> List[str]:
    """Arguments of a submitted job, safe to write: a credential-shaped value is masked, and so is
    the value after a flag whose name is one (``--password hunter2``, ``-p hunter2``, ``--token=abc``,
    ``--conf spark.hadoop.fs.s3a.secret.key=abc``), the value of ``NAME=value`` when the name is,
    and a secret in a JSON value or a connection string. An argument too long to read whole (over
    :data:`_MAX_ARG` characters once its white space is collapsed) is not shown: only its size."""
    words = _Words()
    out: List[str] = []
    for arg in args:
        text = clean_chars(str(arg), keep="\n\t")  # the lines of a script are read as lines
        if len(text) > _MAX_ARG:
            text = " ".join(text.split())
        if len(text) > _MAX_ARG:
            words.skip()
            out.append(_too_long("an argument", len(text)))
            continue
        out.append(safe_text(words.show(text), limit))
    return out


def safe_command(command: str, limit: int = 2000) -> str:
    """A command line, safe to write: each word is looked at as :func:`safe_args` does (a word
    that holds white space, a header or a JSON value in quotes, is read whole), and the words
    that are nothing to hide stay as written, lines, operators and quoting included. A command
    over :data:`_MAX_COMMAND_TEXT` characters is not shown: only its size."""
    if len(command) > _MAX_COMMAND_TEXT:
        return _too_long("a command", len(command))
    # the characters that can hide a credential from a rule (a zero-width one inside a name or a
    # host) go before the rules read it, not after
    cleaned = clean_chars(command.replace("\r\n", "\n"), keep="\n\t")
    text = clean_chars(_redact_command(cleaned), keep="\n\t").strip()
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
