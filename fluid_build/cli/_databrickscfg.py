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

"""Remove one profile from a Databricks config file (``~/.databrickscfg``).

The file belongs to the Databricks CLI, SDK, dbt and terraform, and holds every
profile and its token, so fluid edits it the way its owner does:
``databricks auth logout <profile> --delete`` (databricks/cli
``libs/databrickscfg/ops.go``, ``DeleteProfile`` + ``ClearDefaultProfile``)
removes that one section, and a ``[__settings__] default_profile`` that names
it, and keeps everything else. Python's configparser drops comments when it
writes, so the edit here is a line-level cut that keeps every other line byte
for byte, and configparser only checks the result.
"""

from __future__ import annotations

import configparser
import io
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: The Databricks CLI's own settings section, not a profile.
SETTINGS_SECTION = "__settings__"

# A section header at column 0, read the way configparser's SECTCRE reads it:
# the name runs to the last ``]``. An indented header is left to the re-parse
# check in ``remove_profile``.
_HEADER = re.compile(r"\[(?P<name>.+)\]")
_DEFAULT_PROFILE = re.compile(r"\s*default_profile\s*[=:]\s*(?P<value>.*?)\s*$")
# The Databricks CLI and Go SDK read the file with go-ini: with
# SpaceBeforeInlineComment a value ends at " #" or " ;", and then loses one
# pair of surrounding quotes (go-ini parser.go readValue).
_INLINE_COMMENT = re.compile(r" [#;]")
_COMMENT_PREFIXES = ("#", ";")


class ConfigFileError(ValueError):
    """The file does not parse, or a cut would change another profile.

    The message never quotes the file: a malformed line can be a token.
    """


def config_path() -> str:
    """``DATABRICKS_CONFIG_FILE``, else ``~/.databrickscfg``, as the SDK and CLI resolve it."""
    return os.path.expanduser(os.environ.get("DATABRICKS_CONFIG_FILE") or "~/.databrickscfg")


def drop_profile(profile: Optional[str], logger: logging.Logger) -> bool:
    """Cut ``profile`` from the config file; False, with the reason logged, if not.

    Follows ``databricks auth logout <profile> --delete``: a profile must be
    named (an ambient ``DATABRICKS_CONFIG_PROFILE`` is only quoted in the
    hint), and an OAuth profile is left to the Databricks CLI, which clears
    its cached tokens before it drops the section. Unlike the CLI, no ``.bak``
    copy is kept: it would hold the very token logout removes.
    """
    path = config_path()
    if not os.path.exists(path):
        if profile:
            logger.error("No profile %r to remove: %s does not exist.", profile, path)
        return not profile
    # The real file, so a symlinked ~/.databrickscfg stays a link.
    real = os.path.realpath(path)
    with open(real, encoding="utf-8", newline="") as fh:
        text = fh.read()
    before = os.stat(real)
    try:
        profiles = read_profiles(text)
        if not profile and not _names(profiles):
            return True  # no profile in the file: nothing to remove
        refusal = _refusal(profile, profiles, before)
        if refusal is None and profile:
            text = remove_profile(text, profile)
    except ConfigFileError as exc:
        refusal = str(exc)
    if refusal is not None:
        logger.error("%s was not changed: %s", path, refusal)
        return False

    from fluid_build.credentials.encrypted_store import _atomic_write_bytes

    # Owner-only (0600) through a temp file and a rename, never a delete.
    _atomic_write_bytes(Path(real), text.encode("utf-8"), mode=0o600)
    logger.info(
        "Removed profile %r from %s (kept: %s). The token or secret it held stays valid "
        "until it is revoked in the Databricks workspace.",
        profile,
        path,
        ", ".join(name for name in _names(profiles) if name != profile) or "none",
    )
    return True


def _refusal(
    profile: Optional[str], profiles: Dict[str, Dict[str, str]], before: os.stat_result
) -> Optional[str]:
    """Why ``profile`` stays in a file that holds ``profiles``; None when it may go."""
    names = ", ".join(_names(profiles)) or "none"
    if not profile:
        named = os.environ.get("DATABRICKS_CONFIG_PROFILE")
        return (
            f"it holds every Databricks profile ({names}) and is shared with the "
            "Databricks CLI, SDK, dbt and terraform. Name the profile to remove: "
            f"fluid auth logout databricks {_word(named) if named else '<profile>'}"
        )
    if profile == SETTINGS_SECTION:
        return f"[{profile}] holds the Databricks CLI's settings, not a profile."
    if profile not in profiles:
        return f"it has no profile {profile!r} (profiles: {names})."
    if profiles[profile].get("auth_type") == "databricks-cli":
        # Its OAuth tokens sit in the Databricks CLI's cache, found through this
        # profile: cut the profile first and the CLI can no longer clear them.
        return (
            f"profile {profile!r} signs in through the Databricks CLI's OAuth cache. Run "
            f"`databricks auth logout --profile={shlex.quote(profile)} --delete` so its "
            "cached tokens go too."
        )
    if before.st_nlink > 1:
        # The rename replaces one name; the file's other names would keep the token.
        return f"another hard link to this file would keep [{profile}]; remove it by hand."
    if hasattr(os, "geteuid") and before.st_uid != os.geteuid():
        # The rewrite would belong to this user (root, under sudo), and the
        # owner's own tools could no longer read it.
        return "it belongs to another user; run the logout as that user."
    return None


def _word(name: str) -> str:
    """``name`` as one shell word that a command line cannot take for a flag."""
    return ("-- " if name.startswith("-") else "") + shlex.quote(name)


def _names(profiles: Dict[str, Dict[str, str]]) -> List[str]:
    return [name for name in profiles if name != SETTINGS_SECTION]


def read_profiles(text: str) -> Dict[str, Dict[str, str]]:
    """Each section of ``text`` with its own keys, in file order.

    ``[DEFAULT]`` is an ordinary profile here, as it is to the Databricks
    tools, so its keys are not copied into every other section.
    """
    parser = configparser.ConfigParser(default_section="\0", interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_file(io.StringIO(text, newline=""))
    except configparser.Error as exc:
        lineno = getattr(exc, "lineno", None)
        errors = getattr(exc, "errors", None)
        if lineno is None and errors:
            lineno = errors[0][0]
        where = f" at line {lineno}" if lineno else ""
        raise ConfigFileError(f"it does not parse ({type(exc).__name__}{where}).") from None
    return {name: dict(parser.items(name)) for name in parser.sections()}


def remove_profile(text: str, profile: str) -> str:
    """Return ``text`` without the ``[profile]`` section.

    The cut takes the header, the lines under it, and the comment lines
    directly above it, plus a ``[__settings__] default_profile`` that names
    the profile. Every other line is kept byte for byte. Raises
    ``ConfigFileError`` unless the result parses to the old profiles minus
    this one, so a layout the line scan misreads is never written.
    """
    expected = {name: keys for name, keys in read_profiles(text).items() if name != profile}
    settings = expected.get(SETTINGS_SECTION, {})
    clear_default = _value(settings.get("default_profile", "")) == profile
    if clear_default:
        del settings["default_profile"]

    lines = list(io.StringIO(text, newline=""))
    blocks = _blocks(lines)
    kept: List[str] = lines[: blocks[0][0]] if blocks else list(lines)
    for start, end, name in blocks:
        if name == profile:
            continue
        block = lines[start:end]
        if name == SETTINGS_SECTION and clear_default:
            block = [line for line in block if not _names_default(line, profile)]
        kept.extend(block)

    result = "".join(kept)
    if read_profiles(result) != expected:
        raise ConfigFileError(
            f"[{profile}] could not be cut out cleanly (an indented header, say); "
            "remove it by hand."
        )
    return result


def _blocks(lines: List[str]) -> List[Tuple[int, int, str]]:
    """``(start, end, name)`` for each column-0 section header.

    A block starts at the comment lines directly above its header and ends
    where the next block starts.
    """
    starts: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADER.match(line.rstrip())
        if match is None:
            continue
        start = index
        while start > 0 and lines[start - 1].lstrip().startswith(_COMMENT_PREFIXES):
            start -= 1
        starts.append((start, match.group("name")))
    if not starts:
        return []
    ends = [start for start, _ in starts[1:]] + [len(lines)]
    return [(start, end, name) for (start, name), end in zip(starts, ends, strict=True)]


def _names_default(line: str, profile: str) -> bool:
    match = _DEFAULT_PROFILE.match(line)
    return match is not None and _value(match.group("value")) == profile


def _value(raw: str) -> str:
    """A value as the Databricks CLI reads it: no inline comment, no quotes."""
    value = _INLINE_COMMENT.split(raw, 1)[0].strip()
    quote = value[:1]
    if quote in ("'", '"') and len(value) > 1 and value.endswith(quote):
        if quote not in value[1:-1]:
            value = value[1:-1]
    return value
