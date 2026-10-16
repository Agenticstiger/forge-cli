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

"""
.env file credential storage.

Loads credentials from .env files with environment-specific overrides.
Supports .env, .env.{environment}, and .env.local patterns.
"""

import logging
import os
import stat
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from .protected_env import (
    EndpointGroup,
    EndpointRefusal,
    endpoint_group,
    environment_is_case_insensitive,
    is_endpoint_of,
    is_protected_env_key,
    refused_project_authenticators,
    unpaired_endpoints,
)

logger = logging.getLogger(__name__)

# H17 — one-shot log gate so the "Loaded N credentials from .env files" line
# only renders once per process even when multiple credential consumers
# (connector / credential-store / catalog-resolver / …) each instantiate
# their own ``DotEnvCredentialStore``. Tests reset this via the public
# ``reset_dotenv_load_log()`` helper at the bottom of the module.
_LOGGED_CRED_COUNT: bool = False

# (name, file) pairs already reported as ignored, so the several stores one
# run builds (hydration, then each credential lookup) warn once, not once
# each, while another project hydrated by the same process is still reported.
_WARNED_PROTECTED_KEYS: Set[Tuple[str, str]] = set()

# Names a project dotenv file exported into ``os.environ`` in this process, with
# the value, so a reader that pairs an endpoint with an ambient credential of
# its own (llm/coding_agent.py) can tell a project endpoint from an operator one.
# Keyed by ``_environ_name``: the name ``os.environ`` stores the value under.
_PROJECT_EXPORTS: Dict[str, str] = {}

# Every value project dotenv files supplied after the filter, exported or not
# (an operator's blank variable of the same name wins the export, and the
# credential resolver's .env step then returns the project's value).
_PROJECT_VALUES: Dict[str, str] = {}


def _environ_name(name: str) -> str:
    """The name ``os.environ`` keys ``name`` under: upper case on Windows."""
    return name.upper() if environment_is_case_insensitive() else name


def project_supplied(name: str) -> bool:
    """Whether a project dotenv file supplied ``os.environ[name]``, and it is in effect.

    In effect as ``protected_env.unpaired_endpoints`` counts a project
    credential: not blank (readers treat a blank value as unset and fall back
    to their stored login), and not replaced by the operator's value
    (``FLUID_SECRETS_FILE`` loads after the project files and wins). Names
    compare as ``os.environ`` compares them, so on Windows a project file's
    ``codex_api_key`` is ``CODEX_API_KEY``.
    """
    value = _PROJECT_EXPORTS.get(_environ_name(name))
    return value is not None and bool(value.strip()) and os.environ.get(name) == value


def project_value_in_effect(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a reader of ``name`` gets the value project dotenv files set.

    It does when the project exported it and ``env`` (default ``os.environ``)
    still holds it, and when ``env`` holds nothing for it (unset or blank)
    while the project files set it: the credential resolver then falls back
    to those files. A value the operator set themselves, even the same one,
    is the operator's.
    """
    source = os.environ if env is None else env
    current = source.get(name)
    exported = _PROJECT_EXPORTS.get(_environ_name(name))
    if exported is not None and exported.strip() and current == exported:
        return True
    value = _PROJECT_VALUES.get(_environ_name(name))
    return bool(value and value.strip()) and not (current or "").strip()


def project_values_in_effect(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Every value project dotenv files supplied that a reader of its name gets
    (:func:`project_value_in_effect`), keyed by the name as the files spell it.

    A reader pinned to the project's own material (the dlt runner, for a
    destination whose endpoint the project set) takes its values from here.
    """
    return {
        name: _PROJECT_VALUES.get(name, _PROJECT_EXPORTS.get(name, ""))
        for name in sorted(set(_PROJECT_EXPORTS) | set(_PROJECT_VALUES))
        if project_value_in_effect(name, env)
    }


def project_endpoints_in_effect(service: str, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The endpoint variables of ``service`` (``protected_env``) whose value in
    effect came from project dotenv files, as the files spell them.

    Names match in any case, as ``protected_env`` and the readers compare
    them: pydantic-settings (the dlt bridge's credential classes) reads
    ``snowflake_account`` as ``SNOWFLAKE_ACCOUNT``, and Windows keeps one
    variable for both. Another spelling than the upper-case one is in effect
    while ``env`` (default ``os.environ``) holds the value the project exported
    under it; the upper-case name also as :func:`project_value_in_effect` has
    it, since the credential resolver falls back to the project's files for
    that name only.
    """
    group = endpoint_group(service)
    source = os.environ if env is None else env
    found: List[str] = []
    for name in sorted(set(_PROJECT_EXPORTS) | set(_PROJECT_VALUES)):
        if not is_endpoint_of(group, name):
            continue
        if name == name.upper():
            in_effect = project_value_in_effect(name, env)
        else:
            exported = _PROJECT_EXPORTS.get(name)
            in_effect = bool(exported and exported.strip()) and source.get(name) == exported
        if in_effect:
            found.append(name)
    return found


def project_endpoint_in_effect(service: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether any endpoint of ``service`` (``protected_env``) in effect came from
    project dotenv files (:func:`project_endpoints_in_effect`).

    Such an endpoint loaded because the project files also supply a credential
    for it; a reader that would otherwise find the operator's credential
    outside the environment (keyring, credentials.enc, an SDK config file)
    must then use only the project's (``credentials/resolver.py``).
    """
    return bool(project_endpoints_in_effect(service, env))


def project_exports() -> Dict[str, str]:
    """What project dotenv files exported into ``os.environ`` and is still in effect.

    Already filtered (``protected_env``): no protected name and no refused
    endpoint. A value the operator replaced (shell, ``FLUID_SECRETS_FILE``) is
    left out; it is the operator's, not the project's.
    """
    return {
        name: value for name, value in _PROJECT_EXPORTS.items() if os.environ.get(name) == value
    }


#: python-dotenv's ``load_dotenv`` does nothing while this is set (1.2.0 and later).
DOTENV_SWITCH = "PYTHON_DOTENV_DISABLED"

# Whether this process turned the switch on itself, rather than inheriting or
# loading the operator's value; only a switch FLUID set is FLUID's to leave out
# of a subprocess that runs the project's own code.
_SWITCH_SET_HERE = False


def switch_off_dotenv_loading() -> None:
    """Turn python-dotenv's ``load_dotenv`` off for this process and its children.

    dbt loads the nearest ``.env`` when it starts and litellm loads one when
    it is imported, both without the ``protected_env`` filter, so they would
    bring back what it dropped. A value the operator set (to anything) is kept.
    """
    global _SWITCH_SET_HERE
    if os.environ.get(DOTENV_SWITCH) is None:
        os.environ[DOTENV_SWITCH] = "1"
        _SWITCH_SET_HERE = True


def operator_set_dotenv_switch() -> None:
    """The operator's ``FLUID_SECRETS_FILE`` set the switch: theirs to pass on."""
    global _SWITCH_SET_HERE
    _SWITCH_SET_HERE = False


def environment_for_project_code() -> Dict[str, str]:
    """``os.environ`` for a subprocess that runs the checkout's own code.

    A python build step is the project's code: it may load a dotenv file of
    its own, and the switch would turn that into a silent no-op while
    protecting nothing. So a switch FLUID set is left out; one the operator
    set passes unchanged. Another fluid process (``fluid ship``'s stages)
    gets the same environment and turns the switch on again itself.
    """
    env = dict(os.environ)
    if _SWITCH_SET_HERE and env.get(DOTENV_SWITCH) == "1":
        del env[DOTENV_SWITCH]
    return env


def _read_project_env_files(
    env_files: List[Path],
) -> Tuple[Dict[str, Optional[str]], Dict[str, Path], Dict[str, Path]]:
    """Merge ``env_files`` in order (later wins), minus the protected names.

    Returns the merged values, for each name ``protected_env`` refuses a
    project file the file that set it, and for each merged name the file its
    value came from. Refused names are dropped here, before the caller caches
    or exports anything, so neither ``os.environ`` nor ``get_credential`` ever
    sees them.
    """
    combined: Dict[str, Optional[str]] = {}
    ignored: Dict[str, Path] = {}
    sources: Dict[str, Path] = {}
    for env_file in env_files:
        if not env_file.exists():
            continue
        _warn_if_world_readable(env_file)
        logger.debug(f"Loading credentials from {env_file.name}")
        try:
            # Literal values. python-dotenv expands ``${VAR}`` from os.environ
            # by default, which would let a project file copy an operator
            # secret into a loadable name that a contract ``{{ env.X }}`` or
            # a log line then carries somewhere. (The operator's own
            # FLUID_SECRETS_FILE is loaded elsewhere and keeps expansion.)
            values = dotenv_values(env_file, interpolate=False)
        except Exception as e:
            logger.warning(f"Failed to load {env_file}: {e}")
            continue
        for key, value in values.items():
            if is_protected_env_key(key):
                ignored[key] = env_file
            else:
                combined[key] = value
                sources[key] = env_file
    return combined, ignored, sources


def _warn_ignored_protected_keys(ignored: Dict[str, Path], already_set: FrozenSet[str]) -> None:
    """Report protected settings a project dotenv file tried to set.

    ``ignored`` maps each name to the file that set it; the message carries
    the file's base name, never a value. A name in ``already_set`` (upper-cased
    names the operator set to a value that is not blank) is skipped: the
    operator's value is the one in effect.
    """
    new = sorted(
        key
        for key, path in ignored.items()
        if key.upper() not in already_set and (key, str(path)) not in _WARNED_PROTECTED_KEYS
    )
    if not new:
        return
    _WARNED_PROTECTED_KEYS.update((key, str(ignored[key])) for key in new)
    listed = ", ".join(f"{key} ({ignored[key].name})" for key in new)
    one = len(new) == 1
    logger.warning(
        "Ignored %s from project dotenv files: %s. A project file may hold credentials, "
        "but not settings that widen a sandbox, change authentication or trust, or "
        "change where data and credentials are sent. If you meant to set %s, export "
        "%s in your shell or CI environment instead.",
        "1 setting" if one else f"{len(new)} settings",
        listed,
        "it" if one else "them",
        "it" if one else "them",
    )


# How many of one code path's pairing names a refusal warning shows (the
# others spell the same credential for another reader).
_PAIRING_NAMES_SHOWN = 3


def _warn_unpaired_endpoints(
    refused: Dict[str, EndpointRefusal], sources: Dict[str, Path], already_set: FrozenSet[str]
) -> None:
    """Report endpoints refused because they would carry an operator credential.

    Names variables and files, never values. A name the operator set to a
    value that is not blank (in ``already_set``, upper-cased) is skipped, as
    for protected settings. The advice fits the refusal: exporting the endpoint always works; adding
    a credential to the project file only where one pairs it
    (``EndpointRefusal.pairable``), so not while a switch is set or the
    operator holds a credential of the service.
    """
    new = sorted(
        key
        for key in refused
        if key.upper() not in already_set and (key, str(sources[key])) not in _WARNED_PROTECTED_KEYS
    )
    if not new:
        return
    _WARNED_PROTECTED_KEYS.update((key, str(sources[key])) for key in new)

    def either(names: Tuple[str, ...]) -> str:
        shown = names[:_PAIRING_NAMES_SHOWN]
        return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " or " + shown[-1]

    def reason(refusal: EndpointRefusal) -> str:
        if refusal.operator_credentials:
            return "would receive " + ", ".join(refusal.operator_credentials)
        text = "would receive your " + " / ".join(refusal.services) + " login or keyring credential"
        if refusal.switches:
            verb = "is" if len(refusal.switches) == 1 else "are"
            text += f" while {' and '.join(refusal.switches)} {verb} set"
        elif len(refusal.pairing) == 1:
            text += "; it pairs only with " + either(refusal.pairing[0])
        elif refusal.pairing:
            text += "; it pairs only with " + " and ".join(
                either(path) if len(path) == 1 else "one of " + either(path)
                for path in refusal.pairing
            )
        if refusal.pairs_inside and refusal.pairing and not refusal.switches:
            text += " or a password inside the connection string"
        return text

    listed = "; ".join(f"{key} ({sources[key].name}, {reason(refused[key])})" for key in new)
    one = len(new) == 1
    advice = f"Export {'it' if one else 'them'} in your shell or CI environment"
    pairable = [key for key in new if refused[key].pairable]
    if len(pairable) == len(new):
        advice += ", or put the credential in the same project file"
    elif pairable:
        advice += f", or, for {', '.join(pairable)}, put the credential in the same project file"
    logger.warning(
        "Ignored %s from project dotenv files: %s. A project file may not point a credential "
        "you hold outside the project at a host the project chose. %s.",
        "1 endpoint setting" if one else f"{len(new)} endpoint settings",
        listed,
        advice,
    )


def _warn_refused_authenticators(
    refused: Dict[str, EndpointGroup], sources: Dict[str, Path], already_set: FrozenSet[str]
) -> None:
    """Report authenticators dropped next to the project's own endpoint.

    Names variables and files, never values. A name the operator set to a
    value that is not blank (in ``already_set``, upper-cased) is skipped, as
    for protected settings.
    """
    new = sorted(
        key
        for key in refused
        if key.upper() not in already_set and (key, str(sources[key])) not in _WARNED_PROTECTED_KEYS
    )
    if not new:
        return
    _WARNED_PROTECTED_KEYS.update((key, str(sources[key])) for key in new)
    for service in sorted({refused[key].service for key in new}):
        keys = [key for key in new if refused[key].service == service]
        one = len(keys) == 1
        allowed = " or ".join(f"'{value}'" for value in refused[keys[0]].project_authenticators)
        logger.warning(
            "Ignored %s from project dotenv files: %s. Next to a %s endpoint from the project's "
            "files, a project file may set %s only to %s, which signs in with the project's own "
            "credential; other values can sign in with your identity or credentials. If "
            "you mean to sign in to that endpoint that way, export it in your shell or CI "
            "environment.",
            "1 sign-in setting" if one else f"{len(keys)} sign-in settings",
            ", ".join(f"{key} ({sources[key].name})" for key in keys),
            service,
            "it" if one else "them",
            allowed,
        )


def _warn_if_world_readable(path: Path) -> None:
    """Warn when a credential-bearing ``.env`` file is group/other-readable.

    Real secrets live in ``.env`` / ``.env.local`` — on a shared host a
    group- or other-readable mode leaks them. POSIX-only (Windows
    ``st_mode`` does not carry these permission bits).
    """
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Credential file %s is mode %o — readable by group/other. " "Run: chmod 600 %s",
            path,
            mode,
            path,
        )


try:
    from dotenv import dotenv_values, load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

    # Provide a no-op ``load_dotenv`` so callers (``cli/_common.hydrate_dotenv``)
    # that ``from fluid_build.credentials.dotenv_store import load_dotenv`` keep
    # importing cleanly when ``python-dotenv`` is absent. The helper logs a
    # debug skip in that path and returns early, so a missing optional dep is
    # graceful rather than a hard ImportError on every CLI invocation.
    def load_dotenv(*args, **kwargs):  # type: ignore[no-redef]
        return False


class DotEnvCredentialStore:
    """
    Load credentials from .env files with security best practices.

    Search order:
    1. .env (base configuration)
    2. .env.{environment} (e.g., .env.dev, .env.prod)
    3. .env.local (local overrides, highest priority)

    All values are cached and automatically loaded into os.environ
    for backward compatibility; ``credentials.protected_env`` names are dropped,
    and so is a third-party endpoint that would carry an operator credential
    (``protected_env.unpaired_endpoints``).
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        environment: Optional[str] = None,
        *,
        operator_env: Optional[Mapping[str, Optional[str]]] = None,
    ):
        """
        Initialize .env credential store.

        Args:
            project_root: Root directory to search for .env files (default: cwd)
            environment: Environment name (dev, staging, prod)
            operator_env: Operator values that are not in ``os.environ`` yet
                but will win over the project files, i.e. the operator's
                ``FLUID_SECRETS_FILE``. Their credentials count as the
                operator's when ``protected_env.unpaired_endpoints`` decides
                which project endpoints to refuse.
        """
        if not DOTENV_AVAILABLE:
            raise ImportError(
                "python-dotenv package required for .env file support. "
                "Install with: pip install python-dotenv"
            )

        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.environment = environment or os.environ.get("FLUID_ENV", "dev")
        self._cache: Optional[Dict[str, str]] = None
        self._loaded = False
        # Snapshot env vars the operator set BEFORE this store runs. ``load()``
        # uses this snapshot to decide which keys ``.env`` is allowed to push
        # into ``os.environ``: preexisting keys represent the operator's
        # explicit intent (e.g. ``set -a; . fluid.local.env; set +a``) and must
        # not be clobbered by a project ``.env`` placeholder like
        # ``DMM_API_KEY=``. The secrets-file override path in
        # ``cli._common.hydrate_dotenv`` is a separate, explicit override
        # (``load_dotenv(path, override=True)``) and still wins — that's the
        # documented way to push operator-supplied values in.
        # Upper-cased: on Windows ``pghost`` *is* ``PGHOST``, and readers such
        # as pydantic-settings (``case_sensitive=False``) treat them as one
        # name on POSIX too, so a case variant must not slip past either.
        self._preexisting_env_keys = frozenset(key.upper() for key in os.environ)
        self._operator_env: Dict[str, Optional[str]] = dict(operator_env or {})
        # What the warnings below leave out: the names whose value in effect is
        # the operator's, from the shell or the operator's FLUID_SECRETS_FILE
        # (which loads next and wins), so they never tell the operator to
        # export what they already set. A blank one is not: readers take it as
        # unset and the credential resolver falls back to the project's files,
        # so a value this store drops there is reported (a compose or CI
        # passthrough of an unset variable is blank).
        self._operator_set_keys = frozenset(
            key.upper()
            for key, value in {**os.environ, **self._operator_env}.items()
            if value and value.strip()
        )

        logger.debug(f"Initialized .env store: {self.project_root} (env: {self.environment})")

    def load(self) -> Dict[str, str]:
        """
        Load all .env files in priority order.

        Returns:
            Combined dictionary of all loaded values
        """
        if self._cache is not None:
            return self._cache

        # Load in reverse priority order (later files override earlier)
        env_files = [
            self.project_root / ".env",  # Base config
            self.project_root / f".env.{self.environment}",  # Environment-specific
            self.project_root / ".env.local",  # Local overrides (highest priority)
        ]
        combined, ignored, sources = _read_project_env_files(env_files)

        # A third-party endpoint (OPENAI_BASE_URL, PGHOST, SNOWFLAKE_ACCOUNT
        # ...) loads only next to a credential the project supplies itself;
        # otherwise the SDK would send the operator's credential to it.
        operator: Dict[str, Optional[str]] = {**os.environ, **self._operator_env}
        refused = unpaired_endpoints(combined, operator)
        for key in refused:
            del combined[key]
        # Next to such an endpoint of its own, a project file may not choose
        # an authenticator that signs in with the operator's identity
        # (SNOWFLAKE_AUTHENTICATOR=externalbrowser). Dropped here, so neither
        # os.environ nor the resolver's fallback to this store sees it.
        refused_authenticators = refused_project_authenticators(combined, operator)
        for key in refused_authenticators:
            del combined[key]

        # Export to ``os.environ`` for backward compatibility, but never
        # override an env var the operator set BEFORE this store ran. Previous
        # behavior called ``load_dotenv(path, override=True)`` per file, which
        # silently clobbered shell-set values (e.g. a valid ``DMM_API_KEY``
        # sourced from ``fluid.local.env``) with empty placeholders from
        # project ``.env``. The explicit override path for operator secrets is
        # ``cli._common.hydrate_dotenv`` (loads ``FLUID_SECRETS_FILE`` with
        # ``override=True`` AFTER this store runs) — that still works and is
        # the documented way to push operator values into the process.
        operator_sets = {
            _environ_name(key) for key, value in self._operator_env.items() if value is not None
        }
        for key, value in combined.items():
            if value is None:
                continue
            _PROJECT_VALUES[_environ_name(key)] = value
            if key.upper() in self._preexisting_env_keys:
                continue
            if _environ_name(key) in operator_sets:
                # FLUID_SECRETS_FILE sets it next: the value is the operator's.
                continue
            os.environ[key] = value
            _PROJECT_EXPORTS[_environ_name(key)] = value

        # dbt loads the nearest ``.env`` when it starts and litellm loads one
        # when it is imported, both with python-dotenv and without this
        # filter, so the refused names would come back in a dbt subprocess or
        # in this process. python-dotenv skips loading while this is set; an
        # operator who exported it (to 0 or 1) keeps their value.
        if sources or ignored:
            switch_off_dotenv_loading()

        _warn_ignored_protected_keys(ignored, already_set=self._operator_set_keys)
        _warn_unpaired_endpoints(refused, sources, already_set=self._operator_set_keys)
        _warn_refused_authenticators(
            refused_authenticators, sources, already_set=self._operator_set_keys
        )

        self._cache = combined
        self._loaded = True

        # Security: Log loaded keys (NOT values)
        if combined:
            global _LOGGED_CRED_COUNT
            if not _LOGGED_CRED_COUNT:
                # H17 — first-loader wins. Subsequent stores from the same
                # process drop to DEBUG so ``fluid auth status`` doesn't
                # echo the same banner 7× across connector / store /
                # resolver bootstraps.
                logger.info(f"Loaded {len(combined)} credentials from .env files")
                _LOGGED_CRED_COUNT = True
            else:
                logger.debug(f"Loaded {len(combined)} credentials from .env files (duplicate)")
            logger.debug(f"Available keys: {', '.join(sorted(combined.keys()))}")

        return combined

    def get_credential(self, key: str) -> Optional[str]:
        """
        Get a credential value from .env files.

        Args:
            key: Credential key (e.g., "SNOWFLAKE_PASSWORD")

        Returns:
            Credential value or None if not found
        """
        values = self.load()
        return values.get(key)

    def has_credential(self, key: str) -> bool:
        """Check if credential exists."""
        return self.get_credential(key) is not None

    @staticmethod
    def create_example_file(
        output_path: Path, credentials: Dict[str, str], provider: str = "example"
    ):
        """
        Create a .env.example file with placeholder values.

        Args:
            output_path: Path to .env.example file
            credentials: Dict of key -> description
            provider: Provider name for documentation
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {provider.upper()} Credentials\n")
            f.write("# Copy to .env and fill in your actual values\n")
            f.write("# DO NOT commit .env to Git!\n\n")

            for key, description in credentials.items():
                f.write(f"# {description}\n")
                f.write(f"{key}=your_{key.lower()}_here\n\n")

        # Defence-in-depth: the example file carries only placeholders, but
        # operators routinely ``cp .env.example .env`` and fill in real
        # secrets — start it owner-only so a copy inherits a safe mode.
        try:
            os.chmod(output_path, 0o600)
        except (NotImplementedError, OSError):
            pass

        logger.info(f"Created example file: {output_path}")


def ensure_gitignore(project_root: Path):
    """
    Ensure .env files are in .gitignore.

    Adds if not already present:
    - .env
    - .env.local
    - .env.*.local
    """
    gitignore = project_root / ".gitignore"

    entries = [".env", ".env.local", ".env.*.local"]

    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        existing_entries = set(line.strip() for line in content.splitlines())
    else:
        content = ""
        existing_entries = set()

    new_entries = [entry for entry in entries if entry not in existing_entries]

    if new_entries:
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write("\n# FLUID CLI - Environment files\n")
            for entry in new_entries:
                f.write(f"{entry}\n")

        logger.info(f"Added {len(new_entries)} entries to .gitignore")


def reset_dotenv_load_log() -> None:
    """Reset the one-shot log gates and the record of project exports.

    Used by tests that exercise the first-time log path more than once
    in a single process. Production code should not need this — the gate
    intentionally lasts for the lifetime of the process so multi-store
    bootstraps (connector / credential-store / catalog-resolver) don't
    each emit the banner.
    """
    global _LOGGED_CRED_COUNT, _SWITCH_SET_HERE
    _LOGGED_CRED_COUNT = False
    _SWITCH_SET_HERE = False
    _WARNED_PROTECTED_KEYS.clear()
    _PROJECT_EXPORTS.clear()
    _PROJECT_VALUES.clear()
