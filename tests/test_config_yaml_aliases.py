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

"""A YAML anchor shares a value inside one file; it never joins two config paths.

``yaml.safe_load`` gives an alias (``*name``) and every key a merge key
(``<<: *name``) copies the same object as the anchor. Merged by reference, the
two paths stayed one dict, so a later layer that wrote under one name wrote
under the other too, while the trust rule, which tracks every value by its
path, still credited the second path to the layer that set it first. The
checkout's project config could so set the endpoint of an operator catalog
block through another name and get the operator's key sent there. Every layer
is now loaded as a tree in which each path holds its own copy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.config_manager import FluidConfig, UntrustedCatalogEndpoint
from fluid_build.errors import ConfigurationError

pytestmark = pytest.mark.unit

_KEY = "operator-env-key-not-a-secret"  # pragma: allowlist secret
_PROJECT_ENDPOINT = "https://project.attacker.test/p"

_ENV_VARS = (
    "FLUID_CC_ENDPOINT",
    "FLUID_CATALOG_FLUID_CC_URL",
    "FLUID_API_KEY",
    "FLUID_CATALOG_FLUID_CC_TOKEN",
    "FLUID_BEARER_TOKEN",
    "FLUID_CC_ORG_ID",
    "DMM_API_URL",
    "FLUID_CATALOG_DMM_URL",
    "DMM_API_KEY",
    "FLUID_CATALOG_DMM_TOKEN",
    "FLUID_CATALOG_DATAHUB_URL",
    "DATAHUB_GMS_URL",
    "DATAHUB_GMS_HOST",
    "DATAHUB_SERVER",
    "FLUID_CATALOG_DATAHUB_TOKEN",
    "DATAHUB_GMS_TOKEN",
    "DATAHUB_TOKEN",
)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A checkout as cwd, an empty HOME, no catalog env, and a system config
    file under ``tmp_path`` instead of ``/etc/fluid/config.yaml``."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    (home / ".fluid").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    system = tmp_path / "etc" / "config.yaml"
    system.parent.mkdir()
    monkeypatch.setattr(
        FluidConfig,
        "_load_system_config",
        lambda self: self._merge_config_file(system, "system"),
    )
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def _layer_file(checkout: Path, layer: str) -> Path:
    return {
        "system": checkout.parent / "etc" / "config.yaml",
        "user": Path.home() / ".fluid" / "config.yaml",
        "project": checkout / "fluid.config.yaml",
    }[layer]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ── the operator's own file shares a block under two names ────────────────


@pytest.mark.parametrize(
    ("shared", "other", "env"),
    [
        ("datamesh-manager", "dmm", "DMM_API_KEY"),
        ("dmm", "entropy-data", "DMM_API_KEY"),
        ("fluid_cc", "command-center", "FLUID_API_KEY"),
        ("datahub", "openmetadata", "FLUID_CATALOG_DATAHUB_TOKEN"),
    ],
)
@pytest.mark.parametrize("layer", ["system", "user"])
def test_a_project_endpoint_under_an_aliased_name_stays_under_that_name(
    checkout, monkeypatch, layer, shared, other, env
):
    _write(
        _layer_file(checkout, layer),
        f"catalogs:\n  {shared}: &shared\n    timeout: 30\n  {other}: *shared\n",
    )
    _write(
        checkout / "fluid.config.yaml",
        f"catalogs:\n  {other}:\n    endpoint: {_PROJECT_ENDPOINT}\n",
    )
    monkeypatch.setenv(env, _KEY)

    block = FluidConfig().get_catalog_config(shared)

    assert _PROJECT_ENDPOINT not in str(dict(block))
    assert block.project_endpoint_file is None
    assert block["timeout"] == 30


def test_the_name_the_project_wrote_under_is_still_refused_the_env_key(checkout, monkeypatch):
    _write(
        Path.home() / ".fluid" / "config.yaml",
        "catalogs:\n  datamesh-manager: &dmm\n    timeout: 30\n  dmm: *dmm\n",
    )
    _write(
        checkout / "fluid.config.yaml", f"catalogs:\n  dmm:\n    endpoint: {_PROJECT_ENDPOINT}\n"
    )
    monkeypatch.setenv("DMM_API_KEY", _KEY)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("dmm")

    assert _KEY not in str(raised.value)


def test_a_hidden_anchor_is_not_the_catalog_block_it_fills(checkout, monkeypatch):
    # The usual ``x-<name>: &anchor`` layout: the anchor's own key is a
    # different path from the catalog block the alias fills.
    _write(
        Path.home() / ".fluid" / "config.yaml",
        "x-dmm: &dmm\n  timeout: 30\ncatalogs:\n  datamesh-manager: *dmm\n",
    )
    _write(checkout / "fluid.config.yaml", f"x-dmm:\n  endpoint: {_PROJECT_ENDPOINT}\n")
    monkeypatch.setenv("DMM_API_KEY", _KEY)

    block = FluidConfig().get_catalog_config("datamesh-manager")

    assert "endpoint" not in block
    assert block.project_endpoint_file is None


# ── anchors, aliases and merge keys in every layer ────────────────────────

_ANCHORED = """\
x-common: &common
  auth:
    type: api_key
  timeout: 30
catalogs:
  datamesh-manager: &dmm
    timeout: 30
  dmm: *dmm
  fluid_cc:
    <<: *common
    endpoint: https://cc.operator.test
  command-center:
    <<: *common
x-limits: &limits
  rate: 5
limits:
  read: *limits
  write: *limits
"""

_LATER_WRITES = {
    # path a later layer writes -> the path that shared its object in the file
    "catalogs.dmm.endpoint": "catalogs.datamesh-manager",
    "catalogs.command-center.auth.type": "catalogs.fluid_cc.auth",
    "limits.write.rate": "limits.read",
}


def _write_later(checkout: Path, layer: str, cfg_factory):
    """The anchored file in ``layer``; each of :data:`_LATER_WRITES` written
    by a layer above it (the project file, or the command line)."""
    _write(_layer_file(checkout, layer), _ANCHORED)
    if layer == "project":
        cfg = cfg_factory()
        for path in _LATER_WRITES:
            cfg.set(path, "WRITTEN-LATER")
        return cfg
    _write(
        checkout / "fluid.config.yaml",
        "catalogs:\n"
        "  dmm:\n    endpoint: WRITTEN-LATER\n"
        "  command-center:\n    auth:\n      type: WRITTEN-LATER\n"
        "limits:\n  write:\n    rate: WRITTEN-LATER\n",
    )
    return cfg_factory()


@pytest.mark.parametrize("layer", ["system", "user", "project"])
def test_a_later_write_reaches_only_its_own_path(checkout, layer):
    cfg = _write_later(checkout, layer, FluidConfig)

    for written, sharer in _LATER_WRITES.items():
        assert cfg.get(written) == "WRITTEN-LATER", written
        assert "WRITTEN-LATER" not in str(cfg.get(sharer)), (written, sharer, cfg.get(sharer))
    assert cfg.get("catalogs.fluid_cc.auth") == {"type": "api_key"}
    assert cfg.get("limits.read.rate") == 5


@pytest.mark.parametrize("layer", ["system", "user", "project"])
def test_every_path_is_credited_to_the_layer_that_wrote_it(checkout, layer):
    cfg = _write_later(checkout, layer, FluidConfig)
    later = "project" if layer != "project" else "cli"

    sources = cfg._sources
    assert sources.source_of(("catalogs", "dmm", "endpoint")) == later
    assert sources.source_of(("catalogs", "datamesh-manager", "timeout")) == layer
    assert sources.source_of(("catalogs", "fluid_cc", "auth", "type")) == layer
    assert sources.source_of(("limits", "read", "rate")) == layer


def test_a_merge_key_copies_nested_maps_too(checkout, monkeypatch):
    # The project sets a key under one merge-key user; the other user of the
    # same anchor, the operator's own endpoint, does not get it.
    _write(Path.home() / ".fluid" / "config.yaml", _ANCHORED)
    _write(
        checkout / "fluid.config.yaml",
        "catalogs:\n  command-center:\n    auth:\n      type: bearer\n",
    )
    monkeypatch.setenv("FLUID_API_KEY", _KEY)

    block = FluidConfig().get_catalog_config("fluid_cc")

    assert block["auth"] == {"type": "api_key", "api_key": _KEY}
    assert block.project_endpoint_file is None


def test_a_recursive_alias_is_a_config_error_not_a_hang(checkout):
    _write(Path.home() / ".fluid" / "config.yaml", "catalogs:\n  dmm: &loop\n    again: *loop\n")

    with pytest.raises(ConfigurationError, match="Failed to parse user configuration"):
        FluidConfig()


# ── the env layer is credited by the paths it writes ───────────────────────


@pytest.mark.parametrize("value", ["k", "a-longer-key"], ids=["one-character", "longer"])
def test_an_env_value_equal_to_the_users_is_credited_to_the_env(checkout, monkeypatch, value):
    # The env override replaced the user's value at that path: what the block
    # holds there is the environment's, however Python stores the string.
    _write(
        Path.home() / ".fluid" / "config.yaml", f"catalogs:\n  datahub:\n    api_token: {value}\n"
    )
    _write(
        checkout / "fluid.config.yaml",
        f"catalogs:\n  datahub:\n    endpoint: {_PROJECT_ENDPOINT}\n",
    )
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", value)

    with pytest.raises(UntrustedCatalogEndpoint) as raised:
        FluidConfig().get_catalog_config("datahub")

    assert raised.value.withheld.credentials == ("api_token from the environment",)


def test_the_backend_env_overrides_say_which_keys_they_wrote(monkeypatch):
    import fluid_build.build_runners.catalog_registrars  # noqa: F401 - registers the backends
    from fluid_build.api.catalog_backend import apply_env_overrides

    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", _KEY)
    block = {"api_token": _KEY, "timeout": 5}

    assert apply_env_overrides("datahub", block) == ["api_token"]
    assert apply_env_overrides("no-such-backend", block) == []


# ── market.yaml: the same rule for the market config ──────────────────────


def test_a_checkout_market_file_alias_does_not_carry_the_env_token_to_another_block(
    checkout, monkeypatch
):
    # The env fills the ``datahub`` block (its endpoint and its token together);
    # an alias in the checkout's market.yaml must not hand that token to the
    # ``mcp`` block, whose endpoint the checkout chose.
    from fluid_build.cli.market import CheckoutMarketBlock, load_market_config

    monkeypatch.setenv("DATAHUB_SERVER_URL", "https://datahub.operator.test")
    monkeypatch.setenv("DATAHUB_TOKEN", _KEY)
    _write(
        checkout / "market.yaml",
        "catalogs: [datahub, mcp]\n"
        "datahub: &shared\n"
        f"  url: {_PROJECT_ENDPOINT}\n"
        "mcp: *shared\n",
    )

    config = load_market_config(SimpleNamespace(catalogs=None), logging.getLogger("t"))

    assert isinstance(config["mcp"], CheckoutMarketBlock)
    assert _KEY not in str(dict(config["mcp"]))
    assert config["datahub"]["token"] == _KEY


# ── aliases that would expand without bound are refused ────────────────────
#
# Every path holds its own copy, so each alias is copied out in full. Nested
# aliases (``l2: [*l1, *l1, ...]``, ``l3: [*l2, ...]``) grow ninefold a level:
# a few hundred bytes would expand to hundreds of millions of values, and hang
# or exhaust the memory of any ``fluid`` command run in the checkout. The copy
# is refused past go-yaml v3's alias budget instead, as a YAML error that names
# the file.
#
# Nested repeated merge keys (``m2: {<<: [*m1, *m1, ...]}``, ``m3: {<<: [*m2,
# ...]}``) grow the same way, but inside the YAML loader: SafeLoader copies the
# merged key/value pairs into every mapping that merges them before anything is
# constructed, so the copy above only ever sees the few keys left once the
# duplicates collapse. The loader spends the same budget on every pair a merge
# key copies.

_LAYERS = ("system", "user", "project")


def _laughs(depth: int, width: int = 9) -> str:
    """``depth`` levels of ``width`` aliases each: ``width ** depth`` values copied out."""
    lines = ["x-laughs:", "  l1: &l1 [" + ", ".join(["lol"] * width) + "]"]
    for level in range(2, depth + 1):
        lines.append(f"  l{level}: &l{level} [" + ", ".join([f"*l{level - 1}"] * width) + "]")
    return "\n".join(lines) + "\n"


def _merge_laughs(depth: int, width: int = 9) -> str:
    """``depth`` levels of ``width`` merged aliases each: ``width ** depth`` pairs merged."""
    lines = ["x-laughs:", "  m1: &m1 {" + ", ".join(f"k{i}: v" for i in range(width)) + "}"]
    for level in range(2, depth + 1):
        lines.append(f"  m{level}: &m{level}")
        lines.append("    <<: [" + ", ".join([f"*m{level - 1}"] * width) + "]")
    return "\n".join(lines) + "\n"


# Each of these completes in well under a second on a loader without a budget,
# so a refusal is what tells them apart.
_BOMBS = {
    "aliases": _laughs(5),
    "merge-keys": _merge_laughs(6),
    "narrow-merge-keys": _merge_laughs(16, width=2),
}


def _market_file(checkout: Path, which: str) -> Path:
    return {
        "operator": Path.home() / ".fluid" / "market.yaml",
        "checkout": checkout / "market.yaml",
    }[which]


@pytest.mark.parametrize("bomb", sorted(_BOMBS))
@pytest.mark.parametrize("layer", _LAYERS)
def test_nested_aliases_past_the_budget_are_refused_naming_the_file(checkout, layer, bomb):
    path = _write(_layer_file(checkout, layer), _BOMBS[bomb])

    with pytest.raises(ConfigurationError, match=f"Failed to parse {layer} configuration") as e:
        FluidConfig()

    assert "excessive aliasing" in str(e.value)
    assert str(path) in str(e.value)


@pytest.mark.parametrize("bomb", sorted(_BOMBS))
@pytest.mark.parametrize("which", ["operator", "checkout"])
def test_a_market_file_with_nested_aliases_is_refused_naming_the_file(
    checkout, which, bomb, caplog
):
    from fluid_build.cli.market import load_market_config

    path = _write(_market_file(checkout, which), "catalogs: [datahub]\n" + _BOMBS[bomb])

    with caplog.at_level(logging.WARNING):
        config = load_market_config(SimpleNamespace(catalogs=None), logging.getLogger("t"))

    assert "x-laughs" not in config
    assert config["catalogs"] != ["datahub"]  # nothing of the file was merged
    assert "excessive aliasing" in caplog.text
    # The checkout's file is named as the loader opens it, relative to the checkout.
    assert f"{path if which == 'operator' else 'market.yaml'}: document" in caplog.text


_CHILD = """
import logging, sys, time
from types import SimpleNamespace
try:  # a regression must fail the test, not exhaust the runner's memory
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
except (ImportError, ValueError, OSError):
    pass
from fluid_build.config_manager import FluidConfig
from fluid_build.cli.market import load_market_config
from fluid_build.errors import ConfigurationError
logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="%(message)s")
start = time.monotonic()
if sys.argv[1] == "config":
    try:
        FluidConfig()
    except ConfigurationError as e:
        print(e)
else:
    load_market_config(SimpleNamespace(catalogs=None), logging.getLogger("t"))
print(f"returned after {time.monotonic() - start:.3f}s")
"""


@pytest.mark.parametrize("bomb", [_laughs, _merge_laughs], ids=["aliases", "merge-keys"])
@pytest.mark.parametrize(
    ("loader", "name"),
    [("config", "fluid.config.yaml"), ("market", "market.yaml")],
    ids=["project-config", "checkout-market-file"],
)
def test_a_depth_nine_alias_bomb_is_refused_within_seconds(checkout, loader, name, bomb):
    # About 9**9 = 387 million values (or merged pairs) once expanded, from a
    # few hundred bytes. Run in a child process under a timeout (and, where the
    # platform allows, a memory cap), so that a regression fails this test
    # instead of hanging the run.
    import os
    import re
    import subprocess
    import sys

    path = _write(checkout / name, bomb(9))
    assert len(path.read_bytes()) < 1024

    child = subprocess.run(
        [sys.executable, "-c", _CHILD, loader],
        cwd=checkout,
        env={**os.environ, "HOME": str(Path.home())},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert child.returncode == 0, child.stderr[-2000:]
    named = str(path) if loader == "config" else name  # market.yaml is opened relative to cwd
    assert f"{named}: document contains excessive aliasing" in child.stdout, child.stdout
    seconds = float(re.search(r"returned after ([0-9.]+)s", child.stdout).group(1))
    assert seconds < 5, child.stdout


_LEGIT = (
    """\
x-base: &base
  timeout: 30
  retries: 3
  headers: {Accept: application/json, X-Team: data}
x-wide: &wide
"""
    + "".join(f"  k{i}: v{i}\n" for i in range(30))
    + """\
catalogs:
  datamesh-manager: &dmm
    <<: *base
    endpoint: https://dmm.operator.test
  dmm: *dmm
  datahub:
    <<: *base
    timeout: 60
limits:
"""
    + "".join(f"  item{i}: *wide\n" for i in range(300))
)


@pytest.mark.parametrize("layer", _LAYERS)
def test_anchors_aliases_and_merge_keys_within_the_budget_load_unchanged(checkout, layer):
    import yaml

    _write(_layer_file(checkout, layer), _LEGIT)
    expected = yaml.safe_load(_LEGIT)

    cfg = FluidConfig()

    for key in ("x-base", "x-wide", "limits"):
        assert cfg.get(key) == expected[key], key
    for name in ("datamesh-manager", "dmm", "datahub"):
        assert cfg.get(f"catalogs.{name}") == expected["catalogs"][name], name
    assert cfg.get("catalogs.datahub.timeout") == 60


# Merge keys in the shapes a config uses them: one anchor, a list of them (the
# first mapping listed wins, the mapping's own keys over all), an inline
# mapping, a list behind an alias, a merge of a mapping that merges, nested
# maps, many blocks merging the same anchors, and three levels of nine.
_MERGES = (
    """\
x-a: &a {timeout: 30, retries: 3, auth: {type: api_key}, tags: [a, b]}
x-b: &b {timeout: 60, verify: true}
x-both: &both [*a, *b]
inline:
  <<: {timeout: 5, inline: yes}
  own: 1
single:
  <<: *a
  retries: 9
precedence:
  <<: [*b, *a]
  own: 2
list-alias:
  <<: *both
chained: &chained
  <<: *a
  extra: e
merge-of-merge:
  <<: [*chained, *b]
  auth: {type: none}
blocks:
"""
    + "".join(f"  b{i}:\n    <<: [*a, *b]\n    own: {i}\n" for i in range(200))
    + _merge_laughs(3)
)


@pytest.mark.parametrize("layer", _LAYERS)
def test_merge_keys_within_the_budget_load_as_yaml_safe_load_reads_them(checkout, layer):
    import yaml

    _write(_layer_file(checkout, layer), _MERGES)
    expected = yaml.safe_load(_MERGES)

    cfg = FluidConfig()

    for key, value in expected.items():
        assert cfg.get(key) == value, key
    assert cfg.get("precedence.timeout") == 60 and cfg.get("blocks.b7.own") == 7


def test_the_config_loader_reads_every_shape_within_the_budget_as_yaml_safe_load():
    import yaml

    from fluid_build.config_manager import load_config_file

    for text in (_MERGES, _LEGIT, _ANCHORED, _laughs(3), "a: &a !!set {x, y}\nb: *a\n", ""):
        assert load_config_file(text, "f.yaml") == (yaml.safe_load(text) or {}), text[:60]
    for bad in ("a:\n  <<: 1\n", "a:\n  <<: [1]\n", "a: *undefined\n"):
        with pytest.raises(yaml.YAMLError) as ours:
            load_config_file(bad, "f.yaml")
        with pytest.raises(yaml.YAMLError) as theirs:
            yaml.safe_load(bad)
        assert str(ours.value) == str(theirs.value), bad


def test_the_copy_equals_what_yaml_loaded_for_every_shape_within_the_budget():
    import yaml

    from fluid_build.config_manager import without_aliases

    for text in (_ANCHORED, _LEGIT, _laughs(3), "a: &a !!set {x, y}\nb: *a\n", "[]", "{}"):
        loaded = yaml.safe_load(text)
        assert without_aliases(loaded) == loaded, text[:60]


@pytest.mark.parametrize("which", ["operator", "checkout"])
def test_a_market_file_with_aliases_within_the_budget_still_loads(checkout, which):
    from fluid_build.cli.market import load_market_config

    _write(
        _market_file(checkout, which),
        "catalogs: [datahub]\nx-d: &d\n  timeout: 9\ndatahub:\n  <<: *d\n  max_results: 5\n",
    )

    config = load_market_config(SimpleNamespace(catalogs=None), logging.getLogger("t"))

    assert config["catalogs"] == ["datahub"]
    assert config["datahub"]["timeout"] == 9 and config["datahub"]["max_results"] == 5
