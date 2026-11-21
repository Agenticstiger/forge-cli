# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Static gate: no dataclass in ``fluid_build/`` prints a secret in its repr.

A dataclass's generated ``__repr__`` prints every field. A debug line, an
f-string with ``!r`` or a traceback that shows locals would then put a
password, a token or a whole subprocess environment in a log. This test keeps
such fields out of the repr. Two classes showed the way before it existed:
``secrets/_vault.py::_Request.token`` uses ``field(repr=False)``, and
``observability/config.py::CommandCenterConfig`` writes its own ``__repr__``.

**The scan** walks the AST of every module under ``fluid_build/`` and imports
nothing from it. It reads every class decorated with ``dataclass`` or
``dataclasses.dataclass`` (nested and function-local ones too) and every
annotated field in its body (``ClassVar`` excluded). A field is hidden when it
is ``field(repr=False)``, when the decorator says ``repr=False``, or when the
class defines ``__repr__``. Only a literal ``False`` counts.

A field is a suspect when a word of its name (split on ``_``) names a secret
(:data:`SECRET_WORDS`, or a pair in :data:`SECRET_PAIRS` such as ``api_key``),
or when it is an environment (:data:`ENV_WORDS`) annotated as a mapping, which
holds values. A suspect is cleared when its annotation is a number or a flag
(``max_tokens: int``), or when its last word names where a secret is, not the
secret (:data:`PLACE_WORDS`: ``token_env``, ``password_ref``, ``secret_name``)
and its annotation is not a mapping.

**Fix a report** with ``field(repr=False)``, or with ``SecretStr`` where the
value flows to one call only. A field whose name reads as a secret but holds
none goes in :data:`ACCEPTED` with the reason. An entry whose field no longer
exists fails, so the list can only shrink; one that is hidden by now passes.

**Prior art.** No linter flags this: ruff's S105-S107 find hardcoded password
literals, and CodeQL's ``py/clear-text-logging-sensitive-data`` follows taint
into a log call, not a repr. The shape is this repo's other AST gates
(``test_no_dollar_anchored_validators.py``, ``fragments/test_no_raw_readers.py``).

**Not covered.** ``repr=False`` hides a field from ``repr()`` only:
``dataclasses.asdict()``, ``vars()`` and ``__dict__`` still hold it. A name the
word lists miss (``key``, ``auth``, ``dsn``), a URL that carries userinfo, a
``NamedTuple``, an ``attrs`` class and a pydantic model are out of scope.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "fluid_build"

#: A word of a field name that names a secret.
SECRET_WORDS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passphrases",
        "secret",
        "secrets",
        "token",
        "tokens",
        "credential",
        "credentials",
        "bearer",
        "apikey",
        "webhook",
    }
)
#: Two words that together name a secret (``api_key``, ``hmac_key``).
SECRET_PAIRS = frozenset(
    {
        ("api", "key"),
        ("api", "keys"),
        ("private", "key"),
        ("hmac", "key"),
        ("signing", "key"),
        ("integration", "key"),
    }
)
#: An environment: a secret holder only as a mapping of names to values.
ENV_WORDS = frozenset({"env", "envs", "environ", "environment", "environments"})
#: A last word that names where a secret is, not the secret.
PLACE_WORDS = frozenset(
    {
        "ref",
        "refs",
        "reference",
        "references",
        "env",
        "var",
        "vars",
        "variable",
        "variables",
        "path",
        "paths",
        "file",
        "files",
        "arn",
        "arns",
        "name",
        "names",
        "id",
        "ids",
        "policy",
        "source",
        "sources",
        "mount",
        "scheme",
        "schemes",
        "keys",
    }
)
_SCALARS = frozenset({"bool", "int", "float"})
_MAPPING = re.compile(r"\b(Dict|dict|Mapping|MutableMapping|OrderedDict|_Environ)\b")

#: ``module:Class.field`` -> why the field holds no secret.
ACCEPTED: Dict[str, str] = {
    "fluid_build.api.catalog_backend:CatalogBackendSpec.env_vars": (
        "each catalog setting's variable names, never their values"
    ),
    "fluid_build.cli._welcome_scan:WelcomeFindings.cloud_credentials": (
        "the names of the clouds whose credentials the welcome scan found"
    ),
    "fluid_build.config_manager:WithheldCredentials.credentials": (
        "the names of the credentials withheld from an endpoint"
    ),
    "fluid_build.credentials.protected_env:EndpointGroup.credentials": "variable names",
    "fluid_build.credentials.protected_env:EndpointGroup.connection_string_passwords": (
        "variable names"
    ),
    "fluid_build.credentials.protected_env:EndpointRefusal.operator_credentials": (
        "variable names"
    ),
    "fluid_build.forge.core.pipeline_systems._engine_specs:EngineRuntime.env_vars": (
        "fixed paths a generated pipeline sets for an engine, written in the source"
    ),
    "fluid_build.policy.lakekeeper_apply:Settings.token_uri": (
        "the URL of the token endpoint, not a token"
    ),
}


@dataclasses.dataclass(frozen=True)
class Field:
    module: str
    owner: str
    name: str
    annotation: str
    hidden: bool
    line: int

    @property
    def key(self) -> str:
        return f"{self.module}:{self.owner}.{self.name}"


def _decorator_name(node: ast.expr) -> Optional[str]:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _repr_false(call: ast.Call) -> bool:
    return any(
        kw.arg == "repr" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in call.keywords
    )


def _is_field_call(value: Optional[ast.expr]) -> bool:
    return isinstance(value, ast.Call) and _decorator_name(value) == "field"


def _dataclass_decorator(cls: ast.ClassDef) -> Optional[ast.expr]:
    for dec in cls.decorator_list:
        if _decorator_name(dec) == "dataclass":
            return dec
    return None


def _fields(module: str, tree: ast.Module) -> Iterator[Field]:
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        dec = _dataclass_decorator(cls)
        if dec is None:
            continue
        whole = (isinstance(dec, ast.Call) and _repr_false(dec)) or any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__repr__"
            for item in cls.body
        )
        for item in cls.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            annotation = ast.unparse(item.annotation)
            if "ClassVar" in annotation:
                continue
            value = item.value
            hidden = whole or (
                _is_field_call(value) and isinstance(value, ast.Call) and _repr_false(value)
            )
            yield Field(module, cls.name, item.target.id, annotation, hidden, item.lineno)


def _words(name: str) -> List[str]:
    return [w for w in name.lower().split("_") if w]


def suspect(name: str, annotation: str) -> bool:
    """True when a field of this name and annotation may hold a secret value."""
    words = _words(name)
    if not words:
        return False
    mapping = bool(_MAPPING.search(annotation))
    bare = annotation.replace("Optional[", "").rstrip("]").strip()
    if bare in _SCALARS:
        return False
    named = any(w in SECRET_WORDS for w in words) or any(
        pair in SECRET_PAIRS for pair in zip(words, words[1:], strict=False)
    )
    environment = mapping and any(w in ENV_WORDS for w in words)
    if not (named or environment):
        return False
    if environment:
        return True
    return mapping or words[-1] not in PLACE_WORDS


def _scan() -> List[Field]:
    found: List[Field] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        module = ".".join(rel.parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(_fields(module, tree))
    return found


@pytest.fixture(scope="module")
def fields() -> List[Field]:
    return _scan()


def test_no_dataclass_field_that_holds_a_secret_is_in_its_repr(fields: List[Field]) -> None:
    shown = [
        f
        for f in fields
        if suspect(f.name, f.annotation) and not f.hidden and f.key not in ACCEPTED
    ]
    assert not shown, (
        "These dataclass fields may hold a secret and stay in the generated repr. "
        "Give each field(repr=False), or add it to ACCEPTED with the reason it holds none:\n"
        + "\n".join(f"  {f.key} ({f.annotation}) at line {f.line}" for f in shown)
    )


def test_every_accepted_entry_still_names_a_field(fields: List[Field]) -> None:
    keys = {f.key for f in fields}
    stale = sorted(set(ACCEPTED) - keys)
    assert not stale, f"ACCEPTED names fields that no longer exist: {stale}"


def test_the_scan_sees_the_known_shapes(fields: List[Field]) -> None:
    by_key = {f.key: f for f in fields}
    hidden = by_key["fluid_build.secrets._vault:_Request.token"]
    assert hidden.hidden and suspect(hidden.name, hidden.annotation)
    # A whole class hidden by its own __repr__.
    config = [f for f in fields if f.module == "fluid_build.observability.config"]
    assert any(f.owner == "CommandCenterConfig" and f.hidden for f in config)


@pytest.mark.parametrize(
    "name, annotation, expected",
    [
        ("password", "Optional[str]", True),
        ("api_key", "str", True),
        ("oauth_token", "Optional[str]", True),
        ("private_key_passphrase", "Optional[str]", True),
        ("env", "Dict[str, str]", True),
        ("child_env", "Dict[str, str]", True),
        ("env", "str", False),  # an environment's name
        ("max_tokens", "int", False),
        ("token_env", "str", False),  # the variable a token is read from
        ("password_ref", "Optional[str]", False),
        ("secret_name", "str", False),
        ("secrets", "Dict[str, str]", True),
        ("key", "str", False),
    ],
)
def test_suspect_names(name: str, annotation: str, expected: bool) -> None:
    assert suspect(name, annotation) is expected


#: The fields this gate was written for: each is hidden, checked on the class itself.
_HIDDEN_AT_RUNTIME: Tuple[Tuple[str, str, str], ...] = (
    ("fluid_build.cli._apply_opentofu_engine", "_Engine", "env"),
    ("fluid_build.api.runner", "RunContext", "env"),
    ("fluid_build.build_runners._lineage", "HttpLineageEmitter", "api_key"),
    ("fluid_build.iac.providers.databricks_genie", "Space", "token"),
    ("fluid_build.llm.providers", "LlmConfig", "api_key"),
    ("fluid_build.providers.snowflake.types", "ProviderOptions", "password"),
    ("fluid_build.cli.ship", "_Context", "child_env"),
    # Its endpoint may carry a token in the query (?token=...).
    ("fluid_build.build_runners._lineage", "HttpLineageEmitter", "endpoint"),
)


@pytest.mark.parametrize("module, owner, name", _HIDDEN_AT_RUNTIME)
def test_the_named_fields_are_hidden_at_runtime(module: str, owner: str, name: str) -> None:
    cls = getattr(importlib.import_module(module), owner)
    (found,) = [f for f in dataclasses.fields(cls) if f.name == name]
    assert found.repr is False, f"{owner}.{name} is in the repr"


def test_a_repr_never_shows_the_values() -> None:
    from fluid_build.build_runners._lineage import HttpLineageEmitter
    from fluid_build.llm.providers import LlmConfig
    from fluid_build.providers.snowflake.types import ProviderOptions

    sentinel = "sentinel-secret-value-0f1e"
    shown = [
        repr(
            HttpLineageEmitter(
                endpoint=f"https://lineage.example/?token={sentinel}", api_key=sentinel
            )
        ),
        repr(LlmConfig(provider="p", model="m", endpoint="https://llm.example", api_key=sentinel)),
        repr(
            ProviderOptions(
                account="acct",
                user="svc",
                password=sentinel,
                private_key_passphrase=sentinel,
                oauth_token=sentinel,
            )
        ),
    ]
    for text in shown:
        assert sentinel not in text, text
