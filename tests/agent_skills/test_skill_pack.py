# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The agent skills pack follows the Agent Skills spec and tells the truth about ``fluid``.

A skill can go wrong in five ways that nobody notices until an agent trips on them:

1. frontmatter the spec or a client refuses, so the skill never loads;
2. a body that grew past what an agent reads well;
3. a link to a file the skill does not ship;
4. a ``fluid`` command or flag that does not exist (``fluid generate-artifacts`` was
   one, in the steering text this pack replaced);
5. a YAML example that ``fluid validate`` refuses.

The frontmatter rules are ported from the Agent Skills reference validator,
agentskills/agentskills ``skills-ref`` 0.1.0 (Apache-2.0), ``validator.py``; it is not a
dependency because it needs Python 3.11 and click/strictyaml.
"""

from __future__ import annotations

import importlib.util
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

from fluid_build.agent_skills import frontmatter, skill_dirs, skill_files
from fluid_build.schema_manager import FluidSchemaManager

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMANDS_TEST = REPO_ROOT / "tests" / "docs" / "test_template_readme_commands_are_real.py"
_spec = importlib.util.spec_from_file_location("_readme_commands", _COMMANDS_TEST)
assert _spec and _spec.loader
_readme_commands = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_readme_commands)

#: The agentskills.io frontmatter fields. Claude Code honours more, but a skill that
#: uses only these loads in every client that implements the spec.
ALLOWED_FIELDS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
#: Anthropic's skill-authoring guidance: keep SKILL.md under 500 lines.
MAX_BODY_LINES = 500
#: SkillsBench (arXiv 2602.12670): focused skills with at most three supporting
#: modules beat exhaustive bundles.
MAX_SUPPORTING_FILES = 3

#: A YAML example names where it goes in a contract on its first line; that path is
#: the schema ``$defs`` entry ``fluid validate --fragment`` checks it against.
FRAGMENT_KINDS = {
    "exposes[].binding": "binding",
    "exposes[].contract.schema[]": "column",
    "exposes[].contract.dq": "dqSpec",
    "builds[]": "build",
    "consumes[]": "consumeRef",
}
#: The one email a skill may print: the starter placeholder it tells agents to remove.
ALLOWED_EMAILS = {"data-team@company.com"}

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.S)
_LINK = re.compile(r"\]\(([^)#\s]+)\)")
_INLINE_COMMAND = re.compile(r"(?<!`)`(fluid [^`\n]+)`(?!`)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SKILLS = skill_dirs()


def _ids(paths: List[Path]) -> List[str]:
    return [p.name for p in paths]


def _body(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    return text[text.find("\n---\n", 4) + 5 :]


def _docs(skill: Path) -> Iterator[Path]:
    for rel in skill_files(skill):
        if rel.suffix == ".md":
            yield skill / rel


def test_the_pack_ships_four_skills() -> None:
    """Guard the guard: a green run must not mean 'found no skills'."""
    assert _ids(SKILLS) == [
        "authoring-fluid-contracts",
        "composing-fluid-data-products",
        "deploying-fluid-contracts-safely",
        "troubleshooting-fluid-errors",
    ]


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_frontmatter_follows_the_agent_skills_spec(skill: Path) -> None:
    meta = frontmatter(skill / "SKILL.md")
    assert set(meta) <= ALLOWED_FIELDS, f"fields outside the spec: {set(meta) - ALLOWED_FIELDS}"

    name = unicodedata.normalize("NFKC", str(meta.get("name", "")).strip())
    assert name, "name is required"
    assert len(name) <= MAX_NAME
    assert name == name.lower()
    assert not name.startswith("-") and not name.endswith("-")
    assert "--" not in name
    assert all(c.isalnum() or c == "-" for c in name)
    assert name == skill.name, "the name must match the skill's directory"

    description = str(meta.get("description", "")).strip()
    assert description, "description is required"
    assert len(description) <= MAX_DESCRIPTION
    assert "Use when" in description, "say when to use the skill, so an agent picks it"

    if "compatibility" in meta:
        assert len(str(meta["compatibility"])) <= MAX_COMPATIBILITY


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_each_skill_stays_focused(skill: Path) -> None:
    lines = _body(skill / "SKILL.md").count("\n")
    assert lines <= MAX_BODY_LINES, f"SKILL.md body is {lines} lines"
    supporting = [p for p in skill_files(skill) if p.name != "SKILL.md"]
    assert len(supporting) <= MAX_SUPPORTING_FILES, supporting
    for rel in supporting:
        assert rel.parts[0] in {"references", "scripts", "assets"}, rel


def test_hidden_files_are_not_part_of_a_skill(tmp_path: Path) -> None:
    """An editable install reads the source tree, where Finder drops a binary .DS_Store."""
    skill = tmp_path / "some-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "__pycache__").mkdir()
    (skill / ".git").mkdir()
    for rel in ("SKILL.md", "references/sheet.md"):
        (skill / rel).write_text("text\n", encoding="utf-8")
    (skill / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1\xff")
    (skill / "references" / ".sheet.md.swp").write_bytes(b"\xff\xfe")
    (skill / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
    (skill / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    assert skill_files(skill) == [Path("SKILL.md"), Path("references/sheet.md")]


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_links_resolve_inside_the_skill(skill: Path) -> None:
    """A skill can be installed on its own, so it may only link to its own files."""
    for doc in _docs(skill):
        for target in _LINK.findall(doc.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            resolved = (doc.parent / target).resolve()
            assert resolved.is_file(), f"{doc.name} links to missing {target}"
            assert skill.resolve() in resolved.parents, f"{doc.name} links outside: {target}"


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_fluid_commands_in_skills_exist(skill: Path) -> None:
    """Fenced commands must run as written; inline ones must name real commands and flags."""
    defects = []
    for doc in _docs(skill):
        text = doc.read_text(encoding="utf-8")
        for command in _readme_commands._commands(text):
            found = _readme_commands._defect(command)
            if found:
                defects.append(f"{doc.name}: `{command}` -> {found[0]}: {found[1]}")
        for command in _INLINE_COMMAND.findall(text):
            found = _readme_commands._defect(command)
            if found and found[0] != "missing required argument":
                defects.append(f"{doc.name}: `{command}` -> {found[0]}: {found[1]}")
    assert not defects, "\n".join(defects)


def test_retired_commands_and_flags_are_not_taught() -> None:
    retired = ("fluid generate-artifacts", "--no-verify-digest", "13 tools")
    offenders = [
        f"{doc.relative_to(REPO_ROOT)}: {term}"
        for skill in SKILLS
        for doc in _docs(skill)
        for term in retired
        if term in doc.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


@pytest.mark.parametrize("skill", SKILLS, ids=_ids(SKILLS))
def test_no_skill_teaches_an_invented_email(skill: Path) -> None:
    """The pilot agent copied the starter's placeholder owner email into its contract."""
    for rel in skill_files(skill):
        found = set(_EMAIL.findall((skill / rel).read_text(encoding="utf-8")))
        assert found <= ALLOWED_EMAILS, f"{rel}: {found - ALLOWED_EMAILS}"


# ---------------------------------------------------------------------------
# Every YAML example validates
# ---------------------------------------------------------------------------


def _yaml_examples() -> List[Tuple[str, str, str]]:
    """``(id, kind, text)`` for every fenced YAML block in every skill."""
    found = []
    for skill in SKILLS:
        for doc in _docs(skill):
            for n, (lang, block) in enumerate(_FENCE.findall(doc.read_text(encoding="utf-8"))):
                if lang not in {"yaml", "yml"}:
                    continue
                first = block.splitlines()[0].strip() if block.strip() else ""
                kind = FRAGMENT_KINDS.get(first.lstrip("# ").strip(), "")
                found.append((f"{skill.name}/{doc.name}#{n}", kind, block))
    return found


EXAMPLES = _yaml_examples()


def test_there_are_yaml_examples_to_check() -> None:
    assert len(EXAMPLES) >= 8, EXAMPLES


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """``fluid <args>`` in-process from an empty directory: ``(exit code, output)``."""
    from fluid_build.cli import main
    from fluid_build.credentials.dotenv_store import DOTENV_SWITCH

    monkeypatch.delenv(DOTENV_SWITCH, raising=False)
    monkeypatch.chdir(tmp_path)

    def run(*args: Any) -> Tuple[int, str]:
        capsys.readouterr()
        try:
            code = main([str(a) for a in args])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        out = capsys.readouterr()
        return code, out.out + out.err

    return run


@pytest.mark.parametrize("example", EXAMPLES, ids=[e[0] for e in EXAMPLES])
def test_yaml_example_validates(example: Tuple[str, str, str], tmp_path: Path, cli) -> None:
    name, kind, block = example
    assert kind, f"{name}: open the block with one of {sorted(FRAGMENT_KINDS)}"
    doc = yaml.safe_load(block)
    items: List[Dict[str, Any]] = doc if isinstance(doc, list) else [doc]
    version = FluidSchemaManager.latest_bundled_version()
    for i, item in enumerate(items):
        path = tmp_path / f"fragment-{i}.yaml"
        path.write_text(yaml.safe_dump(item, sort_keys=False), encoding="utf-8")
        code, out = cli("validate", "--fragment", kind, path, "--schema-version", version)
        assert code == 0, f"{name} item {i} is not a valid {kind}:\n{out}"


@pytest.mark.parametrize(
    "contract",
    sorted(p for s in SKILLS for p in s.rglob("*.fluid.yaml")),
    ids=lambda p: p.name,
)
def test_example_contracts_validate(contract: Path, cli) -> None:
    code, out = cli("validate", contract, "--offline")
    assert code == 0, out
    doc = yaml.safe_load(contract.read_text(encoding="utf-8"))
    assert doc["fluidVersion"] == FluidSchemaManager.latest_bundled_version()
    assert "email" not in doc["metadata"]["owner"], "an example must not invent an email"
