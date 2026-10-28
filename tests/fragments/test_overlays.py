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

"""Overlays compose from fragments before they merge, and are never dropped (scenarios F-01..F-35).

The spec this file holds the code to (section 1.5 of the scenario catalog):

* an overlay's refs resolve relative to the overlay file, confined to the same
  ref root, **before** the merge, so the keyed merge (``builds[].id``,
  ``exposes[].exposeId``, ``consumes[]`` by productId + exposeId) sees the ids
  the fragments name; same-document pointers resolve **after** the merge;
* an overlay is never silently dropped: one that cannot be applied is an
  error, and ONE loader serves every command and the library.

Fixtures are tiny hand-built trees under ``tmp_path``. The CLI runs in
process (``fluid_build.cli.main``); nothing touches the network, Docker or a
cloud. Sentinel values are chosen so a dropped overlay shows up as the base
value: ``orders.dev.events`` against ``orders.prod.events``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import loader
from fluid_build.loader import RefConfinementError, RefResolutionError

_CHECKOUT = Path(__file__).resolve().parents[2]

DEV = "orders.dev.events"
PROD = "orders.prod.events"


# ── isolation ───────────────────────────────────────────────────────────


def _clear_loader_registries() -> None:
    """Empty every process-wide warn-once set of the loader (``_NOTED_*``)."""
    for name, value in vars(loader).items():
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_imported() -> None:
    imported = Path(fluid_build.__file__).resolve()
    assert imported.is_relative_to(_CHECKOUT), f"{imported} is not inside {_CHECKOUT}"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    for name in ("FLUID_REF_ROOT", "FLUID_ENV", "FLUID_PROVIDER", "FLUID_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    _clear_loader_registries()
    yield
    _clear_loader_registries()


# ── helpers ─────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def _put(root: Path, files: Dict[str, str]) -> Path:
    """Write ``files`` (relative path -> text) under ``root``; the contract path."""
    for rel, text in files.items():
        _write(root / rel, text)
    return root / "contract.fluid.yaml"


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _has_ref_key(node: Any) -> bool:
    """True when some mapping anywhere in ``node`` has a ``$ref`` KEY (substrings are text)."""
    stack = [node]
    seen = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            if "$ref" in current:
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _tree_text(directory: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(directory.rglob("*")) if p.is_file()
    )


def _tree_documents(directory: Path) -> List[Tuple[str, Any]]:
    """Every YAML/JSON file under ``directory`` parsed, with its relative name."""
    out = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix in (".yaml", ".yml", ".json"):
            out.append((p.relative_to(directory).as_posix(), _read(p)))
    return out


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - no symlinks on this platform
        pytest.skip("this platform cannot create a symlink")


def cli(argv: Sequence[Any], capsys: pytest.CaptureFixture) -> Tuple[int, str]:
    """``fluid <argv>`` in process: ``(exit code, stdout + stderr)``."""
    from fluid_build.cli import main

    rc = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _topics(document: Dict[str, Any]) -> Dict[str, str]:
    return {e["exposeId"]: e["binding"]["location"]["topic"] for e in document["exposes"]}


def _base_doc(**over: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "commerce.orders_events",
        "name": "Orders Events",
        "description": "Order events published to Kafka.",
        "domain": "commerce",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "commerce-data", "email": "commerce@example.com"},
        },
    }
    doc.update(over)
    return doc


def _topic_expose(expose_id: str, topic: str, **extra: Any) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": "topic",
        "version": "1.0.0",
        "binding": {
            "platform": "kafka",
            "format": "kafka_topic",
            "location": {"topic": topic},
        },
        "contract": {"schema": [{"name": "order_id", "type": "string", "required": True}]},
    }
    expose.update(extra)
    return expose


_PROD_BINDING = {
    "platform": "kafka",
    "format": "kafka_topic",
    "location": {"topic": PROD},
}


# Draft 22 case A: the overlay takes ONE field of its expose from a fragment.
def _case_a(root: Path, *, split_base: bool = True) -> Path:
    expose = _topic_expose("orders_events", DEV)
    files = {
        "fragments/prod-binding.yaml": _dump(_PROD_BINDING),
        "overlays/prod.yaml": (
            "exposes:\n  - exposeId: orders_events\n"
            "    binding: {$ref: ../fragments/prod-binding.yaml}\n"
        ),
    }
    if split_base:
        files["fragments/exposes/orders_events.yaml"] = _dump(expose)
        exposes: Any = [{"$ref": "./fragments/exposes/orders_events.yaml"}]
    else:
        exposes = [expose]
    files["contract.fluid.yaml"] = _dump(_base_doc(exposes=exposes))
    return _put(root, files)


# Draft 22 case B: the overlay takes a WHOLE entry from a fragment.
def _case_b(root: Path) -> Path:
    expose = _topic_expose("orders_events", DEV)
    prod_expose = {"exposeId": "orders_events", "binding": {"location": {"topic": PROD}}}
    return _put(
        root,
        {
            "fragments/exposes/orders_events.yaml": _dump(expose),
            "fragments/prod-expose.yaml": _dump(prod_expose),
            "overlays/prod.yaml": "exposes:\n  - {$ref: ../fragments/prod-expose.yaml}\n",
            "contract.fluid.yaml": _dump(
                _base_doc(exposes=[{"$ref": "./fragments/exposes/orders_events.yaml"}])
            ),
        },
    )


# Draft 22 case B2: the same, with two exposes in the base.
def _case_b2(root: Path) -> Path:
    prod_expose = {"exposeId": "audit_events", "binding": {"location": {"topic": "audit.prod"}}}
    return _put(
        root,
        {
            "fragments/prod-audit.yaml": _dump(prod_expose),
            "overlays/prod.yaml": "exposes:\n  - {$ref: ../fragments/prod-audit.yaml}\n",
            "contract.fluid.yaml": _dump(
                _base_doc(
                    exposes=[
                        _topic_expose("orders_events", DEV),
                        _topic_expose("audit_events", "audit.dev"),
                    ]
                )
            ),
        },
    )


def _assert_no_overlay_drop_notice(text: str) -> None:
    assert "overlay_not_applied" not in text
    assert "not applied" not in text.lower()


# ── F-01 / F-02 · the overlay is applied by every command ──────────────────

#: Each way an overlay can compose from a fragment, and the contract it patches.
_SHAPES = {"binding": _case_a, "entry": _case_b}


def _artifact_files(out: Path) -> str:
    return _tree_text(out)


def _prod_everywhere(
    path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    command: str,
) -> None:
    """Run one command for ``--env prod`` and check its result holds the prod topic."""
    if command == "validate":
        rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)
        assert rc == 0, text
        _assert_no_overlay_drop_notice(text)
    elif command == "bundle":
        out = tmp_path / "bundle.yaml"
        rc, text = cli(["bundle", path, "--env", "prod", "--out", out], capsys)
        assert rc == 0, text
        document = _read(out)
        assert _topics(document)["orders_events"] == PROD
        assert not _has_ref_key(document)
        _assert_no_overlay_drop_notice(text)
    elif command == "artifacts":
        out = tmp_path / "artifacts"
        rc, text = cli(["generate", "artifacts", path, "--env", "prod", "--out", out], capsys)
        assert rc == 0, text
        written = _artifact_files(out)
        assert PROD in written and DEV not in written
        assert not any(_has_ref_key(doc) for _, doc in _tree_documents(out))
        _assert_no_overlay_drop_notice(text)
    elif command == "odcs":
        out = tmp_path / "std.odcs.yaml"
        argv = ["generate", "standard", path, "--format", "odcs", "--env", "prod", "--out", out]
        rc, text = cli(argv, capsys)
        assert rc == 0, text
        assert PROD in out.read_text(encoding="utf-8")
        assert DEV not in out.read_text(encoding="utf-8")
        assert not _has_ref_key(_read(out))
        _assert_no_overlay_drop_notice(text)
    elif command == "plan":
        out = tmp_path / "plan.json"
        rc, text = cli(["plan", path, "--env", "prod", "--out", out], capsys)
        assert rc == 0, text
        plan = json.loads(out.read_text(encoding="utf-8"))
        assert _topics(plan["contract"])["orders_events"] == PROD
        assert not _has_ref_key(plan)
        assert DEV not in json.dumps(plan)
        _assert_no_overlay_drop_notice(text)
    elif command == "api":
        from fluid_build.api.contract import load_contract

        with caplog.at_level(logging.DEBUG):
            loaded = load_contract(path, env="prod")
        assert _topics(loaded.contract)["orders_events"] == PROD
        assert loaded.overlay == (path.parent / "overlays" / "prod.yaml").resolve()
        assert loaded.unresolved_refs == ()
        _assert_no_overlay_drop_notice(caplog.text)
    else:  # pragma: no cover
        raise AssertionError(command)


_COMMANDS = ["validate", "bundle", "artifacts", "odcs", "plan", "api"]


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_f01_f02_every_command_applies_an_overlay_composed_from_a_fragment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    shape: str,
    command: str,
) -> None:
    """F-01 (a field of an entry from a fragment) and F-02 (a whole entry from one)."""
    path = _SHAPES[shape](tmp_path / "p")

    _prod_everywhere(path, tmp_path, capsys, caplog, command)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_f01_f02_the_base_still_answers_without_an_env(
    tmp_path: Path, capsys: pytest.CaptureFixture, shape: str
) -> None:
    """The control: with no env the same contract holds the dev topic, so the
    prod topic above can only have come from the overlay."""
    path = _SHAPES[shape](tmp_path / "p")
    out = tmp_path / "bundle.yaml"

    rc, text = cli(["bundle", path, "--out", out], capsys)

    assert rc == 0, text
    assert _topics(_read(out)) == {"orders_events": DEV}


# ── F-03 · a ref entry is matched by the id its fragment names ─────────────


def test_f03_an_overlay_entry_that_is_a_ref_patches_the_expose_its_fragment_names(
    tmp_path: Path,
) -> None:
    path = _case_b2(tmp_path / "p")

    document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"orders_events": DEV, "audit_events": "audit.prod"}
    assert [e["exposeId"] for e in document["exposes"]] == ["orders_events", "audit_events"]
    assert not _has_ref_key(document)


def test_f03_the_ref_entry_does_not_fail_as_naming_no_exposeid(tmp_path: Path) -> None:
    """Draft 22 case B2 failed every command with 'the overlay's exposes[0] names no exposeId'."""
    path = _case_b2(tmp_path / "p")

    from fluid_build.api.contract import load_contract

    assert load_contract(path, env="prod").contract["exposes"][1]["binding"]["location"] == {
        "topic": "audit.prod"
    }


def test_f03_the_cli_agrees(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = _case_b2(tmp_path / "p")
    out = tmp_path / "b.yaml"

    assert cli(["validate", path, "--env", "prod", "--strict"], capsys)[0] == 0
    rc, text = cli(["bundle", path, "--env", "prod", "--out", out], capsys)

    assert rc == 0, text
    assert "names no exposeId" not in text
    assert _topics(_read(out))["audit_events"] == "audit.prod"


# ── F-04 · an overlay's ref is relative to the overlay file ────────────────


def test_f04_a_root_level_overlay_ref_reads_the_file_beside_the_overlay(tmp_path: Path) -> None:
    root = tmp_path / "p"
    overlay_body = {"exposes": [{"exposeId": "orders_events", "binding": _PROD_BINDING}]}
    decoy = {
        "exposes": [{"exposeId": "orders_events", "binding": {"location": {"topic": "WRONG"}}}]
    }
    path = _put(
        root,
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            # The ref names ``./prod-binding.yaml``: beside the overlay, in overlays/.
            "overlays/prod.yaml": "{$ref: ./prod-binding.yaml}\n",
            "overlays/prod-binding.yaml": _dump(overlay_body),
            # A decoy where a ref relative to the CONTRACT would land.
            "prod-binding.yaml": _dump(decoy),
        },
    )

    document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"orders_events": PROD}


def test_f04_a_nested_overlay_ref_reads_the_file_beside_the_overlay(tmp_path: Path) -> None:
    root = tmp_path / "p"
    path = _put(
        root,
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders_events\n    binding: {$ref: ./prod-binding.yaml}\n"
            ),
            "overlays/prod-binding.yaml": _dump(_PROD_BINDING),
            "prod-binding.yaml": _dump({"location": {"topic": "WRONG"}}),
        },
    )
    from fluid_build.api.contract import load_contract

    loaded = load_contract(path, env="prod")

    assert _topics(loaded.contract) == {"orders_events": PROD}
    read = {f.relative_to(root.resolve()).as_posix() for f in loaded.files}
    assert "overlays/prod-binding.yaml" in read
    assert "prod-binding.yaml" not in read


# ── F-05 · an overlay's ref is confined, and reported where it was written ─


def test_f05_an_overlay_ref_may_reach_a_sibling_directory_inside_the_root(tmp_path: Path) -> None:
    path = _case_a(tmp_path / "p")  # ../fragments/prod-binding.yaml from overlays/

    assert _topics(loader.load_with_overlay(path, "prod")) == {"orders_events": PROD}


def test_f05_an_overlay_ref_above_the_root_is_an_escape_naming_the_overlay_and_pointer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "p"
    # Not parseable: an escape must be decided before the file is opened.
    _write(tmp_path / "x.yaml", "{{{ not yaml: [")
    path = _put(
        root,
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders_events\n    binding: {$ref: ../../x.yaml}\n"
            ),
        },
    )

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        loader.load_with_overlay(path, "prod")

    message = str(err.value)
    assert "../../x.yaml" in message
    assert str((root / "overlays" / "prod.yaml").resolve()) in message
    assert "/exposes/0/binding" in message
    assert err.value.pointer == "/exposes/0/binding"
    assert err.value.source == str((root / "overlays" / "prod.yaml").resolve())


# ── F-06 / F-07 · the widened root counts an overlay-only ref ──────────────


def _widened_tree(tmp_path: Path, *, workspace: Optional[str] = None) -> Path:
    """``ws/shared/prod.yaml`` and ``ws/p/``: a base with NO external ref, an
    overlay that reaches ``../../shared`` (one level above the contract's directory)."""
    ws = tmp_path / "ws"
    _write(ws / "shared" / "prod.yaml", _dump(_PROD_BINDING))
    if workspace is not None:
        _write(ws / "fluid.workspace.yaml", f"name: shop\n{workspace}\n")
    return _put(
        ws / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders_events\n"
                "    binding: {$ref: ../../shared/prod.yaml}\n"
            ),
        },
    )


def test_f06_without_a_widened_root_the_overlay_ref_is_an_escape(tmp_path: Path) -> None:
    path = _widened_tree(tmp_path)

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        loader.load_with_overlay(path, "prod")


def test_f06_the_env_root_applies_though_the_base_has_no_external_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    path = _widened_tree(tmp_path)
    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path / "ws"))

    document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"orders_events": PROD}
    rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)
    assert rc == 0, text


def test_f06_the_widened_root_still_confines_an_overlay_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wider root is a wider root, not no root: one level above it still escapes."""
    path = _widened_tree(tmp_path)
    _write(tmp_path / "elsewhere.yaml", "topic: stolen\n")
    _write(
        path.parent / "overlays" / "prod.yaml",
        "binding: {$ref: ../../../elsewhere.yaml}\n",
    )
    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path / "ws"))

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        loader.load_with_overlay(path, "prod")


@pytest.mark.parametrize("setting", ["ref_root: .", "refRoot: ."])
def test_f07_the_workspace_root_applies_though_the_base_has_no_external_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture, setting: str
) -> None:
    path = _widened_tree(tmp_path, workspace=setting)

    assert _topics(loader.load_with_overlay(path, "prod")) == {"orders_events": PROD}
    rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)
    assert rc == 0, text


def test_f07_the_workspace_root_does_not_apply_without_the_env(tmp_path: Path) -> None:
    """The base alone never needed the wider root: no overlay, no escape."""
    path = _widened_tree(tmp_path, workspace="ref_root: .")

    assert _topics(loader.load_with_overlay(path, None)) == {"orders_events": DEV}


# ── F-08 · siblings inside an overlay ──────────────────────────────────────


def test_f08_overlay_siblings_merge_over_the_fragment_then_over_the_base(tmp_path: Path) -> None:
    fragment = {
        "platform": "kafka",
        "format": "kafka_topic",
        "location": {"topic": "from-fragment", "partitions": 6},
    }
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "fragments/b.yaml": _dump(fragment),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders_events\n"
                "    binding: {$ref: ../fragments/b.yaml, location: {topic: x}}\n"
            ),
        },
    )

    binding = loader.load_with_overlay(path, "prod")["exposes"][0]["binding"]

    # The sibling topic wins over the fragment's; the fragment's other key stays;
    # the overlay then merges over the base binding key by key.
    assert binding == {
        "platform": "kafka",
        "format": "kafka_topic",
        "location": {"topic": "x", "partitions": 6},
    }


# ── F-09 · a pointer in the overlay resolves against base + overlay ────────


def test_f09_an_overlay_pointer_names_a_place_in_the_merged_contract(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(
                _base_doc(
                    extensions={"x": {"a": 1}},
                    exposes=[_topic_expose("orders_events", DEV)],
                )
            ),
            "overlays/prod.yaml": (
                "extensions: {x: {b: 2}}\n" 'metadata: {alias: {$ref: "#/extensions/x"}}\n'
            ),
        },
    )

    document = loader.load_with_overlay(path, "prod")

    # ``#/extensions/x`` is base ⊕ overlay: the key only the overlay adds is there.
    assert document["metadata"]["alias"] == {"a": 1, "b": 2}
    assert not _has_ref_key(document)


def test_f09_without_the_env_the_pointer_is_not_part_of_the_contract(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(extensions={"x": {"a": 1}})),
            "overlays/prod.yaml": 'metadata: {alias: {$ref: "#/extensions/x"}}\n',
        },
    )

    assert "alias" not in loader.load_with_overlay(path, None)["metadata"]


# ── F-10 · the overlay is never dropped, whatever the base holds ───────────


def _base_with_sibling_ref(root: Path) -> Path:
    """(a) the base holds a ref with siblings (draft 22 case C, first form)."""
    expose = _topic_expose("orders_events", DEV)
    expose["binding"]["properties"] = {"$ref": "../../kafka-props.yaml", "acks": "all"}
    return _put(
        root,
        {
            "kafka-props.yaml": "acks: '1'\nlinger.ms: '5'\n",
            "fragments/exposes/orders_events.yaml": _dump(expose),
            "overlays/prod.yaml": _dump(
                {
                    "exposes": [
                        {"exposeId": "orders_events", "binding": {"location": {"topic": PROD}}}
                    ]
                }
            ),
            "contract.fluid.yaml": _dump(
                _base_doc(exposes=[{"$ref": "./fragments/exposes/orders_events.yaml"}])
            ),
        },
    )


def _base_with_pointer(root: Path) -> Path:
    """(b) the base holds a same-document pointer (draft 22 case C2)."""
    expose = _topic_expose("orders_events", DEV)
    expose["binding"]["properties"] = {"$ref": "#/extensions/kafkaProps"}
    return _put(
        root,
        {
            "overlays/prod.yaml": _dump(
                {
                    "exposes": [
                        {"exposeId": "orders_events", "binding": {"location": {"topic": PROD}}}
                    ]
                }
            ),
            "contract.fluid.yaml": _dump(
                _base_doc(extensions={"kafkaProps": {"acks": "all"}}, exposes=[expose])
            ),
        },
    )


def _overlay_with_ref(root: Path) -> Path:
    """(c) the overlay itself holds a ref."""
    return _case_a(root)


_F10_SHAPES = {
    "base_sibling_ref": _base_with_sibling_ref,
    "base_pointer": _base_with_pointer,
    "overlay_ref": _overlay_with_ref,
}


@pytest.mark.parametrize("command", [c for c in _COMMANDS if c != "validate"])
@pytest.mark.parametrize("shape", sorted(_F10_SHAPES))
def test_f10_the_overlay_is_applied_every_time(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    shape: str,
    command: str,
) -> None:
    path = _F10_SHAPES[shape](tmp_path / "p")

    _prod_everywhere(path, tmp_path, capsys, caplog, command)


@pytest.mark.parametrize("shape", sorted(_F10_SHAPES))
def test_f10_validate_sees_the_overlay_so_an_invalid_overlay_value_fails_it(
    tmp_path: Path, capsys: pytest.CaptureFixture, shape: str
) -> None:
    """``validate`` emits no document: the proof is that a bad value that only the
    overlay holds fails it (a dropped overlay would let it pass)."""
    path = _F10_SHAPES[shape](tmp_path / "p")
    overlay = path.parent / "overlays" / "prod.yaml"
    document = _read(overlay)
    document["exposes"][0]["kind"] = "not_a_kind"
    _write(overlay, _dump(document))

    rc_prod, text = cli(["validate", path, "--env", "prod"], capsys)
    rc_base, base_text = cli(["validate", path], capsys)

    assert rc_prod == 1, text
    assert "not_a_kind" in text
    assert rc_base == 0, base_text


def test_f10_the_plan_of_the_raw_contract_and_of_its_bundle_are_one_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Draft 22 case C: ``plan C --env prod`` and ``plan <bundle --env prod> --env prod``
    planned two different topics. They now plan one contract and one list of actions.

    The catalog expects equal ``planDigest`` values. The digest also covers
    ``bindingMode`` and ``contract_metadata`` (the source path or the bundle), which
    differ between a raw contract and a bundle by design, so the digest of a plan
    from a bundle can never equal the digest of a plan from the contract it came from.
    The substance of the claim is what is compared here.
    """
    path = _base_with_sibling_ref(tmp_path / "p")
    bundle = tmp_path / "bundle.tgz"
    assert (
        cli(["bundle", path, "--env", "prod", "--format", "tgz", "--out", bundle], capsys)[0] == 0
    )

    raw = tmp_path / "raw.json"
    bound = tmp_path / "bound.json"
    assert cli(["plan", path, "--env", "prod", "--out", raw], capsys)[0] == 0
    assert cli(["plan", bundle, "--env", "prod", "--out", bound], capsys)[0] == 0
    from_raw = json.loads(raw.read_text(encoding="utf-8"))
    from_bundle = json.loads(bound.read_text(encoding="utf-8"))

    assert _topics(from_raw["contract"]) == {"orders_events": PROD}
    assert from_raw["contract"] == from_bundle["contract"]
    assert from_raw["actions"] == from_bundle["actions"]
    differing = {k for k in from_raw if from_raw[k] != from_bundle.get(k)}
    assert differing <= {
        "bindingMode",
        "bundleDigest",
        "contract_metadata",
        "generated_at",
        "planDigest",
    }


# ── F-11 · validate sees the overlay's fragment ────────────────────────────


def test_f11_an_unknown_key_only_the_overlays_binding_fragment_holds_fails_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _case_a(tmp_path / "p")
    _write(
        path.parent / "fragments" / "prod-binding.yaml",
        _dump({**_PROD_BINDING, "bogus": 1}),
    )

    rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)
    rc_base, base_text = cli(["validate", path], capsys)

    assert rc == 1, text
    assert "bogus" in text
    # The key is in the prod fragment only: without --env the contract is valid.
    assert rc_base == 0, base_text


def test_f11_a_bad_kind_only_the_overlays_expose_fragment_holds_fails_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _case_b(tmp_path / "p")
    _write(
        path.parent / "fragments" / "prod-expose.yaml",
        _dump(
            {
                "exposeId": "orders_events",
                "kind": "not_a_kind",
                "binding": {"location": {"topic": PROD}},
            }
        ),
    )

    rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)
    rc_base, base_text = cli(["validate", path], capsys)

    assert rc == 1, text
    assert "not_a_kind" in text
    assert rc_base == 0, base_text


# ── F-12 · an overlay ref that cannot be resolved fails, naming where ──────


def _broken_overlay(root: Path) -> Path:
    path = _case_a(root)
    (root / "fragments" / "prod-binding.yaml").unlink()
    return path


_F12_COMMANDS = {
    "validate": lambda p, t: ["validate", p, "--env", "prod"],
    "bundle": lambda p, t: ["bundle", p, "--env", "prod", "--out", t / "out" / "b.yaml"],
    "bundle_tgz": lambda p, t: [
        "bundle",
        p,
        "--env",
        "prod",
        "--format",
        "tgz",
        "--out",
        t / "out" / "b.tgz",
    ],
    "plan": lambda p, t: ["plan", p, "--env", "prod", "--out", t / "out" / "plan.json"],
    "odcs": lambda p, t: [
        "generate",
        "standard",
        p,
        "--format",
        "odcs",
        "--env",
        "prod",
        "--out",
        t / "out" / "x.odcs.yaml",
    ],
    "artifacts": lambda p, t: [
        "generate",
        "artifacts",
        p,
        "--env",
        "prod",
        "--out",
        t / "out" / "a",
    ],
}


@pytest.mark.parametrize("command", sorted(_F12_COMMANDS))
def test_f12_a_missing_overlay_fragment_fails_every_command_naming_where(
    tmp_path: Path, capsys: pytest.CaptureFixture, command: str
) -> None:
    path = _broken_overlay(tmp_path / "p")
    (tmp_path / "out").mkdir()

    rc, text = cli(_F12_COMMANDS[command](path, tmp_path), capsys)

    assert rc != 0, text
    # The ref as written, the overlay it was written in, and its JSON pointer.
    assert "../fragments/prod-binding.yaml" in text
    assert "overlays/prod.yaml" in text
    assert "/exposes/0/binding" in text
    # Never a fallback to the base: nothing was written.
    assert list((tmp_path / "out").iterdir()) == []


def test_f12_the_library_names_the_ref_the_overlay_and_the_pointer(tmp_path: Path) -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract

    path = _broken_overlay(tmp_path / "p")

    with pytest.raises(ContractLoadError) as err:
        load_contract(path, env="prod")

    assert err.value.event == "contract_ref_unresolved"
    assert "../fragments/prod-binding.yaml" in str(err.value)
    assert "overlays/prod.yaml" in str(err.value)
    assert "/exposes/0/binding" in str(err.value)


def test_f12_the_loader_never_falls_back_to_the_base(tmp_path: Path) -> None:
    path = _broken_overlay(tmp_path / "p")

    with pytest.raises(RefResolutionError, match="not found"):
        loader.load_with_overlay(path, "prod")
    # The base alone is fine, so the failure is the overlay's.
    assert _topics(loader.load_with_overlay(path, None)) == {"orders_events": DEV}


# ── F-13 · an env with no overlay file ─────────────────────────────────────


def test_f13_an_env_with_no_overlay_warns_and_the_base_is_used(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _put(
        tmp_path / "p", {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))}
    )

    rc, text = cli(["validate", path, "--env", "prod"], capsys)

    assert rc == 0, text
    assert "overlay_not_found" in text
    assert "BASE contract is used unchanged" in text


def test_f13_a_workspace_that_declares_the_env_refuses_the_missing_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    ws = tmp_path / "ws"
    _write(ws / "fluid.workspace.yaml", "name: shop\nexpected-environments: {p: [prod]}\n")
    path = _put(
        ws / "p", {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))}
    )

    rc, text = cli(["validate", path, "--env", "prod"], capsys)

    assert rc == 1, text
    assert "declares 'prod' an environment of this product" in text
    assert "overlay_not_found" not in text


def test_f13_the_library_reports_the_declared_but_missing_overlay_by_its_event(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract

    ws = tmp_path / "ws"
    _write(ws / "fluid.workspace.yaml", "name: shop\nexpected-environments: {p: [prod]}\n")
    path = _put(
        ws / "p", {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))}
    )

    with pytest.raises(ContractLoadError) as err:
        load_contract(path, env="prod")

    assert err.value.event == "overlay_declared_but_missing"


# ── F-14 / F-15 · a malformed or JSON overlay ──────────────────────────────


def test_f14_a_yaml_syntax_error_in_the_overlay_names_the_file_and_the_line(
    tmp_path: Path,
) -> None:
    path = _put(
        tmp_path / "p", {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))}
    )
    overlay = _write(path.parent / "overlays" / "prod.yaml", "extensions:\n  ok: 1\n  bad: a: b\n")

    with pytest.raises(RuntimeError) as err:
        loader.load_with_overlay(path, "prod")

    assert str(overlay) in str(err.value)
    assert "line 3" in str(err.value)


def test_f14_the_library_maps_it_to_a_parse_failure_naming_the_overlay(tmp_path: Path) -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract

    path = _put(
        tmp_path / "p", {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))}
    )
    overlay = _write(path.parent / "overlays" / "prod.yaml", "extensions:\n  ok: 1\n  bad: a: b\n")

    with pytest.raises(ContractLoadError) as err:
        load_contract(path, env="prod")

    assert err.value.event == "contract_parse_failed"
    assert str(overlay.resolve()) in str(err.value)


def test_f15_a_json_overlay_with_refs_composes_like_a_yaml_one(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "fragments/prod-binding.json": json.dumps(_PROD_BINDING),
            "overlays/prod.json": json.dumps(
                {
                    "exposes": [
                        {
                            "exposeId": "orders_events",
                            "binding": {"$ref": "../fragments/prod-binding.json"},
                        }
                    ]
                }
            ),
        },
    )

    document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"orders_events": PROD}
    assert not _has_ref_key(document)


def test_f15_a_json_overlay_that_refs_a_yaml_fragment_and_back(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "fragments/prod-binding.yaml": _dump(_PROD_BINDING),
            "overlays/prod.json": json.dumps(
                {
                    "exposes": [
                        {
                            "exposeId": "orders_events",
                            "binding": {"$ref": "../fragments/prod-binding.yaml"},
                        }
                    ]
                }
            ),
        },
    )

    assert _topics(loader.load_with_overlay(path, "prod")) == {"orders_events": PROD}


# ── F-16 · per-environment SQL ─────────────────────────────────────────────

_SQL_DEV = "-- S2-DEV\nSELECT 1 AS id\n"
_SQL_PROD = "-- S2-PROD\nSELECT 1 AS id\n"


def _sql_product(root: Path) -> Path:
    contract = _base_doc(
        id="refs.sql",
        builds=[
            {
                "id": "transform",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": {"$ref": "./sql/transform.sql"}},
            }
        ],
        exposes=[_topic_expose("out", "out.topic")],
    )
    return _put(
        root,
        {
            "contract.fluid.yaml": _dump(contract),
            "sql/transform.sql": _SQL_DEV,
            "sql/transform.prod.sql": _SQL_PROD,
            "overlays/prod.yaml": (
                "builds:\n  - id: transform\n    properties:\n"
                "      sql: {$ref: ../sql/transform.prod.sql}\n"
            ),
        },
    )


def test_f16_the_overlay_swaps_in_the_prod_sql_text_and_the_base_keeps_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _sql_product(tmp_path / "p")

    rc_prod, text = cli(["bundle", path, "--env", "prod", "--out", tmp_path / "prod.yaml"], capsys)
    rc_base, base_text = cli(["bundle", path, "--out", tmp_path / "base.yaml"], capsys)

    assert rc_prod == 0, text
    assert rc_base == 0, base_text
    assert _read(tmp_path / "prod.yaml")["builds"][0]["properties"]["sql"] == _SQL_PROD
    assert _read(tmp_path / "base.yaml")["builds"][0]["properties"]["sql"] == _SQL_DEV


def test_f16_the_prod_sql_reaches_the_plan_and_the_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _sql_product(tmp_path / "p")
    plan = tmp_path / "plan.json"

    assert cli(["plan", path, "--env", "prod", "--out", plan], capsys)[0] == 0
    planned = json.loads(plan.read_text(encoding="utf-8"))

    assert planned["contract"]["builds"][0]["properties"]["sql"] == _SQL_PROD
    assert "S2-DEV" not in json.dumps(planned)


# ── F-17 · overlay merge semantics are unchanged (SG-20) ───────────────────


def test_f17_a_null_in_the_overlay_assigns_null_and_keeps_the_key(tmp_path: Path) -> None:
    """Characterization (SG-20): an overlay merges with ``_deep_merge``, so a ``null``
    assigns; a ``$ref`` sibling merges with RFC 7386, where ``null`` removes."""
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(extensions={"a": 1, "b": 2})),
            "overlays/prod.yaml": "extensions: {a: null}\n",
        },
    )

    extensions = loader.load_with_overlay(path, "prod")["extensions"]

    assert "a" in extensions and extensions["a"] is None
    assert extensions["b"] == 2


def _two_exposes(version: str) -> Dict[str, Any]:
    return _base_doc(
        fluidVersion=version,
        exposes=[_topic_expose("a", "dev-a"), _topic_expose("b", "dev-b")],
    )


_REVERSED_OVERLAY = (
    "exposes:\n"
    "  - exposeId: b\n    binding: {location: {topic: prod-b}}\n"
    "  - exposeId: a\n    binding: {location: {topic: prod-a}}\n"
)


def test_f17_from_0_7_6_a_list_of_dicts_merges_by_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_two_exposes("0.7.6")),
            "overlays/prod.yaml": _REVERSED_OVERLAY,
        },
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"a": "prod-a", "b": "prod-b"}
    assert "overlay_patch_retargeted" not in caplog.text


def test_f17_before_0_7_6_a_list_of_dicts_merges_by_position_with_the_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_two_exposes("0.7.5")),
            "overlays/prod.yaml": _REVERSED_OVERLAY,
        },
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        document = loader.load_with_overlay(path, "prod")

    # By position: the overlay's first entry (b) patched the base's first (a).
    assert [e["binding"]["location"]["topic"] for e in document["exposes"]] == [
        "prod-b",
        "prod-a",
    ]
    assert caplog.text.count("overlay_patch_retargeted") == 2
    assert "set fluidVersion 0.7.6 or later to merge exposes by exposeId" in caplog.text


# ── F-18 · positional merge with ref entries ───────────────────────────────


def test_f18_refs_resolve_before_the_positional_merge_and_the_warnings_name_their_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_two_exposes("0.7.5")),
            "fragments/b.yaml": "exposeId: b\nbinding: {location: {topic: prod-b}}\n",
            "fragments/a.yaml": "exposeId: a\nbinding: {location: {topic: prod-a}}\n",
            "overlays/prod.yaml": (
                "exposes:\n  - {$ref: ../fragments/b.yaml}\n  - {$ref: ../fragments/a.yaml}\n"
            ),
        },
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        document = loader.load_with_overlay(path, "prod")

    # The warning can only name "exposeId b" if the ref was resolved before the merge.
    assert "the overlay's exposes[0] names exposeId b but patches the base's exposes[0]" in (
        caplog.text
    )
    assert "the overlay's exposes[1] names exposeId a but patches the base's exposes[1]" in (
        caplog.text
    )
    assert not _has_ref_key(document)
    assert [e["binding"]["location"]["topic"] for e in document["exposes"]] == [
        "prod-b",
        "prod-a",
    ]


def test_f18_validate_strict_fails_on_the_retargeted_patch_a_ref_entry_names(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The retarget warning is a strict warning: ``validate --strict`` must see it for a
    ref entry too (it could not, while an overlay's refs were never resolved)."""
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_two_exposes("0.7.5")),
            "fragments/b.yaml": "exposeId: b\nbinding: {location: {topic: prod-b}}\n",
            "overlays/prod.yaml": "exposes:\n  - {$ref: ../fragments/b.yaml}\n",
        },
    )

    rc, text = cli(["validate", path, "--env", "prod", "--strict"], capsys)

    assert rc == 1, text
    assert "overlay_patch_retargeted" in text


# ── F-19 · a new entry added through a ref ─────────────────────────────────


def test_f19_an_overlay_ref_to_an_unknown_id_appends_the_resolved_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", "dev-a")])),
            "fragments/c.yaml": _dump(_topic_expose("c", "prod-c")),
            "overlays/prod.yaml": "exposes:\n  - {$ref: ../fragments/c.yaml}\n",
        },
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        document = loader.load_with_overlay(path, "prod")

    assert [e["exposeId"] for e in document["exposes"]] == ["a", "c"]
    assert document["exposes"][1] == _topic_expose("c", "prod-c")
    assert "overlay_entry_added" in caplog.text
    assert not _has_ref_key(document)


# ── F-20 · a split base and an overlay in reverse order ────────────────────


def _builds_and_exposes() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    builds = [
        {
            "id": "ingest_orders",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1 AS o"},
        },
        {
            "id": "ingest_dlq",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 2 AS d"},
        },
    ]
    exposes = [
        _topic_expose("orders_events", "orders.dev.events"),
        _topic_expose("orders_dead_letter", "orders.dev.dlq"),
    ]
    return builds, exposes


_REVERSE_OVERLAY = {
    "builds": [
        {"id": "ingest_dlq", "properties": {"sql": "SELECT 20 AS d"}},
        {"id": "ingest_orders", "properties": {"sql": "SELECT 10 AS o"}},
    ],
    "exposes": [
        {"exposeId": "orders_dead_letter", "binding": {"location": {"topic": "orders.prod.dlq"}}},
        {"exposeId": "orders_events", "binding": {"location": {"topic": PROD}}},
    ],
}


def test_f20_a_split_base_with_a_reversed_overlay_bundles_as_its_flat_twin_does(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    builds, exposes = _builds_and_exposes()
    flat = _put(
        tmp_path / "flat",
        {
            "contract.fluid.yaml": _dump(_base_doc(builds=builds, exposes=exposes)),
            "overlays/prod.yaml": _dump(_REVERSE_OVERLAY),
        },
    )
    files = {f"fragments/builds/{b['id']}.yaml": _dump(b) for b in builds} | {
        f"fragments/exposes/{e['exposeId']}.yaml": _dump(e) for e in exposes
    }
    files["overlays/prod.yaml"] = _dump(_REVERSE_OVERLAY)
    files["contract.fluid.yaml"] = _dump(
        _base_doc(
            builds=[{"$ref": f"./fragments/builds/{b['id']}.yaml"} for b in builds],
            exposes=[{"$ref": f"./fragments/exposes/{e['exposeId']}.yaml"} for e in exposes],
        )
    )
    split = _put(tmp_path / "split", files)

    assert cli(["bundle", flat, "--env", "prod", "--out", tmp_path / "flat.yaml"], capsys)[0] == 0
    rc, text = cli(["bundle", split, "--env", "prod", "--out", tmp_path / "split.yaml"], capsys)

    assert rc == 0, text
    assert (tmp_path / "split.yaml").read_bytes() == (tmp_path / "flat.yaml").read_bytes()
    bundled = _read(tmp_path / "split.yaml")
    # Every patch landed on its own id although the overlay lists them reversed.
    assert [b["properties"]["sql"] for b in bundled["builds"]] == [
        "SELECT 10 AS o",
        "SELECT 20 AS d",
    ]
    assert _topics(bundled) == {"orders_events": PROD, "orders_dead_letter": "orders.prod.dlq"}


# ── F-21 / F-22 · an overlay value over a fragment-sourced value ───────────


def test_f21_an_inline_overlay_value_wins_over_what_the_base_took_from_a_text_ref(
    tmp_path: Path,
) -> None:
    path = _sql_product(tmp_path / "p")
    _write(
        path.parent / "overlays" / "prod.yaml",
        "builds:\n  - id: transform\n    properties: {sql: SELECT 99}\n",
    )

    assert loader.load_with_overlay(path, "prod")["builds"][0]["properties"]["sql"] == "SELECT 99"
    assert loader.load_with_overlay(path, None)["builds"][0]["properties"]["sql"] == _SQL_DEV


def test_f22_an_overlay_value_applies_on_top_of_a_sibling_merged_node(tmp_path: Path) -> None:
    expose = _topic_expose("orders_events", DEV)
    expose["binding"] = {
        "$ref": "./shared/binding.yaml",
        "location": {"topic": "sibling-topic", "region": "eu"},
    }
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[expose])),
            "shared/binding.yaml": _dump(
                {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "target-topic", "partitions": 3},
                }
            ),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders_events\n"
                "    binding: {location: {topic: overlay-topic}}\n"
            ),
        },
    )

    base = loader.load_with_overlay(path, None)["exposes"][0]["binding"]
    prod = loader.load_with_overlay(path, "prod")["exposes"][0]["binding"]

    assert base["location"] == {"topic": "sibling-topic", "partitions": 3, "region": "eu"}
    # The overlay wins on the key it names; the merged node's other keys stay.
    assert prod["location"] == {"topic": "overlay-topic", "partitions": 3, "region": "eu"}
    assert prod["platform"] == "kafka"


# ── F-23 · overlay candidates beside the contract ──────────────────────────


@pytest.mark.parametrize("candidate", ["prod.yaml", "contract.fluid.prod.yaml", "prod.json"])
def test_f23_an_overlay_beside_the_contract_resolves_refs_relative_to_its_own_directory(
    tmp_path: Path, candidate: str
) -> None:
    body = {"exposes": [{"exposeId": "orders_events", "binding": {"$ref": "./fragments/pb.yaml"}}]}
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "fragments/pb.yaml": _dump(_PROD_BINDING),
            candidate: json.dumps(body) if candidate.endswith(".json") else _dump(body),
        },
    )
    from fluid_build.api.contract import load_contract

    loaded = load_contract(path, env="prod")

    assert _topics(loaded.contract) == {"orders_events": PROD}
    assert loaded.overlay == (path.parent / candidate).resolve()


@pytest.mark.xfail(
    strict=True,
    reason="BUG F23-docs-lists-an-overlay-as-a-product: `fluid docs --src` globs "
    "contract.fluid.*, which also matches the overlay candidate contract.fluid.<env>.yaml, and "
    "writes it a page and an index row with no id",
)
def test_f23_docs_do_not_list_an_overlay_beside_the_contract_as_a_product(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    product = tmp_path / "src" / "p"
    _put(
        product,
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "contract.fluid.prod.yaml": "extensions: {x: 1}\n",
        },
    )

    rc, text = cli(["docs", "--src", tmp_path / "src", "--out", tmp_path / "site"], capsys)

    assert rc == 0, text
    rows = json.loads((tmp_path / "site" / "index.json").read_text(encoding="utf-8"))
    assert [Path(row["path"]).name for row in rows] == ["contract.fluid.yaml"]
    assert [p.name for p in (tmp_path / "site").glob("contract-*.html")] == [
        "contract-commerce-orders-events.html"
    ]


# ── F-24 · a ref decides the version that decides the merge ────────────────


def test_f24_the_overlays_fluidversion_ref_is_resolved_before_the_keyed_merge_reads_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_two_exposes("0.7.5")),
            "v.yaml": "v: '0.7.6'\n",
            "overlays/prod.yaml": "fluidVersion: {$ref: ../v.yaml#/v}\n" + _REVERSED_OVERLAY,
        },
    )

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        document = loader.load_with_overlay(path, "prod")

    assert document["fluidVersion"] == "0.7.6"
    # Merged by id although the base is 0.7.5: each patch landed on its own entry.
    assert _topics(document) == {"a": "prod-a", "b": "prod-b"}
    assert "overlay_patch_retargeted" not in caplog.text


# ── F-25 · a consumes entry that is a ref is matched by both keys ──────────


def test_f25_a_consumes_ref_entry_is_matched_by_productid_and_exposeid(tmp_path: Path) -> None:
    consumes = [
        {"productId": "up.a", "exposeId": "x", "pin": "1"},
        {"productId": "up.a", "exposeId": "y", "pin": "1"},
    ]
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(
                _base_doc(consumes=consumes, exposes=[_topic_expose("a", "dev-a")])
            ),
            "fragments/cy.yaml": _dump({"productId": "up.a", "exposeId": "y", "pin": "2"}),
            "overlays/prod.yaml": "consumes:\n  - {$ref: ../fragments/cy.yaml}\n",
        },
    )

    document = loader.load_with_overlay(path, "prod")

    # Same productId on both entries: only the exposeId tells them apart.
    assert [c["pin"] for c in document["consumes"]] == ["1", "2"]
    assert not _has_ref_key(document)


# ── F-26 · provenance of overlay files ─────────────────────────────────────


def test_f26_the_files_list_the_source_its_refs_the_overlay_and_its_refs_in_that_order(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import load_contract

    path = _case_a(tmp_path / "p")
    root = path.parent.resolve()

    loaded = load_contract(path, env="prod")

    relative = [f.relative_to(root).as_posix() for f in loaded.files]
    # The documented order (LoadedContract.files): the source, each ref target of
    # the base, then the overlay and the targets of its refs (SG-28).
    assert relative == [
        "contract.fluid.yaml",
        "fragments/exposes/orders_events.yaml",
        "overlays/prod.yaml",
        "fragments/prod-binding.yaml",
    ]
    assert loaded.overlay == root / "overlays" / "prod.yaml"
    assert loaded.source == root / "contract.fluid.yaml"


def test_f26_without_the_env_no_overlay_file_is_listed(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    path = _case_a(tmp_path / "p")

    loaded = load_contract(path)

    assert loaded.overlay is None
    assert all("overlays" not in f.parts[-2:] for f in loaded.files)
    assert [f.name for f in loaded.files] == ["contract.fluid.yaml", "orders_events.yaml"]


# ── F-27 · a bundle records its overlay, and is stable ─────────────────────


def _manifest(bundle: Path) -> Dict[str, Any]:
    with tarfile.open(bundle, "r:gz") as tar:
        member = tar.extractfile("MANIFEST.json")
        assert member is not None
        return json.loads(member.read().decode("utf-8"))


def test_f27_the_manifest_names_the_overlay_and_two_bundles_have_one_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _case_a(tmp_path / "p")
    first, second = tmp_path / "one.tgz", tmp_path / "two.tgz"

    rc1, text1 = cli(["bundle", path, "--env", "prod", "--format", "tgz", "--out", first], capsys)
    rc2, text2 = cli(["bundle", path, "--env", "prod", "--format", "tgz", "--out", second], capsys)

    assert (rc1, rc2) == (0, 0), (text1, text2)
    one, two = _manifest(first), _manifest(second)
    assert one["source"]["overlay"] == "overlays/prod.yaml"
    assert one["source"]["env"] == "prod"
    assert one["digest"] == two["digest"]
    assert one == two
    assert first.read_bytes() == second.read_bytes()


def test_f27_a_bundle_without_an_env_records_no_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _case_a(tmp_path / "p")
    out = tmp_path / "base.tgz"

    assert cli(["bundle", path, "--format", "tgz", "--out", out], capsys)[0] == 0

    assert _manifest(out)["source"].get("overlay") is None


# ── F-28 · the same fragment from the base and from the overlay ────────────


def test_f28_a_fragment_both_the_base_and_the_overlay_ref_is_not_a_cycle(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(
                _base_doc(
                    extensions={"shared": {"$ref": "./shared/x.yaml"}},
                    exposes=[_topic_expose("a", "dev-a")],
                )
            ),
            "shared/x.yaml": "k: v\n",
            "overlays/prod.yaml": "extensions: {alsoShared: {$ref: ../shared/x.yaml}}\n",
        },
    )

    extensions = loader.load_with_overlay(path, "prod")["extensions"]

    assert extensions == {"shared": {"k": "v"}, "alsoShared": {"k": "v"}}


def test_f28_the_expansion_budget_is_one_for_the_base_and_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What refs bring into a contract is capped as a whole (spec 7): the overlay's refs are
    counted with the base's, not on a budget of their own."""
    from fluid_build import contract_refs

    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(
                _base_doc(extensions={"a": {"$ref": "./a.yaml"}}, exposes=[_topic_expose("a", "t")])
            ),
            "a.yaml": _dump({"items": list(range(30))}),
            "b.yaml": _dump({"items": list(range(40))}),
            "overlays/prod.yaml": "extensions: {b: {$ref: ../b.yaml}}\n",
        },
    )
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 60)

    assert len(loader.load_with_overlay(path, None)["extensions"]["a"]["items"]) == 30
    with pytest.raises(
        RefResolutionError, match="takes what refs bring into the contract past"
    ) as err:
        loader.load_with_overlay(path, "prod")

    # It is the overlay's ref that tips it over, and the error says where it was written.
    assert "../b.yaml" in str(err.value) and "overlays/prod.yaml" in str(err.value)


# ── F-29 · no state is shared between two loads ────────────────────────────


def test_f29_loading_prod_then_dev_in_one_process_leaves_no_prod_value_in_dev(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import load_contract

    path = _case_a(tmp_path / "p")

    prod = load_contract(path, env="prod")
    dev = load_contract(path, env="dev")

    assert _topics(prod.contract) == {"orders_events": PROD}
    assert _topics(dev.contract) == {"orders_events": DEV}
    assert PROD not in json.dumps(dev.contract)
    assert dev.overlay is None


def test_f29_a_result_the_caller_changes_changes_no_later_load(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    path = _case_a(tmp_path / "p")

    first = load_contract(path, env="prod")
    first.contract["exposes"][0]["binding"]["location"]["topic"] = "MUTATED"
    first.contract["metadata"]["owner"]["team"] = "MUTATED"
    second = load_contract(path, env="prod")

    assert _topics(second.contract) == {"orders_events": PROD}
    assert second.contract["metadata"]["owner"]["team"] == "commerce-data"
    assert "MUTATED" not in json.dumps(second.contract)


def test_f29_two_entries_made_from_one_fragment_share_no_object(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(
                _base_doc(
                    exposes=[
                        {"$ref": "./e.yaml", "exposeId": "one"},
                        {"$ref": "./e.yaml", "exposeId": "two"},
                    ]
                )
            ),
            "e.yaml": _dump(_topic_expose("template", "shared.topic")),
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: one\n    binding: {location: {topic: prod-one}}\n"
            ),
        },
    )

    document = loader.load_with_overlay(path, "prod")

    assert _topics(document) == {"one": "prod-one", "two": "shared.topic"}
    document["exposes"][1]["binding"]["location"]["topic"] = "MUTATED"
    assert document["exposes"][0]["binding"]["location"]["topic"] == "prod-one"


# ── F-30 · a cycle among an overlay's fragments ────────────────────────────


def test_f30_a_cycle_in_an_overlay_fragment_is_an_error_naming_the_fragment(
    tmp_path: Path,
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", "dev-a")])),
            "overlays/prod.yaml": "extensions: {x: {$ref: ../fragments/a.yaml}}\n",
            "fragments/a.yaml": "next: {$ref: ./b.yaml}\n",
            "fragments/b.yaml": "back: {$ref: ./a.yaml}\n",
        },
    )

    with pytest.raises(RefResolutionError, match="Circular \\$ref detected") as err:
        loader.load_with_overlay(path, "prod")

    assert "fragments/a.yaml" in str(err.value)
    assert "fragments/b.yaml" in str(err.value)
    # The base alone has no cycle.
    assert loader.load_with_overlay(path, None)["id"] == "commerce.orders_events"


# ── F-31 / F-32 · an overlay in another place, or entirely a ref ───────────


def test_f31_an_overlays_directory_that_is_a_symlink_out_of_the_root_is_refused(
    tmp_path: Path,
) -> None:
    """SG-15, decided. ``overlays/`` is a link to a directory outside the contract's
    tree: the overlay file inside it is not read, as a ref to it would not be (the ref
    root holds after symlinks are resolved). It is refused, naming the overlay, on every
    entry point, so a link planted in an uploaded tree cannot pull a host's
    ``<env>.yaml`` into a bundle."""
    outside = tmp_path / "shared-overlays"
    _write(
        outside / "prod.yaml",
        "exposes:\n  - exposeId: orders_events\n    binding: {location: {topic: FROM-OUTSIDE}}\n",
    )
    product = tmp_path / "p"
    path = _put(
        product,
        {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)]))},
    )
    _symlink(product / "overlays", outside)
    from fluid_build.api.contract import ContractLoadError, load_contract

    with pytest.raises(ContractLoadError) as info:
        load_contract(path, env="prod")
    assert info.value.event == "contract_ref_unresolved"
    for call in (
        lambda: loader.load_with_overlay(path, "prod"),
        lambda: loader.compose_contract(path, "prod"),
        lambda: loader.load_overlay_document(path, "prod"),
    ):
        with pytest.raises(RefConfinementError, match="escapes the ref root") as refused:
            call()
        assert "overlays/prod.yaml" in str(refused.value)
        assert "FROM-OUTSIDE" not in str(refused.value)


def test_f31_a_symlinked_overlay_is_refused_before_its_refs_are_resolved(
    tmp_path: Path,
) -> None:
    """The overlay's own place is checked before it is read, so its refs (which would
    resolve relative to the real file, and be confined) are never reached."""
    outside = tmp_path / "shared-overlays"
    _write(
        outside / "prod.yaml",
        "exposes:\n  - exposeId: orders_events\n    binding: {$ref: ../fragments/b.yaml}\n",
    )
    _write(tmp_path / "fragments" / "b.yaml", _dump(_PROD_BINDING))
    product = tmp_path / "p"
    path = _put(
        product,
        {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)]))},
    )
    _symlink(product / "overlays", outside)

    with pytest.raises(RefConfinementError, match="escapes the ref root") as info:
        loader.load_with_overlay(path, "prod")

    assert str(info.value).startswith(f"overlay {product.resolve() / 'overlays' / 'prod.yaml'} ")
    assert "../fragments/b.yaml" not in str(info.value)


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_f31_an_overlay_candidate_that_is_not_a_regular_file_is_refused_not_read(
    tmp_path: Path,
) -> None:
    candidate = "overlays/prod.yaml"
    """Run in a child with a timeout: the defect is a hang. The read must be refused with an
    error that names the overlay, as a FIFO behind a ``$ref`` is."""
    product = tmp_path / "p"
    path = _put(
        product,
        {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)]))},
    )
    (product / candidate).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(product / candidate)
    code = (
        "import sys; from fluid_build import loader; "
        "loader.load_with_overlay(sys.argv[1], 'prod'); print('LOADED')"
    )
    env = {**os.environ, "PYTHONPATH": str(_CHECKOUT)}

    done = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=8,
    )

    assert done.returncode != 0 and "LOADED" not in done.stdout
    assert "prod.yaml" in done.stderr


def test_f32_an_overlay_that_is_entirely_a_ref_merges_when_the_target_is_an_object(
    tmp_path: Path,
) -> None:
    """SG-19, characterization: a root-level ``$ref`` in an overlay is allowed."""
    body = {"exposes": [{"exposeId": "orders_events", "binding": _PROD_BINDING}]}
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "shared/prod-overlay.yaml": _dump(body),
            "overlays/prod.yaml": "{$ref: ../shared/prod-overlay.yaml}\n",
        },
    )

    assert _topics(loader.load_with_overlay(path, "prod")) == {"orders_events": PROD}


def test_f32_an_overlay_ref_to_a_list_is_refused_as_not_an_object(tmp_path: Path) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("orders_events", DEV)])),
            "shared/list.yaml": "- a\n- b\n",
            "overlays/prod.yaml": "{$ref: ../shared/list.yaml}\n",
        },
    )

    with pytest.raises(ValueError, match="Overlay root must be an object/dict"):
        loader.load_with_overlay(path, "prod")


# ── F-33 · a null $ref in the overlay ──────────────────────────────────────


def test_f33_a_null_ref_in_the_overlay_must_be_a_string_and_names_the_overlay(
    tmp_path: Path,
) -> None:
    path = _put(
        tmp_path / "p",
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", "dev-a")])),
            "overlays/prod.yaml": "extensions: {x: {$ref: null}}\n",
        },
    )

    with pytest.raises(RefResolutionError, match="must be a string") as err:
        loader.load_with_overlay(path, "prod")

    assert str((path.parent / "overlays" / "prod.yaml").resolve()) in str(err.value)
    assert "/extensions/x" in str(err.value)


# ── F-34 · --env dev with no dev overlay ───────────────────────────────────


def test_f34_env_dev_without_a_dev_overlay_is_the_base_by_convention(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _put(
        tmp_path / "p",
        {"contract.fluid.yaml": _dump(_base_doc(exposes=[_topic_expose("a", DEV)]))},
    )

    rc, text = cli(["validate", path, "--env", "dev"], capsys)

    assert rc == 0, text
    assert "overlay_base_env" in text
    assert "base by convention" in text
    assert "overlay_not_found" not in text


# ── F-35 · generate artifacts composes without an env ──────────────────────


def _split_contract(root: Path) -> Path:
    builds, exposes = _builds_and_exposes()
    files = {f"fragments/builds/{b['id']}.yaml": _dump(b) for b in builds} | {
        f"fragments/exposes/{e['exposeId']}.yaml": _dump(e) for e in exposes
    }
    files["contract.fluid.yaml"] = _dump(
        _base_doc(
            builds=[{"$ref": f"./fragments/builds/{b['id']}.yaml"} for b in builds],
            exposes=[{"$ref": f"./fragments/exposes/{e['exposeId']}.yaml"} for e in exposes],
        )
    )
    return _put(root, files)


def _avsc_and_pointer_contract(root: Path) -> Path:
    expose = _topic_expose("orders_events", DEV)
    expose["binding"]["properties"] = {"avroSchema": {"$ref": "./schemas/orders-value.avsc"}}
    expose["contract"]["quality"] = {"$ref": "./quality.yaml#/rules"}
    return _put(
        root,
        {
            "contract.fluid.yaml": _dump(_base_doc(exposes=[expose])),
            "schemas/orders-value.avsc": json.dumps(
                {
                    "type": "record",
                    "name": "Order",
                    "fields": [{"name": "order_id", "type": "string"}],
                }
            ),
            "quality.yaml": _dump(
                {
                    "rules": [
                        {
                            "rule": "id_present",
                            "expression": "order_id IS NOT NULL",
                            "severity": "error",
                        }
                    ]
                }
            ),
        },
    )


_F35_SHAPES = {"split": _split_contract, "avsc_and_pointer": _avsc_and_pointer_contract}


@pytest.mark.parametrize("shape", sorted(_F35_SHAPES))
def test_f35_generate_artifacts_without_an_env_composes_the_refs(
    tmp_path: Path, capsys: pytest.CaptureFixture, shape: str
) -> None:
    path = _F35_SHAPES[shape](tmp_path / "p")
    out = tmp_path / "artifacts"

    rc, text = cli(["generate", "artifacts", path, "--out", out], capsys)

    assert rc == 0, text
    assert "no usable name" not in text
    documents = _tree_documents(out)
    assert documents
    assert not any(_has_ref_key(doc) for _, doc in documents), [
        name for name, doc in documents if _has_ref_key(doc)
    ]


@pytest.mark.parametrize("shape", sorted(_F35_SHAPES))
def test_f35_the_artifacts_equal_those_of_the_bundle_of_the_same_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture, shape: str
) -> None:
    path = _F35_SHAPES[shape](tmp_path / "p")
    flat = tmp_path / "flat" / "contract.fluid.yaml"
    from_contract, from_bundle = tmp_path / "from-contract", tmp_path / "from-bundle"

    assert cli(["bundle", path, "--out", flat], capsys)[0] == 0
    assert cli(["generate", "artifacts", path, "--out", from_contract], capsys)[0] == 0
    assert cli(["generate", "artifacts", flat, "--out", from_bundle], capsys)[0] == 0

    names = sorted(
        p.relative_to(from_contract).as_posix() for p in from_contract.rglob("*") if p.is_file()
    )
    assert names == sorted(
        p.relative_to(from_bundle).as_posix() for p in from_bundle.rglob("*") if p.is_file()
    )
    for name in names:
        assert (from_contract / name).read_bytes() == (from_bundle / name).read_bytes(), name


_POINTER_BASE = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: d.p
name: P
extensions:
  shared:
    alpha: {exposeId: alpha, kind: table, binding: {platform: local, format: csv, location: {path: a.csv}}}
    beta: {exposeId: beta, kind: table, binding: {platform: local, format: csv, location: {path: b.csv}}}
    both: [{$ref: "#/extensions/shared/alpha"}, {$ref: "#/extensions/shared/beta"}]
"""


@pytest.mark.parametrize(
    "exposes",
    [
        '[{$ref: "#/extensions/shared/alpha"}, {$ref: "#/extensions/shared/beta"}]',
        '{$ref: "#/extensions/shared/both"}',
    ],
    ids=["entries-are-pointers", "the-list-is-a-pointer"],
)
def test_an_overlay_over_a_base_pointer_is_refused_not_misapplied(
    tmp_path: Path, exposes: str
) -> None:
    """Pointers resolve after the overlay: matched by id it would land by position, and a
    list over a pointer would drop what it names. Both are refused, naming the place."""
    from fluid_build.loader import compose_contract
    from fluid_build.util.ref_confinement import RefResolutionError

    (tmp_path / "contract.fluid.yaml").write_text(
        _POINTER_BASE + f"exposes: {exposes}\n", encoding="utf-8"
    )
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        "exposes:\n  - exposeId: beta\n    binding: {location: {path: prod-b.csv}}\n",
        encoding="utf-8",
    )

    assert compose_contract(tmp_path / "contract.fluid.yaml").document["exposes"]
    with pytest.raises(RefResolutionError, match="resolved only after the overlay is merged"):
        compose_contract(tmp_path / "contract.fluid.yaml", "prod")


def test_a_section_that_is_not_a_list_reaches_the_schema_with_an_overlay(tmp_path: Path) -> None:
    """``builds: 5`` with --env is a schema error to report, not a crash of the merge."""
    from fluid_build.loader import compose_contract

    (tmp_path / "contract.fluid.yaml").write_text(
        'fluidVersion: "0.7.6"\nkind: DataProduct\nid: d.p\nbuilds: 5\n', encoding="utf-8"
    )
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text("name: P\n", encoding="utf-8")

    assert compose_contract(tmp_path / "contract.fluid.yaml", "prod").document["builds"] == 5


_SHARED_EXPOSE = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: d.p
name: P
extensions:
  shared:
    binding: {platform: local, format: csv, location: {path: a.csv}}
    schema: [{name: id, type: string}, {name: amount, type: number}]
    tags: [a, b]
exposes:
  - exposeId: out
    kind: table
    binding: {$ref: "#/extensions/shared/binding"}
    contract: {schema: {$ref: "#/extensions/shared/schema"}}
    tags: {$ref: "#/extensions/shared/tags"}
"""


@pytest.mark.parametrize(
    ("patch", "refused"),
    [
        ("binding: {location: {path: prod.csv}}", True),
        ("contract: {schema: [{name: id, type: string}]}", True),
        ("tags: [prod]", False),
        ("tags: []", False),
        ("binding: none-here", False),
    ],
    ids=["object-over-pointer", "object-list-over-pointer", "plain-list", "empty-list", "scalar"],
)
def test_an_overlay_value_over_a_base_pointer(tmp_path: Path, patch: str, refused: bool) -> None:
    """What a base pointer names exists only after the merge: an object or a list of objects
    merged over it is refused; a scalar or a list of plain values replaces it, as inline."""
    from fluid_build.loader import compose_contract
    from fluid_build.util.ref_confinement import RefResolutionError

    (tmp_path / "contract.fluid.yaml").write_text(_SHARED_EXPOSE, encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        "exposes:\n  - exposeId: out\n    " + patch + "\n", encoding="utf-8"
    )

    if refused:
        with pytest.raises(RefResolutionError, match="resolved only after the overlay is merged"):
            compose_contract(tmp_path / "contract.fluid.yaml", "prod")
    else:
        expose = compose_contract(tmp_path / "contract.fluid.yaml", "prod").document["exposes"][0]
        key, _, value = patch.partition(": ")
        assert expose[key] == yaml.safe_load(value)


def test_an_overlay_pointer_over_a_base_list_of_objects_is_refused(tmp_path: Path) -> None:
    """Inline, the list a pointer names merges into the base's by id; resolved after the
    merge, it could only replace it, dropping entries. Refused, naming the overlay."""
    from fluid_build.loader import compose_contract
    from fluid_build.util.ref_confinement import RefResolutionError

    (tmp_path / "contract.fluid.yaml").write_text(
        'fluidVersion: "0.7.6"\nkind: DataProduct\nid: d.p\n'
        "builds:\n  - {id: a, engine: sql}\n  - {id: b, engine: sql}\n",
        encoding="utf-8",
    )
    (tmp_path / "overlays").mkdir()
    overlay = tmp_path / "overlays" / "prod.yaml"
    overlay.write_text(
        'extensions: {prodBuilds: [{id: b, engine: dbt}]}\nbuilds: {$ref: "#/extensions/prodBuilds"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RefResolutionError) as err:
        compose_contract(tmp_path / "contract.fluid.yaml", "prod")
    assert "the list of objects" in str(err.value) and "prod.yaml" in str(err.value)


def test_an_overlay_pointer_over_a_base_pointer_to_a_list_of_objects_is_refused(
    tmp_path: Path,
) -> None:
    """Both written as #/ pointers, the overlay's list would replace the base's whole where,
    inline, it merges by id: refused, as the inline twin is."""
    from fluid_build.loader import compose_contract
    from fluid_build.util.ref_confinement import RefResolutionError

    (tmp_path / "contract.fluid.yaml").write_text(
        'fluidVersion: "0.7.6"\nkind: DataProduct\nid: d.p\n'
        "extensions: {allBuilds: [{id: a, engine: sql}, {id: b, engine: sql}]}\n"
        'builds: {$ref: "#/extensions/allBuilds"}\n',
        encoding="utf-8",
    )
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        'extensions: {prodBuilds: [{id: b, engine: dbt}]}\nbuilds: {$ref: "#/extensions/prodBuilds"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RefResolutionError, match="list of objects"):
        compose_contract(tmp_path / "contract.fluid.yaml", "prod")
    # Keys beside a file $ref are a merge patch, where a list replaces: unchanged.
    assert len(compose_contract(tmp_path / "contract.fluid.yaml").document["builds"]) == 2
