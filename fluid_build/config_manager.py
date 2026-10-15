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
Runtime hierarchical configuration for FLUID CLI.

Loads configuration from multiple sources with decreasing priority:
  1. Defaults (built-in DEFAULT_CONFIG)
  2. System config (/etc/fluid/config.yaml or C:\\ProgramData\\fluid\\config.yaml)
  3. User config (~/.fluidrc.yaml, ~/.fluid/config.yaml, ~/.config/fluid/config.yaml)
  4. Project config (.fluidrc.yaml, fluid.config.yaml in CWD)
  5. Environment variables (FLUID_*)
  6. CLI arguments (highest priority)

The project layer arrives with the checkout being built, so it is not trusted
like the operator's own layers: its ``${VAR}`` placeholders are not filled from
the environment, and a catalog endpoint it names is never paired with a
credential from the environment or the user config (a credential the file sets
to anything but a non-empty string, ``""``, null, ``false``, ``0``, ``[]``, is
no credential of its own): asked for such a block,
:meth:`FluidConfig.get_catalog_config` raises :class:`UntrustedCatalogEndpoint`
(see there).

For compile-time static defaults (RUN_STATE_DIR, DEFAULT_REGION, etc.),
use ``fluid_build.config_defaults`` instead — NOT this module.
"""

from __future__ import annotations

import copy
import datetime
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import yaml

from .errors import ConfigurationError, FileSystemError

LOGGER = logging.getLogger("fluid.config")


# Default configuration
DEFAULT_CONFIG = {
    "logging": {"level": "INFO", "format": "text", "file": None, "console": True},  # text or json
    "cache": {"dir": "~/.fluid/cache", "schema_cache_days": 7, "auto_refresh": True},
    "network": {
        "timeout": 30,
        "connect_timeout": 10,
        "max_retries": 3,
        "rate_limit": 50,  # requests per minute
        "verify_ssl": True,
    },
    "validation": {"strict": False, "offline": False, "schema_version": None},  # auto-detect
    "apply": {
        "dry_run": False,
        "parallel_phases": True,
        "rollback_strategy": "immediate",
        "timeout_minutes": 60,
    },
    "providers": {
        "gcp": {"default_region": "us-central1", "default_location": "US"},
        "aws": {"default_region": "us-east-1"},
        "snowflake": {"default_warehouse": "COMPUTE_WH"},
    },
    "catalogs": {
        "fluid-command-center": {
            "endpoint": "http://localhost:8000",
            "auth": {"type": "api_key"},
            "enabled": True,
            "max_retries": 3,
            "timeout": 30.0,
            "circuit_breaker_threshold": 3,
            "circuit_breaker_timeout": 60,
        }
    },
    "output": {
        "format": "text",  # text, json, yaml
        "color": True,
        "verbose": False,
        "quiet": False,
    },
}


# Every name ``fluid publish --target`` accepts for the Command Center. They
# must match the keys that map to ``FluidCommandCenterProvider`` in
# ``providers/catalogs/__init__.py::CATALOG_PROVIDERS`` (a test pins that); the
# list lives here because importing the provider registry pulls in httpx, which
# this module must not. An alias with no config block of its own reads the
# canonical ``fluid-command-center`` block.
COMMAND_CENTER_CATALOG_NAMES = ("fluid-command-center", "fluid_cc", "command-center")
COMMAND_CENTER_CANONICAL_NAME = "fluid-command-center"


# Config layers, lowest priority first. ``project`` is read from the working
# directory, i.e. from the checkout being built; every other layer is the
# operator's own.
PROJECT_LAYER = "project"
_ENV_LAYER = "env"
_CLI_LAYER = "cli"

# What makes a key in a catalog block look like a credential, wherever it sits:
# a part of its name (``client_secret``, ``api_token``, ``extra_headers.X-Api-Key``),
# or a word squashed into it (``apiKey``, ``accessToken``). Every key under
# ``auth`` except ``auth.type`` is one too. See :func:`_names_a_credential`. It
# decides only what happens to a value the project file itself blanks; what an
# endpoint from the project config gets from the operator is the allow-list
# below, not this list, which can never know every secret's name.
_CREDENTIAL_WORDS = (
    "token",
    "secret",
    "passw",
    "passphrase",
    "credential",
    "creds",
    "cookie",
    "signature",
    "apikey",
    "privatekey",
    "privkey",
    "accesskey",
    "clientkey",
    "bearer",
    "jwt",
    "jaas",
    "hmac",
    "session",
)
_CREDENTIAL_PARTS = frozenset(
    {"key", "auth", "authorization", "pwd", "pass", "pat", "pin", "header", "headers", "dsn", "sas"}
)
# Everything a catalog block whose endpoint the project config named may still
# take from the operator's layers (system and user config, the environment, the
# command line), with the types it may have: which organization, how DMM renders
# the product and its lineage and whether it approves access agreements (the
# settings the Data Mesh Manager provider README puts in ~/.fluid/config.yaml),
# how the auth block authenticates (not with what), and how hard to retry.
# Default-deny: every other value from those layers, under any name, at any
# depth, is withheld from such an endpoint. A plug-in backend's registrar gets
# the whole block and its ``env_vars`` may fill any key, so no list of secret
# names could say what is safe to send. None of these is a secret or chooses
# a host.
_OPERATOR_SETTINGS: Dict[Tuple[str, ...], Tuple[type, ...]] = {
    ("organization_id",): (str,),
    ("organization",): (str,),
    ("data_product_specification",): (str,),
    ("provider_hint",): (str,),
    ("odps_lineage_mode",): (str,),
    ("auto_approve_access",): (bool,),
    ("auth", "type"): (str,),
    ("enabled",): (bool,),
    ("force_contract_sync",): (bool,),
    ("timeout",): (int, float),
    ("max_retries",): (int,),
    ("retry_delay",): (int, float),
    ("circuit_breaker_threshold",): (int,),
    ("circuit_breaker_timeout",): (int, float),
}
# The env vars ``providers.common.get_auth_headers`` falls back to when the
# auth block names a type but carries no value for it.
_AUTH_ENV_FALLBACK = {
    "api_key": ("api_key", "FLUID_API_KEY"),
    "bearer": ("token", "FLUID_BEARER_TOKEN"),
}
# Where ``get_auth_headers`` reads the Command Center credential, by auth type.
# Every other catalog is a registrar backend, which reads ``pick_token``'s slots.
_COMMAND_CENTER_CREDENTIAL_SLOTS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    **{auth_type: (("auth", field),) for auth_type, (field, _env) in _AUTH_ENV_FALLBACK.items()},
    "basic": (("auth", "password"),),
}

_KeyPath = Tuple[Any, ...]


@dataclass(frozen=True)
class WithheldCredentials:
    """A catalog endpoint from the project config, and the credentials it did not get."""

    catalog: str
    endpoint: str
    config_file: str
    credentials: Tuple[str, ...]

    def message(self) -> str:
        from fluid_build.observability.reporter import endpoint_origin

        if self.catalog in COMMAND_CENTER_CATALOG_NAMES:
            how = "set FLUID_CC_ENDPOINT"
        elif self.catalog in ("datamesh-manager", "datamesh_manager", "entropy-data", "dmm"):
            how = "set DMM_API_URL"
        else:
            how = "set the endpoint through its environment variable"
        names = " and ".join(self.credentials)
        return (
            f"not sending {names} to {endpoint_origin(self.endpoint)}: "
            f"that {self.catalog} endpoint comes from the project config {self.config_file}, "
            "and a config file in the checkout only gets credentials it carries itself. "
            f"To use this endpoint, {how}, or put it in ~/.fluid/config.yaml"
        )


class UntrustedCatalogEndpoint(Exception):
    """A catalog endpoint from the project config would need the operator's credentials.

    Raised by :meth:`FluidConfig.get_catalog_config` instead of returning the
    block with those credentials stripped: every consumer that got such a block
    had to remember not to refill the credential from the environment, and one
    that did not sent it to the checkout's endpoint. ``str()`` says what to do.
    """

    def __init__(self, withheld: WithheldCredentials):
        super().__init__(withheld.message())
        self.withheld = withheld


class CatalogConfig(Dict[str, Any]):
    """A resolved catalog block: a plain dict to every consumer, plus where its endpoint came from.

    ``project_endpoint_file`` names the project config file (relative to the
    working directory) that set the endpoint, or is ``None`` when the endpoint
    is the operator's (system or user config, environment, command line). An
    endpoint from the checkout gets no ``{{ env.* }}`` values from the
    contract and must pass the host gate (``cli/publish.py``).
    """

    project_endpoint_file: Optional[str] = None


def without_aliases(value: Any, origin: Optional[str] = None) -> Any:
    """A ``yaml.safe_load`` result as a tree in which every path holds its own copy.

    A YAML alias (``*name``), and every value a merge key (``<<: *name``)
    brings in, is the anchor's own object. Merged by reference, the two paths
    stayed one dict, so a later layer that wrote under one name wrote under
    both, while the trust rule, which knows each value by its path, credited
    the other path to the layer that set it first. ``copy.deepcopy`` keeps
    that sharing (its memo maps one object to one copy), so each occurrence is
    copied on its own here.

    Copied out, nested aliases grow exponentially (a few hundred bytes of
    ``l2: [*l1, *l1, ...]``, ``l3: [*l2, ...]`` would be hundreds of millions
    of values), so the copy keeps go-yaml v3's alias budget
    (:func:`_allowed_alias_ratio`) and refuses a document past it, as it does
    a recursive alias, which no config can mean: both raise a YAML error that
    names ``origin``, the file the value was read from.
    """
    return _Unaliasing(origin).copy(value)


def load_config_file(stream: Union[str, IO[str]], origin: Optional[str]) -> Any:
    """One config file, read as ``yaml.safe_load`` reads it, through :func:`without_aliases`.

    The file is parsed by :class:`_BudgetedLoader`, which spends the alias
    budget on what a merge key (``<<:``) copies while the loader flattens it,
    and the result is copied by :func:`without_aliases`, which spends the same
    budget on what an alias copies out. Past either, a YAML error names
    ``origin``. An empty file is ``{}``.
    """
    loader = _BudgetedLoader(stream, origin)
    try:  # the body of ``yaml.load``
        loaded = loader.get_single_data()
    finally:
        loader.dispose()
    return without_aliases(loaded or {}, origin)


# go-yaml v3's "document contains excessive aliasing" rule, adapted from
# decode.go (allowedAliasRatio, and the check that opens decoder.unmarshal):
# https://github.com/go-yaml/yaml/blob/v3.0.1/decode.go
# Copyright (c) 2011-2019 Canonical Ltd, Apache License 2.0. Every value
# copied counts as one decode, and every value copied for a repeated
# occurrence of a mapping or list (what an alias brings in) as one alias
# decode. Past 1,000 decodes and 100 alias decodes, the alias decodes may make
# up at most 99% of the decodes up to 400,000, a share that falls linearly to
# 10% at 4,000,000: the copy stays within a fixed multiple of what the parser
# built, however deep the aliases nest.
_ALIAS_RATIO_RANGE_LOW = 400_000
_ALIAS_RATIO_RANGE_HIGH = 4_000_000


def _allowed_alias_ratio(decodes: int) -> float:
    """The share of ``decodes`` that may come from aliases (go-yaml's ``allowedAliasRatio``)."""
    if decodes <= _ALIAS_RATIO_RANGE_LOW:
        return 0.99  # small-to-medium documents
    if decodes >= _ALIAS_RATIO_RANGE_HIGH:
        return 0.10  # very large documents
    span = _ALIAS_RATIO_RANGE_HIGH - _ALIAS_RATIO_RANGE_LOW
    return 0.99 - 0.89 * (decodes - _ALIAS_RATIO_RANGE_LOW) / span


class _AliasBudget:
    """go-yaml v3's alias budget for one file: its decodes, its alias decodes, and the limit."""

    def __init__(self, origin: Optional[str]) -> None:
        self.origin = origin
        self.decodes = 0
        self.alias_decodes = 0

    def error(self, reason: str) -> yaml.YAMLError:
        """A YAML error that names the file."""
        return yaml.YAMLError(f"{self.origin}: {reason}" if self.origin else reason)

    def spend(self, decodes: int, aliased: bool) -> None:
        """Count ``decodes`` decodes, all of them alias decodes if ``aliased``; refuse past the budget.

        A run of alias decodes is checked once, after its last: along the run
        their share only rises while the share allowed only falls, so that
        check fails exactly when a check after any one of them would.
        """
        self.decodes += decodes
        if aliased:
            self.alias_decodes += decodes
        if (
            self.alias_decodes > 100
            and self.decodes > 1000
            and self.alias_decodes / self.decodes > _allowed_alias_ratio(self.decodes)
        ):
            raise self.error(
                f"document contains excessive aliasing: {self.alias_decodes:,} of the first "
                f"{self.decodes:,} values it expands to repeat an anchor (*name, <<: *name); "
                "a config file is refused rather than expanded further"
            )


class _BudgetedLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` whose merge keys spend the file's alias budget (:class:`_AliasBudget`).

    SafeLoader flattens a merge key (``<<: *name``, ``<<: [*a, *b]``) by
    copying the merged mapping's key/value pairs into the mapping that merges
    it, after flattening that mapping's own merge keys, and before anything is
    constructed. Nested repeated merges (``m2: {<<: [*m1, *m1, ...]}``,
    ``m3: {<<: [*m2, ...]}``) so grow ninefold a level inside the loader, and
    :func:`without_aliases` only sees the few keys left once the duplicates
    collapse. Here each node the file writes is one decode, each pair a merge
    key copies is one more, and an alias decode when the mapping it comes from
    is one an alias in the file refers to; the budget is checked before the
    pairs are copied.
    """

    def __init__(self, stream: Union[str, IO[str]], origin: Optional[str]) -> None:
        super().__init__(stream)
        self.budget = _AliasBudget(origin)
        self.aliased: Set[yaml.Node] = set()  # the nodes an alias in the file refers to
        self.merging = False  # flattening a mapping that a merge key copies

    def compose_node(self, parent: Optional[yaml.Node], index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            node = super().compose_node(parent, index)
            self.aliased.add(node)
            return node
        self.budget.spend(1, aliased=False)
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # SafeConstructor.flatten_mapping (PyYAML 6.0.3, lib/yaml/constructor.py,
        # MIT) flattens each mapping a merge key names by calling this method on
        # it, right before it copies that mapping's pairs: that call spends them.
        merging, self.merging = self.merging, True
        try:
            super().flatten_mapping(node)
        finally:
            self.merging = merging
        if merging:
            self.budget.spend(len(node.value), aliased=node in self.aliased)


class _Unaliasing:
    """One :func:`without_aliases` copy, and the alias budget it spends."""

    def __init__(self, origin: Optional[str]) -> None:
        self.budget = _AliasBudget(origin)
        self.met: Set[int] = set()  # every mapping or list met so far
        self.ancestors: Set[int] = set()  # the ones being copied: an alias to one loops
        self.alias_depth = 0

    def copy(self, value: Any) -> Any:
        self.budget.spend(1, aliased=self.alias_depth > 0)
        if not isinstance(value, (dict, list, tuple)):
            return value  # a scalar, or a ``!!set`` of hashable (immutable) keys
        key = id(value)
        if key in self.ancestors:
            raise self.budget.error("a YAML alias refers to the mapping or list that contains it")
        repeated = key in self.met
        self.met.add(key)
        self.ancestors.add(key)
        if repeated:
            self.alias_depth += 1
        try:
            if isinstance(value, dict):
                copied: Dict[Any, Any] = {}
                for k, v in value.items():
                    # the key, as go-yaml decodes a key node
                    self.budget.spend(1, aliased=self.alias_depth > 0)
                    copied[k] = self.copy(v)
                return copied
            items = [self.copy(v) for v in value]
            return items if isinstance(value, list) else tuple(items)
        finally:
            if repeated:
                self.alias_depth -= 1
            self.ancestors.discard(key)


def _set_auth_field(catalog_config: Dict[str, Any], field: str, value: str) -> _KeyPath:
    """Set ``catalog_config['auth'][field] = value``, creating the dict if absent."""
    if not isinstance(catalog_config.get("auth"), dict):
        catalog_config["auth"] = {}
    catalog_config["auth"][field] = value
    return ("auth", field)


def _first_env(*names: str) -> Optional[str]:
    """Return the first non-empty environment variable from *names* (or None)."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _apply_catalog_env_overrides(
    catalog_name: str, catalog_config: Dict[str, Any]
) -> List[_KeyPath]:
    """Apply per-catalog environment-variable overrides in place.

    Returns the key path of every value it wrote: the environment's layer,
    told by where it wrote rather than by which objects changed.

    Two paths:

    1. Native async providers (``fluid-command-center``,
       ``datamesh-manager``) — their env vars target nested ``auth.*``
       config keys and toggle provider-specific behaviour (ODCS / ODPS
       selection, provider hints). Hard-coded here because their
       config shape isn't shared with the plug-in backends.

    2. Plug-in catalog backends declared via
       :func:`~fluid_build.api.catalog_backend.register_catalog_backend`
       — every accepted env var is declared in the backend spec next
       to the registrar. We delegate to
       :func:`~fluid_build.api.catalog_backend.apply_env_overrides`
       so adding a new backend never requires editing this function.
    """
    written: List[_KeyPath] = []

    def _set(key: str, value: str) -> None:
        catalog_config[key] = value
        written.append((key,))

    if catalog_name in COMMAND_CENTER_CATALOG_NAMES:
        endpoint = _first_env("FLUID_CC_ENDPOINT", "FLUID_CATALOG_FLUID_CC_URL")
        if endpoint:
            _set("endpoint", endpoint)
        api_key = _first_env("FLUID_API_KEY", "FLUID_CATALOG_FLUID_CC_TOKEN")
        if api_key:
            written.append(_set_auth_field(catalog_config, "api_key", api_key))
        # The organization the asset is created in (sent as X-Organization-Id).
        # Stripped first: a blank CI parameter is "not set", and must not
        # replace an organization_id from the config file.
        org_id = (_first_env("FLUID_CC_ORG_ID") or "").strip()
        if org_id:
            _set("organization_id", org_id)
        return written

    if catalog_name in ("datamesh-manager", "entropy-data", "dmm"):
        endpoint = _first_env("DMM_API_URL", "FLUID_CATALOG_DMM_URL")
        if endpoint:
            _set("endpoint", endpoint)
        api_key = _first_env("DMM_API_KEY", "FLUID_CATALOG_DMM_TOKEN")
        if api_key:
            written.append(_set_auth_field(catalog_config, "api_key", api_key))
        dps = os.environ.get("DMM_DATA_PRODUCT_SPECIFICATION")
        if dps:
            _set("data_product_specification", dps)
        provider_hint = os.environ.get("DMM_PROVIDER_HINT")
        if provider_hint:
            _set("provider_hint", provider_hint)
        return written

    # Plug-in backend — declarative spec owns the env-var mapping.
    try:
        # Importing the registrar package triggers register_catalog_backend
        # side effects; without this the spec lookup misses for callers
        # that talk to config_manager before any provider is instantiated.
        import fluid_build.build_runners.catalog_registrars  # noqa: F401
        from fluid_build.api.catalog_backend import apply_env_overrides
    except Exception:  # pragma: no cover — defensive against import-time failure
        return written
    return [(key,) for key in apply_env_overrides(catalog_name, catalog_config)]


class FluidConfig:
    """
    Hierarchical configuration manager for FLUID CLI.

    Configuration is loaded from multiple sources in order:
    1. Defaults (lowest priority)
    2. System config
    3. User config
    4. Project config
    5. Environment variables
    6. CLI arguments (highest priority)
    """

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._sources = _LayerSources()
        self._project_file: Optional[str] = None  # relative to the working directory
        # The system and user files read, so that the same file is never read
        # again as the project layer (cwd == $HOME).
        self._operator_files: List[Path] = []
        self._unexpanded_warned: Set[_KeyPath] = set()
        self._credential_type_warned: Set[_KeyPath] = set()
        self._load_defaults()
        self._load_system_config()
        self._load_user_config()
        self._load_project_config()
        self._load_env_vars()

    def _load_defaults(self) -> None:
        """Load default configuration.

        Deep-copy, not ``.copy()``: a shallow copy shares the nested dicts
        (``logging``, etc.) with the module-level ``DEFAULT_CONFIG``, so a
        later ``set("logging.level", ...)`` or config-file merge would
        mutate the process-wide defaults in place — corrupting every other
        ConfigManager (and leaking across tests, which is how xdist surfaced
        ``test_defaults_loaded`` seeing a polluted ``DEBUG``)."""
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        LOGGER.debug("Loaded default configuration")

    def _load_system_config(self) -> None:
        """Load system-wide configuration."""
        if os.name == "nt":
            # Windows: C:\ProgramData\fluid\config.yaml
            system_config = (
                Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "fluid" / "config.yaml"
            )
        else:
            # Unix: /etc/fluid/config.yaml
            system_config = Path("/etc/fluid/config.yaml")

        self._merge_config_file(system_config, "system")

    def _load_user_config(self) -> None:
        """Load user configuration."""
        home = Path.home()

        # Try multiple locations
        user_configs = [
            home / ".fluidrc.yaml",
            home / ".fluidrc",
            home / ".fluid" / "config.yaml",
            home / ".config" / "fluid" / "config.yaml",
        ]

        for config_path in user_configs:
            if config_path.exists():
                self._merge_config_file(config_path, "user")
                break

    def _load_project_config(self) -> None:
        """Load project-specific configuration from current directory."""
        cwd = Path.cwd()

        # Try multiple filenames
        project_configs = [
            cwd / ".fluidrc.yaml",
            cwd / ".fluidrc",
            cwd / "fluid.config.yaml",
            cwd / ".fluid" / "config.yaml",
        ]

        for config_path in project_configs:
            if config_path.exists():
                if any(_same_file(config_path, own) for own in self._operator_files):
                    # Run from $HOME, ``.fluid/config.yaml`` is the user config
                    # itself: the operator's, already read as such.
                    LOGGER.debug(
                        "%s is the operator's own config, not a project config", config_path
                    )
                    break
                self._project_file = str(config_path.relative_to(cwd))
                self._merge_config_file(config_path, PROJECT_LAYER)
                break

    def _load_env_vars(self) -> None:
        """Load configuration from environment variables."""
        env_mappings = {
            # Logging
            "FLUID_LOG_LEVEL": ("logging", "level"),
            "FLUID_LOG_FORMAT": ("logging", "format"),
            "FLUID_LOG_FILE": ("logging", "file"),
            # Cache
            "FLUID_CACHE_DIR": ("cache", "dir"),
            # Network
            "FLUID_TIMEOUT": ("network", "timeout"),
            "FLUID_MAX_RETRIES": ("network", "max_retries"),
            "FLUID_RATE_LIMIT": ("network", "rate_limit"),
            # Providers
            "GCP_PROJECT_ID": ("providers", "gcp", "project_id"),
            "GCP_REGION": ("providers", "gcp", "default_region"),
            "AWS_REGION": ("providers", "aws", "default_region"),
            "SNOWFLAKE_ACCOUNT": ("providers", "snowflake", "account"),
            # Output
            "FLUID_OUTPUT_FORMAT": ("output", "format"),
            "FLUID_VERBOSE": ("output", "verbose"),
            "NO_COLOR": ("output", "color"),  # Inverse
        }

        for env_var, config_path in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                # Special handling for NO_COLOR
                if env_var == "NO_COLOR":
                    value = False if value else True
                # Convert string booleans
                elif value.lower() in ("true", "yes", "1"):
                    value = True
                elif value.lower() in ("false", "no", "0"):
                    value = False
                # Convert numeric strings
                elif value.isdigit():
                    value = int(value)

                self._set_nested(self._config, config_path, value)
                self._sources.record(config_path, _ENV_LAYER)
                LOGGER.debug(f"Loaded config from env var {env_var}: {config_path} = {value}")

    def _merge_config_file(self, path: Path, source: str) -> None:
        """
        Merge configuration from a YAML file.

        Args:
            path: Path to config file
            source: Source description for logging
        """
        if not path.exists():
            return

        try:
            with open(path, encoding="utf-8") as f:
                file_config = load_config_file(f, str(path))

            self._sources.record_merge(self._config, file_config, source)
            self._deep_merge(self._config, file_config)
            if source != PROJECT_LAYER:
                self._operator_files.append(path)
            LOGGER.debug(f"Loaded {source} configuration from {path}")

        except yaml.YAMLError as e:
            raise ConfigurationError(
                f"Failed to parse {source} configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=[
                    "Check YAML syntax",
                    f"Validate file with: yamllint {path}",
                    "Remove or fix the configuration file",
                ],
                original_error=e,
            )
        except Exception as e:
            raise FileSystemError(
                f"Failed to read {source} configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=["Check file permissions", "Verify file is readable"],
                original_error=e,
            )

    def _deep_merge(self, base: Dict, override: Dict) -> None:
        """
        Deep merge override dict into base dict.

        Args:
            base: Base dictionary to merge into
            override: Dictionary to merge from
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _set_nested(self, d: Dict, path: tuple, value: Any) -> None:
        """
        Set a nested dictionary value using a path tuple.

        Args:
            d: Dictionary to modify
            path: Tuple of keys representing the path
            value: Value to set
        """
        for key in path[:-1]:
            if key not in d:
                d[key] = {}
            d = d[key]
        d[path[-1]] = value

    def get(self, path: str, default: Any = None) -> Any:
        """
        Get a configuration value using dot notation.

        Args:
            path: Configuration path (e.g., 'logging.level')
            default: Default value if path doesn't exist

        Returns:
            Configuration value or default

        Examples:
            >>> config.get('logging.level')
            'INFO'
            >>> config.get('providers.gcp.default_region')
            'us-central1'
        """
        keys = path.split(".")
        value = self._config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def set(self, path: str, value: Any) -> None:
        """
        Set a configuration value using dot notation.

        Args:
            path: Configuration path (e.g., 'logging.level')
            value: Value to set

        Examples:
            >>> config.set('logging.level', 'DEBUG')
            >>> config.set('providers.gcp.project_id', 'my-project')
        """
        keys = path.split(".")
        self._set_nested(self._config, tuple(keys), value)
        self._sources.record(tuple(keys), _CLI_LAYER)

    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Get an entire configuration section.

        Args:
            section: Section name (e.g., 'logging', 'providers.gcp')

        Returns:
            Dictionary of configuration values
        """
        value = self.get(section, {})
        return value if isinstance(value, dict) else {}

    def get_catalog_config(
        self, catalog_name: Optional[str] = None, *, endpoint: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get catalog configuration.

        ``${VAR}`` is expanded except in values from the project config: the
        checkout is not the operator's.

        Args:
            catalog_name: Specific catalog name (e.g., 'fluid-command-center')
                         If None, returns all catalog configurations
            endpoint: An endpoint the operator passed on the command line
                (``fluid publish --target name:endpoint``); it is trusted.

        Returns:
            Catalog configuration dictionary; for one catalog, a
            :class:`CatalogConfig` that says whether the checkout named its endpoint

        Raises:
            UntrustedCatalogEndpoint: the endpoint comes from the project config
                and a credential for it would come from the environment or the
                user config. Nothing is returned to send, so no consumer can
                refill the credential from the environment on its own.
        """
        if catalog_name:
            catalog_config, withheld = _resolve_catalog(self, catalog_name, endpoint)
            if withheld is not None:
                raise UntrustedCatalogEndpoint(withheld)
            return catalog_config

        catalogs = self.get_section("catalogs")
        return {
            name: _CatalogLayers(self, ("catalogs", name), {}).expand(copy.deepcopy(block))
            for name, block in catalogs.items()
        }

    def to_dict(self) -> Dict[str, Any]:
        """
        Get the complete configuration as a dictionary.

        Returns:
            Complete configuration dictionary
        """
        return self._config.copy()

    def update_from_args(self, **kwargs) -> None:
        """
        Update configuration from command-line arguments.

        This should be called after parsing CLI arguments to give them
        highest priority.

        Args:
            **kwargs: Configuration key-value pairs

        Examples:
            >>> config.update_from_args(verbose=True, log_level='DEBUG')
        """
        mapping = {
            "verbose": "output.verbose",
            "quiet": "output.quiet",
            "log_level": "logging.level",
            "log_file": "logging.file",
            "cache_dir": "cache.dir",
            "timeout": "network.timeout",
            "max_retries": "network.max_retries",
            "dry_run": "apply.dry_run",
            "offline": "validation.offline",
            "strict": "validation.strict",
            "format": "output.format",
            "color": "output.color",
        }

        for arg_name, value in kwargs.items():
            if value is None:
                continue

            config_path = mapping.get(arg_name, arg_name)
            self.set(config_path, value)
            LOGGER.debug(f"Updated config from CLI arg {arg_name}: {config_path} = {value}")

    def save_user_config(self, path: Optional[Path] = None) -> None:
        """
        Save current configuration to user config file.

        Args:
            path: Optional custom path (default: ~/.fluid/config.yaml)

        Raises:
            FileSystemError: If unable to write config file
        """
        if path is None:
            path = Path.home() / ".fluid" / "config.yaml"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)

            LOGGER.info(f"Saved user configuration to {path}")

        except Exception as e:
            raise FileSystemError(
                "Failed to save configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=[
                    "Check directory permissions",
                    f"Manually create directory: mkdir -p {path.parent}",
                ],
                original_error=e,
            )


class _LayerSources:
    """Which config layer set each key path.

    A layer that writes a path as a whole replaces what earlier layers set
    below it; a path with no entry for itself or any prefix is a default.
    """

    def __init__(self) -> None:
        self._by_path: Dict[_KeyPath, str] = {}

    def record(self, path: _KeyPath, source: str) -> None:
        n = len(path)
        for known in [p for p in self._by_path if len(p) > n and p[:n] == path]:
            del self._by_path[known]
        self._by_path[tuple(path)] = source

    def record_merge(self, base: Any, override: Any, source: str, prefix: _KeyPath = ()) -> None:
        """Record what ``FluidConfig._deep_merge(base, override)`` is about to write."""
        if not isinstance(override, dict):
            return
        for key, value in override.items():
            if (
                isinstance(base, dict)
                and isinstance(base.get(key), dict)
                and isinstance(value, dict)
            ):
                self.record_merge(base[key], value, source, prefix + (key,))
            else:
                self.record(prefix + (key,), source)

    def source_of(self, path: _KeyPath) -> str:
        """``default``, ``system``, ``user``, ``project``, ``env`` or ``cli``."""
        for end in range(len(path), 0, -1):
            source = self._by_path.get(path[:end])
            if source is not None:
                return source
        return "default"


def _same_file(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` are one file: by device and inode, else by
    resolved path (case-insensitively where the file system usually is)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        pass

    def _key(path: Path) -> str:
        resolved = os.path.normcase(os.path.realpath(path))
        return resolved.casefold() if sys.platform in ("darwin", "win32") else resolved

    return _key(a) == _key(b)


def _leaves(value: Any, rel: _KeyPath = ()) -> Iterator[Tuple[_KeyPath, Any]]:
    """Every value under ``value`` that is not a non-empty dict or list, with
    its key path (a list item's index is part of the path).

    An empty dict or list is a leaf: the key over it is what it carries
    (``tokens: {<secret>: {}}``), so it is withheld like any other value.
    """
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from _leaves(v, rel + (k,))
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            yield from _leaves(v, rel + (i,))
    elif rel:
        yield rel, value


def _withheld_name(path: _KeyPath, value: Any) -> str:
    """How a refusal names the withheld ``value`` at ``path``: by its key path,
    never by the value. Below the block's top level, the key over an empty map
    or list is the value's only content, so it is not printed either."""
    parts = [str(p) for p in path]
    if len(path) > 1 and isinstance(value, (dict, list)) and not value:
        parts[-1] = f"<a key over an empty {'map' if isinstance(value, dict) else 'list'}>"
    return ".".join(parts)


def _operator_setting(rel: _KeyPath, value: Any) -> bool:
    """Whether a value at ``rel`` from the operator's layers may go to an
    endpoint the project config named (:data:`_OPERATOR_SETTINGS`)."""
    types = _OPERATOR_SETTINGS.get(tuple(rel))
    return types is not None and isinstance(value, types)


def _whole_list(block: Dict[str, Any], rel: _KeyPath) -> _KeyPath:
    """``rel`` cut at the first list on its way down ``block``.

    One layer sets a list as a whole (a merge replaces it), so what is withheld
    from a list is the list.
    """
    node: Any = block
    for i, part in enumerate(rel):
        if isinstance(node, list):
            return rel[:i]
        node = node.get(part) if isinstance(node, dict) else None
    return rel


def _would_expand(value: Any) -> bool:
    if isinstance(value, str):
        return os.path.expandvars(value) != value
    if isinstance(value, list):
        return any(_would_expand(v) for v in value)
    if isinstance(value, dict):
        return any(_would_expand(v) for v in value.values())
    return False


def _names_a_credential(rel: _KeyPath) -> bool:
    """Whether the key path ``rel`` inside a catalog block holds a credential.

    Decided by name, so that it covers every key a consumer may send, not only
    the ones ``pick_token`` reads: a plug-in backend's registrar gets the whole
    block. ``auth.type`` says how to authenticate, not with what.
    """
    if tuple(rel) == ("auth", "type"):
        return False
    for part in rel:
        name = str(part).lower()
        if any(word in re.sub(r"[^a-z0-9]", "", name) for word in _CREDENTIAL_WORDS):
            return True
        if _CREDENTIAL_PARTS.intersection(re.split(r"[^a-z0-9]+", name)):
            return True
    return False


def _blank(value: Any) -> bool:
    """Anything but a non-empty string: a credential slot that carries nothing.

    Not only ``None``, ``""`` and whitespace: ``false``, ``0``, ``no`` / ``off``
    (YAML booleans), ``[]``, ``{}``, ``!!binary ""`` and every other type too.
    A provider's env fallback (``value or os.getenv(...)``) reads each falsy one
    as missing, and a header value has to be a string anyway.
    """
    return not (isinstance(value, str) and value.strip())


# A whole value that is one ``${NAME}`` reference. ``os.path.expandvars``
# leaves it as written when NAME is unset (posixpath's ``_varprog``,
# https://github.com/python/cpython/blob/3.12/Lib/posixpath.py).
_BARE_PLACEHOLDER = re.compile(r"\$\{\w+\}", re.ASCII)


def _carries_no_operator_value(value: Any, raw: Any) -> bool:
    """Whether an operator layer's ``value`` (``raw`` before ``${VAR}``
    expansion) holds nothing: ``None``, a blank string, or a bare ``${NAME}``
    left as written because NAME is unset.

    The provider README's ``auth.api_key: ${DMM_API_KEY}`` is one of these
    when DMM_API_KEY is unset or empty. Narrower than :func:`_blank`: an empty
    map or list may hold a secret in its key, and a ``$word`` with no braces,
    or a reference with text around it, may be a literal secret.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if not value.strip():
        return True
    return value == raw and _BARE_PLACEHOLDER.fullmatch(value.strip()) is not None


def _scalar_kind(value: Any) -> Optional[str]:
    """``"number"`` or ``"date"`` for a YAML scalar that main sent as a credential's string form.

    An int or a float that is not zero, and a date or timestamp: main's
    ``f"Bearer {token}"`` sent each as ``str(value)``, and ``value or env``
    read only a zero as missing. A boolean is never one (``bool`` is an
    ``int``), nor is anything else that is not a string.

    PyYAML's safe loader resolves YAML 1.1's implicit types: ``1.5`` and
    ``.inf`` to a float (https://yaml.org/type/float.html), ``2024-01-31``
    to a ``datetime.date`` and ``2024-01-31 10:20:30`` to a
    ``datetime.datetime`` (https://yaml.org/type/timestamp.html; the
    resolvers in https://github.com/yaml/pyyaml/blob/main/lib/yaml/resolver.py).
    """
    if isinstance(value, bool) or not value:
        return None
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, datetime.date):  # a datetime is a date too
        return "date"
    return None


def _parent(block: Dict[str, Any], rel: _KeyPath) -> Optional[Dict[Any, Any]]:
    """The dict holding the leaf at ``rel`` inside ``block``, if it is still there."""
    node: Any = block
    for part in rel[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            node = None
    return node if isinstance(node, dict) and rel[-1] in node else None


def _resolve_catalog(
    config: "FluidConfig", catalog_name: str, endpoint: Optional[str]
) -> Tuple[CatalogConfig, Optional[WithheldCredentials]]:
    """One catalog block: env overrides applied, ``${VAR}`` expanded where the
    layer is trusted, and credentials a project-config endpoint did not bring
    itself withheld."""
    catalogs = config.get_section("catalogs")
    key = catalog_name
    block = catalogs.get(catalog_name)
    if block is None and catalog_name in COMMAND_CENTER_CATALOG_NAMES:
        key = COMMAND_CENTER_CANONICAL_NAME
        block = catalogs.get(key)
    catalog_config = copy.deepcopy(block or {})
    if not isinstance(catalog_config, dict):
        catalog_config = {}

    layers = _CatalogLayers(config, ("catalogs", key), {})
    layers.scalar_credentials_as_strings(catalog_name, catalog_config)

    # Every path the env overrides wrote is the environment's.
    before = dict(_leaves(catalog_config))
    # The auth map's values before an override writes over one of them.
    auth = catalog_config.get("auth")
    auth_before = dict(auth) if isinstance(auth, dict) else {}
    for path in _apply_catalog_env_overrides(catalog_name, catalog_config):
        layers.overrides[path] = _ENV_LAYER
    if endpoint:
        catalog_config["endpoint"] = endpoint
        layers.overrides[("endpoint",)] = _CLI_LAYER

    resolved = CatalogConfig(layers.expand(catalog_config))
    withheld = layers.withhold_foreign_credentials(catalog_name, resolved, before)
    if layers.endpoint_from_project(resolved):
        resolved.project_endpoint_file = config._project_file or "in the checkout"
    else:
        layers.warn_project_key_not_sent(catalog_name, resolved, auth_before)
    return resolved, withheld


class _CatalogLayers:
    """One catalog block, and the layer each of its values came from.

    ``overrides`` holds the per-call env / CLI values, keyed by their path
    inside the block; everything else is looked up in the config's sources.
    """

    def __init__(self, config: "FluidConfig", prefix: _KeyPath, overrides: Dict[_KeyPath, str]):
        self.config = config
        self.prefix = prefix
        self.overrides = overrides

    def layer(self, rel: _KeyPath) -> str:
        for end in range(len(rel), 0, -1):
            if rel[:end] in self.overrides:
                return self.overrides[rel[:end]]
        return self.config._sources.source_of(self.prefix + rel)

    def from_project(self, rel: _KeyPath) -> bool:
        return self.layer(rel) == PROJECT_LAYER

    def endpoint_from_project(self, catalog_config: Dict[str, Any]) -> bool:
        """Whether the endpoint a provider would use (``endpoint``, else ``url``)
        is the project config's."""
        key = _endpoint_key(catalog_config)
        return key is not None and self.from_project((key,))

    def expand(self, value: Any, rel: _KeyPath = ()) -> Any:
        """Expand ``${VAR}`` in every string except those from the project layer."""
        if isinstance(value, dict):
            return {k: self.expand(v, rel + (k,)) for k, v in value.items()}
        if self.from_project(rel):
            if _would_expand(value):
                self._warn_unexpanded(rel)
            return value
        if isinstance(value, list):
            return [self.expand(v, rel) for v in value]
        if isinstance(value, str):
            return os.path.expandvars(value)
        return value

    def scalar_credentials_as_strings(
        self, catalog_name: str, catalog_config: Dict[str, Any]
    ) -> None:
        """An unquoted number or date as the operator's own credential is its string form.

        YAML reads ``token: 12345`` as an int, ``token: 1.5`` as a float and
        ``token: 2024-01-31`` as a date. In a slot the backend reads its
        credential from (:func:`_credential_slots`), such a value from the
        operator's layers (:func:`_scalar_kind`) becomes its string form, as
        the f-string in ``get_auth_headers`` made it on main, and a warning
        says to quote it (YAML reads ``012345``, ``12_345`` or ``1.50`` as
        other values). A project file's number or date stays no credential
        (:func:`_blank`).
        """
        for slot in _credential_slots(catalog_name, catalog_config):
            container = _parent(catalog_config, slot)
            value = container.get(slot[-1]) if container is not None else None
            kind = _scalar_kind(value)
            if container is None or kind is None or self.layer(slot) in (PROJECT_LAYER, "default"):
                continue
            container[slot[-1]] = str(value)
            if self.prefix + slot not in self.config._credential_type_warned:
                self.config._credential_type_warned.add(self.prefix + slot)
                LOGGER.warning(
                    "%s: %s in %s is a %s, not a string; it is sent as YAML reads it. "
                    "Quote it, so that YAML keeps it exactly as written.",
                    ".".join(str(p) for p in self.prefix[1:]),
                    ".".join(slot),
                    self._where(slot),
                    kind,
                )

    def _warn_unexpanded(self, rel: _KeyPath) -> None:
        # pnpm made the same change for a repository's .npmrc
        # (GHSA-3qhv-2rgh-x77r): say so rather than fail silently.
        warned = self.config._unexpanded_warned
        if self.prefix + rel in warned:
            return
        warned.add(self.prefix + rel)
        LOGGER.warning(
            "%s: %s in the project config %s is not expanded from the environment; "
            "a config file in the checkout cannot read the runner's environment. "
            "Set the value in ~/.fluid/config.yaml or through its environment variable.",
            ".".join(str(p) for p in self.prefix[1:]),
            ".".join(str(p) for p in rel),
            self.config._project_file,
        )

    def withhold_foreign_credentials(
        self,
        catalog_name: str,
        catalog_config: Dict[str, Any],
        before: Dict[_KeyPath, Any],
    ) -> Optional[WithheldCredentials]:
        """Strip what an endpoint from the project config did not bring itself.

        The git ``safe.directory`` model: config that comes with the checkout
        cannot pair its own destination with the operator's secrets. For such
        an endpoint every value from the operator's layers (system and user
        config, the environment, the command line) is withheld, whatever its
        name and however deep in a map or a list it sits, except the settings
        on :data:`_OPERATOR_SETTINGS`. ``before`` is the block before the env
        overrides. The built-in defaults are nobody's secret and stay.

        A value the project file carries itself is kept, and wins over the
        environment's. A credential it sets to anything but a non-empty string
        (:func:`_blank`) carries nothing: it is dropped rather than kept, so it
        neither lets the environment's value through here nor leaves an empty
        slot that a consumer's own env fallback would fill.

        A block that carries a credential of its own where its backend reads
        it (:meth:`carries_own_credential`) is self-contained: a credential
        the env overrides wrote into another slot is dropped, not withheld,
        so it is neither sent nor a reason to refuse the block, and neither is
        the ``get_auth_headers`` env fallback its ``auth`` map has closed.
        So is a credential slot in the operator's config that holds nothing
        (:func:`_carries_no_operator_value`), such as the provider README's
        ``${DMM_API_KEY}`` with DMM_API_KEY unset or empty: no key of the
        operator's is in it. A value from the operator's config files that
        holds something is withheld either way, and the block refused; without
        a credential of its own, so is the environment's credential.
        """
        endpoint_key = _endpoint_key(catalog_config)
        if endpoint_key is None or not self.from_project((endpoint_key,)):
            return None

        own_credential = self.carries_own_credential(catalog_name, catalog_config, before)
        withheld: List[str] = []
        for rel, value in list(_leaves(catalog_config)):
            if rel == (endpoint_key,):
                continue
            if self.from_project(rel):
                self._drop_blank_project_credential(catalog_config, rel, value)
                continue
            if self.layer(rel) == "default" or _operator_setting(rel, value):
                continue
            own = before.get(rel)
            replaced_project_value = (
                rel in before and self.config._sources.source_of(self.prefix + rel) == PROJECT_LAYER
            )
            container = _parent(catalog_config, rel)
            if replaced_project_value and container is not None:
                if not (_names_a_credential(rel) and _blank(own)):
                    container[rel[-1]] = own
                    continue
            if (
                own_credential
                and container is not None
                and _names_a_credential(rel)
                and (
                    self.layer(rel) == _ENV_LAYER
                    or (
                        # Not inside a list: a list is withheld as a whole.
                        _whole_list(catalog_config, rel) == rel
                        and _carries_no_operator_value(value, own)
                    )
                )
            ):
                del container[rel[-1]]
                continue
            target = _whole_list(catalog_config, rel)
            container = _parent(catalog_config, target)
            if container is None:  # already withheld with the list it sits in
                continue
            name = _withheld_name(target, value if target == rel else None)
            withheld.append(f"{name} from {self._where(target)}")
            del container[target[-1]]
        auth = catalog_config.get("auth")
        if isinstance(auth, dict) or catalog_name in COMMAND_CENTER_CATALOG_NAMES:
            # Closed either way. A self-contained block has its own key where
            # its backend reads it, so the fallback it no longer has (to the
            # Command Center's FLUID_API_KEY / FLUID_BEARER_TOKEN) is no
            # reason to refuse it.
            closed = _close_auth_env_fallback(catalog_config, withheld)
            if not own_credential:
                withheld += closed

        if not withheld:
            return None
        return WithheldCredentials(
            catalog=catalog_name,
            endpoint=str(catalog_config[endpoint_key]),
            config_file=self.config._project_file or "in the checkout",
            credentials=tuple(dict.fromkeys(withheld)),
        )

    def carries_own_credential(
        self, catalog_name: str, catalog_config: Dict[str, Any], before: Dict[_KeyPath, Any]
    ) -> bool:
        """Whether the project file sets a non-empty string credential in a
        slot the block's backend reads (:func:`_credential_slots`).

        ``before`` is the block before the env overrides, so a project value
        the environment's replaced at the same path still counts.
        """
        return any(
            self.config._sources.source_of(self.prefix + slot) == PROJECT_LAYER
            and not _blank(before.get(slot))
            for slot in _credential_slots(catalog_name, catalog_config)
        )

    def _drop_blank_project_credential(
        self, catalog_config: Dict[str, Any], rel: _KeyPath, value: Any
    ) -> None:
        """Drop a credential the project file sets to anything but a non-empty string."""
        container = _parent(catalog_config, rel)
        if container is None or not (_names_a_credential(rel) and _blank(value)):
            return
        if value:
            # ``api_key: 12345`` was probably meant as a key: say why none is sent.
            self._warn_not_a_string(rel, value)
        del container[rel[-1]]

    def warn_project_key_not_sent(
        self,
        catalog_name: str,
        catalog_config: Dict[str, Any],
        auth_before: Dict[str, Any],
    ) -> None:
        """Say why a project file's non-string Command Center key is not sent to
        the operator's endpoint.

        For an endpoint the project config names, such a value is dropped with
        this warning (:meth:`withhold_foreign_credentials`). For the operator's
        endpoint it stays where it is, and ``get_auth_headers``, which takes
        only a string there, passes over it to FLUID_API_KEY /
        FLUID_BEARER_TOKEN, the operator's own key for the operator's
        endpoint. A team that committed ``token: 98765`` is told why its key
        is not the one sent. Only the slot ``get_auth_headers`` reads for the
        auth type: ``basic`` sends any value as its string form, as on main,
        and a registrar's ``pick_token`` any value but a boolean, so a number
        is passed over in neither.

        ``auth_before`` is the ``auth`` map before the env overrides.
        FLUID_API_KEY and FLUID_CATALOG_FLUID_CC_TOKEN are overrides: they are
        written into ``auth.api_key`` over the project's value instead of
        being read past it, and that value is still the one to explain.
        """
        if catalog_name not in COMMAND_CENTER_CATALOG_NAMES:
            return
        auth = catalog_config.get("auth")
        if not isinstance(auth, dict):
            return
        field_env = _AUTH_ENV_FALLBACK.get(str(auth.get("type", "api_key")))
        if field_env is None:
            return
        slot = ("auth", field_env[0])
        value = auth_before.get(field_env[0])
        from_project = self.config._sources.source_of(self.prefix + slot) == PROJECT_LAYER
        if value and not isinstance(value, str) and from_project:
            self._warn_not_a_string(slot, value)

    def _warn_not_a_string(self, rel: _KeyPath, value: Any) -> None:
        warned = self.config._credential_type_warned
        if self.prefix + rel in warned:
            return
        warned.add(self.prefix + rel)
        LOGGER.warning(
            "%s: %s in the project config %s is a %s, not a string, so it is no "
            "credential; quote it if it is one.",
            ".".join(str(p) for p in self.prefix[1:]),
            ".".join(str(p) for p in rel),
            self.config._project_file,
            type(value).__name__,
        )

    def _where(self, rel: _KeyPath) -> str:
        layer = self.layer(rel)
        return {
            _ENV_LAYER: "the environment",
            "user": "~/.fluid",
            "system": "/etc/fluid",
            _CLI_LAYER: "the command line",
            "default": "the built-in defaults",
        }.get(layer, layer)


def _credential_slots(
    catalog_name: str, catalog_config: Dict[str, Any]
) -> Tuple[Tuple[str, ...], ...]:
    """The key paths the backend for ``catalog_name`` reads its credential from.

    The Command Center reads one slot, chosen by its auth type
    (``get_auth_headers``); every other catalog is a registrar backend, which
    reads the first of ``pick_token``'s slots that is set.
    """
    if catalog_name in COMMAND_CENTER_CATALOG_NAMES:
        auth = catalog_config.get("auth")
        auth_type = auth.get("type", "api_key") if isinstance(auth, dict) else "api_key"
        return _COMMAND_CENTER_CREDENTIAL_SLOTS.get(str(auth_type), ())
    from fluid_build.build_runners.catalog_registrars._factory_helpers import TOKEN_SLOTS

    return TOKEN_SLOTS


def _endpoint_key(catalog_config: Dict[str, Any]) -> Optional[str]:
    """The key a provider reads its endpoint from: ``endpoint`` wins over ``url``
    (``BaseCatalogProvider``, ``pick_endpoint``)."""
    return next((k for k in ("endpoint", "url") if catalog_config.get(k)), None)


def _close_auth_env_fallback(catalog_config: Dict[str, Any], withheld: List[str]) -> List[str]:
    """Stop ``get_auth_headers`` filling a missing credential from the environment.

    It falls back to FLUID_API_KEY / FLUID_BEARER_TOKEN when the auth block
    names a type but carries no value for it; for an endpoint from the project
    config that is the operator's credential, so the type becomes ``none``.
    Returns the env var that would have been sent, if it is set.
    """
    auth = catalog_config.get("auth")
    if not isinstance(auth, dict):
        auth = catalog_config["auth"] = {}
    field_env = _AUTH_ENV_FALLBACK.get(str(auth.get("type", "api_key")))
    if field_env is None or not _blank(auth.get(field_env[0])):
        return []
    field, env = field_env
    auth["type"] = "none"
    if os.environ.get(env) and f"auth.{field} from the environment" not in withheld:
        return [env]
    return []


# Global configuration instance
_global_config: Optional[FluidConfig] = None


def get_config() -> FluidConfig:
    """
    Get the global configuration instance.

    Returns:
        Global FluidConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = FluidConfig()
    return _global_config


def reset_config() -> None:
    """Reset the global configuration (useful for testing)."""
    global _global_config
    _global_config = None


def create_sample_config(path: Path) -> None:
    """
    Create a sample configuration file with comments.

    Args:
        path: Path to create config file
    """
    sample_config = """# FLUID CLI Configuration File
# 
# This file configures default behavior for the FLUID CLI.
# Settings can be overridden by environment variables or command-line arguments.
#
# Configuration hierarchy (lowest to highest priority):
# 1. This file
# 2. Environment variables (FLUID_*)
# 3. Command-line arguments
#
# Kept in a project directory (fluid.config.yaml, .fluidrc.yaml, .fluid/config.yaml),
# this file is part of the checkout: its ${VAR} placeholders are not expanded, and a
# catalog endpoint it sets is only sent the credentials it carries itself. Keep
# endpoints that use your environment's credentials in ~/.fluid/config.yaml.

# Logging configuration
logging:
  level: INFO  # DEBUG, INFO, WARNING, ERROR
  format: text  # text or json
  file: null  # Path to log file (null = no file logging)
  console: true  # Log to console

# Cache configuration
cache:
  dir: ~/.fluid/cache
  schema_cache_days: 7  # How long to cache schemas
  auto_refresh: true  # Automatically refresh stale caches

# Network configuration
network:
  timeout: 30  # Request timeout in seconds
  connect_timeout: 10  # Connection timeout in seconds
  max_retries: 3  # Maximum retry attempts
  rate_limit: 50  # Max requests per minute
  verify_ssl: true  # Verify SSL certificates

# Validation configuration
validation:
  strict: false  # Treat warnings as errors
  offline: false  # Only use cached schemas
  schema_version: null  # Force specific schema version (null = auto-detect)

# Apply command configuration
apply:
  dry_run: false  # Default dry-run mode
  parallel_phases: true  # Execute phases in parallel when possible
  rollback_strategy: immediate  # immediate, phase_complete, full_rollback
  timeout_minutes: 60  # Global execution timeout

# Provider-specific configuration
providers:
  gcp:
    default_region: us-central1
    default_location: US
    # project_id: my-gcp-project  # Uncomment to set default project
  
  aws:
    default_region: us-east-1
    # account_id: YOUR_AWS_ACCOUNT_ID  # Uncomment to set default account
  
  snowflake:
    default_warehouse: COMPUTE_WH
    # account: myaccount  # Uncomment to set default account

# Catalog configuration for publishing data products
catalogs:
  fluid-command-center:
    endpoint: http://localhost:8000
    auth:
      type: api_key  # api_key, bearer, or basic
      # api_key will be read from FLUID_API_KEY env var
    # organization_id: <id>  # or FLUID_CC_ORG_ID; omitted = the only org the key belongs to
    # organization: <slug>   # the same organization by its slug, for a committed config;
    #                        # an id above wins; a slug no organization carries is an error
    enabled: true
    max_retries: 3
    timeout: 30.0
    circuit_breaker_threshold: 3
    circuit_breaker_timeout: 60

  # Example: Collibra catalog (future support)
  # collibra:
  #   endpoint: https://collibra.company.com
  #   auth:
  #     type: bearer
  #   enabled: false

# Output configuration
output:
  format: text  # text, json, yaml
  color: true  # Colorized output
  verbose: false  # Verbose output
  quiet: false  # Minimal output
"""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(sample_config)
        LOGGER.info(f"Created sample configuration at {path}")
    except Exception as e:
        raise FileSystemError(
            "Failed to create sample configuration",
            context={"path": str(path), "error": str(e)},
            suggestions=[
                "Check directory permissions",
                f"Manually create directory: mkdir -p {path.parent}",
            ],
            original_error=e,
        )
