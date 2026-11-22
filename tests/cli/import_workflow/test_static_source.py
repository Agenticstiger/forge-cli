# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Static reading of Python source: the files, the parse and the folding of values.

The importers for Airflow DAGs and PySpark jobs read a project's code and must never run it.
These tests pin that, and what a value read from the syntax is (or is not).
"""

from __future__ import annotations

import ast
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli.import_workflow import _pysource as P
from fluid_build.cli.import_workflow._fold import (
    ABSENT,
    FoldedDict,
    Folder,
    ModuleIndex,
    Unresolved,
    lookup,
)


def _write(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# ── finding files ─────────────────────────────────────────────────────


def test_find_python_files_is_sorted_and_leaves_out_what_is_not_the_project(tmp_path):
    _write(
        tmp_path,
        {
            "b.py": "",
            "a/z.py": "",
            "a/y.py": "",
            "a/notes.txt": "",
            "tests/test_a.py": "",
            "test/test_b.py": "",
            ".hidden/h.py": "",
            "venv/lib/v.py": "",
            "node_modules/m.py": "",
            "build/out.py": "",
            "pkg/__pycache__/c.py": "",
        },
    )
    walk = P.find_python_files(tmp_path)
    assert [f.rel for f in walk.files] == ["a/y.py", "a/z.py", "b.py"]
    assert walk.notes == []


def test_find_python_files_honours_ignore_patterns(tmp_path):
    _write(tmp_path, {"jobs/a.py": "", "jobs/legacy/old.py": "", "scratch_x.py": ""})
    walk = P.find_python_files(tmp_path, ignore=["jobs/legacy", "scratch_*"])
    assert [f.rel for f in walk.files] == ["jobs/a.py"]


def test_find_python_files_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n", encoding="utf-8")
    root = tmp_path / "root"
    _write(root, {"a.py": ""})
    os.symlink(outside, root / "linked_dir")
    os.symlink(outside / "secret.py", root / "linked_file.py")
    walk = P.find_python_files(root)
    assert [f.rel for f in walk.files] == ["a.py"]
    assert walk.notes == ["2 symbolic link(s) not followed"]


def test_find_python_files_is_bounded(tmp_path, monkeypatch):
    _write(tmp_path, {f"f{i}.py": "" for i in range(6)})
    monkeypatch.setattr(P, "MAX_FILES", 4)
    walk = P.find_python_files(tmp_path)
    assert len(walk.files) == 4 and "stopped at 4 Python files" in walk.notes
    monkeypatch.setattr(P, "MAX_FILES", 5000)
    monkeypatch.setattr(P, "MAX_ENTRIES", 3)
    walk = P.find_python_files(tmp_path)
    assert any("directory entries" in note for note in walk.notes)


def test_find_python_files_is_bounded_in_depth(tmp_path, monkeypatch):
    _write(tmp_path, {"a/b/c/d.py": "", "a/top.py": ""})
    monkeypatch.setattr(P, "MAX_DEPTH", 2)
    walk = P.find_python_files(tmp_path)
    assert [f.rel for f in walk.files] == ["a/top.py"]
    assert any("deeper than 2 levels" in note for note in walk.notes)


def test_find_python_files_refuses_a_file(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        P.find_python_files(tmp_path / "x.py")


# ── reading and parsing ───────────────────────────────────────────────


def test_read_source_refuses_a_large_file_and_a_fifo_without_waiting(tmp_path, monkeypatch):
    big = tmp_path / "big.py"
    big.write_bytes(b"#" * 40)
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 10)
    with pytest.raises(P.SourceUnreadable, match="larger than 10 bytes"):
        P.read_source(big)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "pipe.py"
        os.mkfifo(fifo)
        started = time.monotonic()
        with pytest.raises(P.SourceUnreadable, match="cannot read it"):
            P.read_source(fifo)
        assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "data",
    [b"def (:\n", b"x = 1\x00\n", b"(" * 5000 + b")" * 5000, b"\xff\xfe\x00bad"],
    ids=["syntax", "null-byte", "deep-nesting", "bad-bytes"],
)
def test_parse_module_refuses_what_is_not_python_without_echoing_it(data):
    with pytest.raises(P.SourceUnreadable) as refused:
        P.parse_module("x.py", data)
    assert "def (" not in str(refused.value) and "\x00" not in str(refused.value)


def test_parse_module_is_quiet_about_escape_sequences():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        module = P.parse_module("x.py", b"x = '\\d'\n")
    assert isinstance(module.tree, ast.Module)


def test_a_file_is_parsed_and_never_run(tmp_path):
    marker = tmp_path / "ran"
    source = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "raise SystemExit(3)\n"
        "import os; os.system('touch /tmp/should-never-exist-fluid-import')\n"
    )
    module = P.parse_module("hostile.py", source.encode("utf-8"))
    Folder(module).fold(module.tree.body[0].value)  # type: ignore[attr-defined]
    assert not marker.exists()
    assert not Path("/tmp/should-never-exist-fluid-import").exists()


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"from airflow import DAG\n", True),
        (b"# AIRFLOW dag\n", True),
        (b"import pyspark\n", False),
        (b"airflow only\n", False),
    ],
)
def test_might_contain_dag_is_the_two_word_prefilter(data, expected):
    assert P.might_contain_dag(data) is expected


def test_import_table_and_qualified_names():
    module = P.parse_module(
        "m.py",
        (
            b"import os\n"
            b"import pyspark.sql.functions as F\n"
            b"import a.b\n"
            b"from airflow import DAG\n"
            b"from airflow.providers.x import Op as Renamed\n"
            b"from . import sibling\n"
            b"from .pkg import thing\n"
            b"from ..up import deeper\n"
            b"from star import *\n"
            b"def f():\n    import late\n"
        ),
    )
    table = module.imports
    assert table["os"] == "os" and table["F"] == "pyspark.sql.functions"
    assert table["a"] == "a" and table["DAG"] == "airflow.DAG"
    assert table["Renamed"] == "airflow.providers.x.Op"
    assert table["sibling"] == ".sibling" and table["thing"] == ".pkg.thing"
    assert table["deeper"] == "..up.deeper" and table["late"] == "late"
    assert "*" not in table

    def q(code: str) -> Optional[str]:
        return P.qualified_name(ast.parse(code, mode="eval").body, table)

    assert q("F.col") == "pyspark.sql.functions.col"
    assert q("Renamed") == "airflow.providers.x.Op"
    assert q("a.b.c") == "a.b.c"
    assert q("spark.read") == "spark.read"
    assert q("f(x).y") is None and q("1") is None


def test_snippet_is_one_short_line_and_survives_deep_trees():
    node = ast.parse("(a +\n  b)", mode="eval").body
    assert P.snippet(node) == "a + b"
    assert len(P.snippet(ast.parse("x" * 500, mode="eval").body)) == 80
    deep = ast.parse("1", mode="eval").body
    for _ in range(3000):
        deep = ast.UnaryOp(op=ast.USub(), operand=deep)
    assert P.snippet(deep) == "<expression>"


# ── folding ───────────────────────────────────────────────────────────


def _fold(source: str, name: str = "x", **variables: str) -> Any:
    module = P.parse_module("m.py", source.encode("utf-8"))
    folder = Folder(module, variables=variables)
    for node in module.tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return folder.fold(node.value)
    raise AssertionError(f"no assignment to {name}")


def _why(value: Any) -> str:
    assert isinstance(value, Unresolved), value
    return value.why


@pytest.mark.parametrize(
    "source, expected",
    [
        ("x = 'a'", "a"),
        ("x = 3", 3),
        ("x = None", None),
        ("x = ('a' 'b')", "ab"),
        ("x = 'a' + 'b'", "ab"),
        ("T = 'orders'\nx = f'retail.{T}'", "retail.orders"),
        ("T = 'orders'\nx = f'{T!s}'", "orders"),
        ("x = '%s.%s' % ('db', 'orders')", "db.orders"),
        ("x = '%(a)s-%(b)d' % {'a': 'z', 'b': 4}", "z-4"),
        ("x = '100%%' % ()", "100%"),
        ("x = '{}.{}'.format('db', 'orders')", "db.orders"),
        ("x = '{1}{0}'.format('a', 'b')", "ba"),
        ("x = '{t}'.format(t='orders')", "orders"),
        ("x = '{{}}'.format()", "{}"),
        ("x = '/'.join(['a', 'b'])", "a/b"),
        ("import os\nx = os.path.join('s3://b', 'k', 'f')", "s3://b/k/f"),
        ("x = ' Ab '.strip().lower()", "ab"),
        ("x = 'a-b'.replace('-', '_')", "a_b"),
        ("x = 's3://b/p/'.rstrip('/')", "s3://b/p"),
        ("x = 'pre_t'.removeprefix('pre_')", "t"),
        ("x = str(4) + 'x'", "4x"),
    ],
)
def test_what_folds(source, expected):
    assert _fold(source) == expected


@pytest.mark.parametrize(
    "source, why",
    [
        ("import sys\nx = sys.argv[1]", "command-line"),
        ("x = unknown_name", "not defined"),
        ("x = f'{env}_orders'", "not defined"),
        ("x = 'a' + 1", "not text"),
        ("x = '{:>9}'.format('a')", "format field"),
        ("x = '{0.real}'.format(1)", "format field"),
        ("x = '{!r}'.format('a')", "format field"),
        ("x = '{'.format()", "format"),
        ("x = '%s %s' % ('a',)", "do not match"),
        ("x = '%s' % ('a', 'b')", "do not match"),
        ("x = '%x' % 3", "percent format"),
        ("x = '%(a)s' % ('a',)", "keyed"),
        ("x = f'{1:>5}'", "format spec"),
        ("x = f'{\"a\"!r}'", "format spec or conversion"),
        ("x = open('p').read()", "call to"),
        ("x = [1][0] + 2", "not text"),
        ("x = 1 if c else 2", "conditional"),
        ("x = (lambda: 1)()", "call to"),
        ("x = b'bytes'", "not text or a number"),
    ],
)
def test_what_does_not_fold(source, why):
    assert why in _why(_fold(source))


def test_hostile_formats_do_not_spend_memory():
    started = time.monotonic()
    assert "format field" in _why(_fold("x = '{:>999999999}'.format('a')"))
    assert "percent format" in _why(_fold("x = '%999999999s' % 'a'"))
    assert "percent format" in _why(_fold("x = '%*d' % (9, 1)"))
    assert time.monotonic() - started < 2


def test_a_string_over_the_cap_is_refused():
    big = "a" * 3000
    value = _fold(f"x = '{big}' + '{big}'")
    assert "longer than" in _why(value)
    assert "longer than" in _why(_fold("x = '" + "b" * 5000 + "'"))


def test_f_string_with_an_unresolved_field_keeps_its_template():
    value = _fold("x = f'{env}.orders_{kind}'")
    assert isinstance(value, Unresolved)
    assert value.text == "{env}.orders_{kind}"


def test_names_are_read_when_assigned_once_in_their_scope():
    source = (
        "A = 'x'\n"
        "B = A + '_y'\n"
        "C = 'one'\n"
        "C = 'two'\n"
        "D: str = 'typed'\n"
        "for E in ['p']:\n    pass\n"
        "F = 'f'\nif 1:\n    F += 'g'\n"
        "G, H = 'g', 'h'\n"
        "def fn():\n    pass\n"
        "class K:\n    pass\n"
        "import os as O\n"
    )
    assert _fold(source + "x = B") == "x_y"
    assert "more than once" in _why(_fold(source + "x = C"))
    assert _fold(source + "x = D") == "typed"
    assert "loop variable" in _why(_fold(source + "x = E"))
    assert "changed in place" in _why(_fold(source + "x = F"))
    assert "unpacked" in _why(_fold(source + "x = G"))
    assert "is a function" in _why(_fold(source + "x = fn"))
    assert "is a class" in _why(_fold(source + "x = K"))


def test_a_function_parameter_and_a_function_local():
    source = (
        "T = 'module'\n"
        "def f(table):\n"
        "    local = 'db.' + 'orders'\n"
        "    y = table\n"
        "    z = local\n"
        "    w = T\n"
        "    return y, z, w\n"
    )
    module = P.parse_module("m.py", source.encode("utf-8"))
    folder = Folder(module)
    fn = module.tree.body[1]
    results = {
        stmt.targets[0].id: folder.fold(stmt.value)  # type: ignore[attr-defined]
        for stmt in fn.body  # type: ignore[attr-defined]
        if isinstance(stmt, ast.Assign)
    }
    assert "parameter" in _why(results["y"])
    assert results["z"] == "db.orders" and results["w"] == "module"


def test_a_name_that_refers_to_itself_does_not_loop():
    assert "refers to itself" in _why(_fold("a = b\nb = a\nx = a"))


def test_class_attributes_of_a_class_in_the_same_module():
    source = "class Cfg:\n    ORDERS = 'retail.orders'\n    MORE = ORDERS\nx = Cfg.ORDERS\ny = Cfg.MORE\n"
    assert _fold(source) == "retail.orders"
    assert _fold(source, "y") == "retail.orders"
    assert "not a constant of the class" in _why(_fold(source + "z = Cfg.NOPE\n", "z"))
    # a module-level name of the same spelling is not an attribute of the class
    assert "not a constant of the class" in _why(
        _fold("NOPE = 'module'\nclass Cfg:\n    A = 'a'\nz = Cfg.NOPE\n", "z")
    )
    assert "assigned more than once" in _why(
        _fold("class Cfg:\n    A = 'a'\n    A = 'b'\nz = Cfg.A\n", "z")
    )


def test_dicts_lists_and_incomplete_containers():
    d = _fold("x = {'a': 'b', 'n': 1}")
    assert isinstance(d, FoldedDict) and d["a"] == "b" and d.complete
    assert lookup(d, "a") == "b" and lookup(d, "q") is ABSENT

    spread = _fold("x = {'a': 'b', **unknown}")
    assert not spread.complete
    assert isinstance(lookup(spread, "q"), Unresolved)  # might be in the spread
    assert lookup(spread, "a") == "b"

    merged = _fold("base = {'k': 'v'}\nx = {**base, 'o': 'p'}")
    assert merged == {"k": "v", "o": "p"} and merged.complete

    items = _fold("x = ['a', *unknown, 'b']")
    assert list(items) == ["a", "b"] and not items.complete
    assert _fold("x = ['a'] + ['b']") == ["a", "b"]
    assert _fold("L = ['p', 'q']\nx = L[1]") == "q"
    assert _fold("D = {'k': 'v'}\nx = D['k']") == "v"
    assert "not in the dict" in _why(_fold("D = {'k': 'v'}\nx = D['z']"))
    assert isinstance(lookup(Unresolved("w"), "k"), Unresolved)
    assert isinstance(lookup("text", "k"), Unresolved)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("x = dict(script='a.py', day=1)", {"script": "a.py", "day": 1}),
        ("B = {'k': 'v'}\nx = dict(B, j='w')", {"k": "v", "j": "w"}),
        ("B = {'k': 'v'}\nx = dict(**B, j='w')", {"k": "v", "j": "w"}),
        ("x = dict()", {}),
        ("D = {'k': 'v'}\nx = D.get('k')", "v"),
        ("D = {'k': 'v'}\nx = D.get('nope', 'dflt')", "dflt"),
        ("D = {'k': 'v'}\nx = D.get('nope')", None),
        ("D = dict(k='v')\nx = D.get('k') + '!'", "v!"),
        ("D = {1: 'one'}\nx = D.get(1)", "one"),
    ],
)
def test_dict_calls_and_get_fold_as_the_literal_they_build(source, expected):
    value = _fold(source)
    assert value == expected and getattr(value, "complete", True)


@pytest.mark.parametrize(
    "source, why",
    [
        ("def dict(**kw):\n    return kw\nx = dict(k='v')", "the result of a call"),
        ("dict = lambda **kw: kw\nx = dict(k='v')", "the result of a call"),
        ("from acme import dict\nx = dict(k='v')", "the result of a call"),
        ("x = dict([('k', 'v')])", "not a dict"),
        ("x = dict({'a': 1}, {'b': 2})", "more than one mapping"),
        ("import os\nD = {**os.environ}\nx = D.get('k', 'dflt')", "not a literal"),
        ("D = {'k': 'v'}\nx = D.get(key)", "not defined"),
        ("D = {'k': 'v'}\nx = D.get('k', default='d')", "dict.get with other arguments"),
        ("D = {'k': 'v'}\nD['k'] = 'changed'\nx = D.get('k')", "the result of a call"),
    ],
)
def test_a_dict_call_or_get_that_is_not_known_is_unresolved(source, why):
    assert why in _why(_fold(source))


def test_an_incomplete_dict_call_keeps_what_it_knows_and_says_it_is_not_whole():
    value = _fold("import os\nx = dict(**os.environ, k='v')")
    assert isinstance(value, FoldedDict) and value["k"] == "v" and not value.complete
    assert isinstance(lookup(value, "other"), Unresolved)


def test_a_scope_knows_the_names_bound_by_one_def_and_the_parameters_bound_again():
    module = P.parse_module(
        "m.py",
        (
            "def once():\n    pass\n"
            "def twice():\n    pass\n"
            "def twice():\n    pass\n"
            "def then_assigned():\n    pass\n"
            "then_assigned = 1\n"
            "with ctx:\n    def under_with():\n        pass\n"
            "if flag:\n    def under_if():\n        pass\n"
            "class Cls:\n    pass\n"
            "def run(kept, again, added, looped, imported, *rest, **kwargs):\n"
            "    again = again.strip()\n"
            "    added += 1\n"
            "    for looped in []:\n        pass\n"
            "    import imported\n"
            "    def inner():\n        pass\n"
            "    return kept\n"
        ).encode("utf-8"),
    )
    folder = Folder(module)
    top = folder.scope_of(module.tree)
    assert set(top.functions) == {"once", "under_with", "under_if", "run"}
    run = top.functions["run"]
    scope = folder.function_scope(run)
    assert scope.parameters == {"kept", "again", "added", "looped", "imported", "rest", "kwargs"}
    assert scope.rebound_parameters == {"again", "added", "looped", "imported"}
    assert set(scope.functions) == {"inner"}


def test_a_function_folded_with_what_its_caller_passes():
    module = P.parse_module(
        "m.py",
        (
            "ROOT = 's3://code'\n"
            "def submit(script, day, **kwargs):\n"
            "    path = f'{ROOT}/{script}'\n"
            "    args = [path, kwargs['env'], day]\n"
            "    def inner():\n        return script\n"
            "    return args\n"
        ).encode("utf-8"),
    )
    folder = Folder(module)
    submit = module.tree.body[1]
    assign = submit.body[1]  # args = [...]
    inner_return = submit.body[2].body[0].value  # the `script` that inner() returns
    assert "is a parameter" in _why(folder.fold(assign.value)[0])
    env = FoldedDict({"env": "prod"})
    with folder.bound(submit, {"script": "jobs/a.py", "kwargs": env}):
        value = folder.fold(assign.value)
        assert value[:2] == ["s3://code/jobs/a.py", "prod"]
        assert "is a parameter" in _why(value[2])  # `day` is not passed
        assert folder.fold(inner_return) == "jobs/a.py"  # a function inside sees it too
    # nothing folded meanwhile is remembered for later
    assert "is a parameter" in _why(folder.fold(assign.value)[0])
    assert "is a parameter" in _why(folder.fold(inner_return))


@pytest.mark.parametrize(
    "why, expected",
    [
        ("`cfg.paths.ROOT` is not in the scanned files", ("module", "cfg.paths.ROOT")),
        ("`ROOT` is imported from outside the scanned files", ("module", "ROOT")),
        ("the environment variable ENV", ("environment", "ENV")),
        ("the Airflow Variable region", ("airflow_variable", "region")),
        ("the Airflow Variable region is read as JSON", None),
        ("`day` is a parameter of the function", None),
        ("the result of a call to f()", None),
        ("an environment variable", None),
    ],
)
def test_the_reasons_that_name_a_value_the_conventions_can_give(why, expected):
    from fluid_build.cli.import_workflow._fold import missing_value

    assert missing_value(why) == expected


def test_the_reasons_the_folder_gives_are_the_ones_it_classifies(tmp_path):
    from fluid_build.cli.import_workflow._fold import missing_value

    _write(tmp_path, {"job.py": "from acme_cfg import ROOT\nx = f'{ROOT}/a.py'\n"})
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    module = index.load(walk.files[0])
    assert module is not None
    node = next(n for n in module.tree.body if isinstance(n, ast.Assign))
    assert missing_value(_why(index.folder(module).fold(node.value))) == (
        "module",
        "acme_cfg.ROOT",
    )
    environment = _fold("import os\nx = os.environ['STAGE']")
    assert missing_value(_why(environment)) == ("environment", "STAGE")


def test_a_tuple_and_a_list_differ_as_percent_operands():
    assert _fold("x = '%s' % ('a',)") == "a"
    assert "not text" in _why(_fold("x = '%s' % ['a']"))


def test_the_environment_comes_from_the_operators_variables_only():
    src_get = "import os\nx = os.environ.get('ENV', 'dev')\n"
    src_idx = "import os\nx = os.environ['ENV']\n"
    src_geten = "from os import getenv\nx = getenv('ENV')\n"
    for source in (src_get, src_idx, src_geten):
        assert "environment variable ENV" in _why(_fold(source))  # the default is not assumed
        assert _fold(source, ENV="prod") == "prod"
    assert "environment" in _why(_fold("import os\nx = os.environ[k]\n"))
    assert _fold("import os\nx = f\"{os.environ['ENV']}_orders\"", ENV="prod") == "prod_orders"


def test_a_constant_of_another_module_of_the_tree(tmp_path):
    _write(
        tmp_path,
        {
            "jobs/etl/job.py": (
                "from config import ORDERS\n"
                "import config as cfg\n"
                "from .local import LOCAL\n"
                "from . import local as loc\n"
                "from ..shared import SHARED\n"
                "from config import Cls\n"
                "a = ORDERS\nb = cfg.ORDERS\nc = LOCAL\nd = loc.LOCAL\ne = SHARED\nf = Cls.NAME\n"
            ),
            "jobs/etl/local.py": "LOCAL = 'near'\n",
            "jobs/shared.py": "SHARED = 'up'\n",
            "jobs/config.py": "ORDERS = 'retail.orders'\nclass Cls:\n    NAME = 'in-class'\n",
        },
    )
    walk = P.find_python_files(tmp_path / "jobs")
    index = ModuleIndex(walk.files)
    module = index.load(next(f for f in walk.files if f.rel == "etl/job.py"))
    assert module is not None
    folder = index.folder(module)
    values = {
        stmt.targets[0].id: folder.fold(stmt.value)  # type: ignore[attr-defined]
        for stmt in module.tree.body
        if isinstance(stmt, ast.Assign)
    }
    assert values == {
        "a": "retail.orders",
        "b": "retail.orders",
        "c": "near",
        "d": "near",
        "e": "up",
        "f": "in-class",
    }


def test_a_module_name_two_files_answer_to_is_ambiguous_unless_one_is_next_door(tmp_path):
    _write(
        tmp_path,
        {
            "a/job.py": "from config import T\nx = T\n",
            "b/job.py": "from config import T\nx = T\n",
            "a/config.py": "T = 'from-a'\n",
            "b/config.py": "T = 'from-b'\n",
            "c/lonely.py": "from config import T\nx = T\n",
        },
    )
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    got = {}
    for source in walk.files:
        if source.rel.endswith("job.py") or source.rel.endswith("lonely.py"):
            module = index.load(source)
            assert module is not None
            node = next(n for n in module.tree.body if isinstance(n, ast.Assign))
            got[source.rel] = index.folder(module).fold(node.value)
    assert got["a/job.py"] == "from-a" and got["b/job.py"] == "from-b"
    assert "more than one scanned file answers" in _why(got["c/lonely.py"])


def test_a_config_module_outside_the_tree_is_found_by_name(tmp_path):
    _write(tmp_path, {"jobs/job.py": "from acme_cfg.settings import T\nx = T\n"})
    outside = tmp_path / "libs" / "settings.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("T = 'outside'\n", encoding="utf-8")
    walk = P.find_python_files(tmp_path / "jobs")
    index = ModuleIndex(walk.files, extra={"acme_cfg.settings": outside})
    module = index.load(walk.files[0])
    assert module is not None
    node = next(n for n in module.tree.body if isinstance(n, ast.Assign))
    assert index.folder(module).fold(node.value) == "outside"


def test_a_module_of_the_importing_files_own_package_is_found_by_its_full_name(tmp_path):
    """The scanned directory may sit below the import root (Airflow puts the DAGs folder on the
    path, and the files import ``teamx.dags.<dag>.settings``): a name that runs through the
    importing file's own directory and goes on to a file there is that file."""
    _write(
        tmp_path,
        {
            "dags/nightly/dag.py": (
                "from teamx.dags.nightly.settings import STEP\n"
                "from teamx.dags.nightly import pkg\n"
                "from teamx.dags.nightly.pkg.inner import DEEP\n"
                "import teamx.dags.nightly.settings as cfg\n"
                "from teamx.weekly.settings import WEEKLY\n"
                "from teamx.dags.nightly.missing import GONE\n"
                "a = STEP['script']\nb = pkg.X\nc = DEEP\nd = cfg.STEP['config']\n"
                "e = WEEKLY\nf = GONE\n"
            ),
            "dags/nightly/settings.py": (
                "STEP = {'script': 's3://code/a.py', 'config': 's3://code/a.yaml'}\n"
            ),
            "dags/nightly/pkg/__init__.py": "X = 'from the package'\n",
            "dags/nightly/pkg/inner.py": "DEEP = 'deeper'\n",
            "dags/weekly/settings.py": "WEEKLY = 'not next door'\n",
            "dags/top.py": "from teamx.dags.settings import T\nt = T\n",
            "dags/settings.py": "T = 'at the root'\n",
        },
    )
    walk = P.find_python_files(tmp_path / "dags")
    index = ModuleIndex(walk.files)
    module = index.load(next(f for f in walk.files if f.rel == "nightly/dag.py"))
    assert module is not None
    folder = index.folder(module)
    values = {
        stmt.targets[0].id: folder.fold(stmt.value)  # type: ignore[attr-defined]
        for stmt in module.tree.body
        if isinstance(stmt, ast.Assign)
    }
    assert values["a"] == "s3://code/a.py" and values["d"] == "s3://code/a.yaml"
    assert values["b"] == "from the package" and values["c"] == "deeper"
    # a name that does not run through the importing file's directory is not guessed at
    assert "is not in the scanned files" in _why(values["e"])
    assert "is not in the scanned files" in _why(values["f"])
    # a file at the root of the scan has no directory of its own to go by
    top = index.load(next(f for f in walk.files if f.rel == "top.py"))
    assert top is not None
    node = next(n for n in top.tree.body if isinstance(n, ast.Assign))
    assert "is not in the scanned files" in _why(index.folder(top).fold(node.value))


def test_a_full_name_that_starts_with_the_names_of_the_scanned_directory_is_a_file_of_it(tmp_path):
    """A scan of ``.../teamx/dags``: ``teamx.dags.common.paths`` is ``common/paths.py``, from any
    file of the scan; a name that starts otherwise is not guessed at."""
    _write(
        tmp_path,
        {
            "teamx/dags/nightly/dag.py": (
                "from teamx.dags.common.paths import ROOT\n"
                "from dags.common.paths import ROOT as SHORT\n"
                "from other.dags.common.paths import ROOT as OTHER\n"
                "from teamx.dags.common import pkg_value\n"
                "a = ROOT\nb = SHORT\nc = OTHER\nd = pkg_value\n"
            ),
            "teamx/dags/common/__init__.py": "pkg_value = 'from the package'\n",
            "teamx/dags/common/paths.py": "ROOT = 's3://code'\n",
        },
    )
    walk = P.find_python_files(tmp_path / "teamx" / "dags")
    index = ModuleIndex(walk.files)
    module = index.load(next(f for f in walk.files if f.rel == "nightly/dag.py"))
    assert module is not None
    folder = index.folder(module)
    values = {
        stmt.targets[0].id: folder.fold(stmt.value)  # type: ignore[attr-defined]
        for stmt in module.tree.body
        if isinstance(stmt, ast.Assign)
    }
    assert values["a"] == "s3://code" and values["b"] == "s3://code"
    assert values["d"] == "from the package"
    assert "is not in the scanned files" in _why(values["c"])


@pytest.mark.parametrize("where", ["project", "dags", "outside"])
def test_a_dag_files_imports_resolve_whatever_the_working_directory(tmp_path, monkeypatch, where):
    """A DAG file is shown by its path from the working directory, and its imports are resolved
    from the path the scan knows it by: a relative import (and the file next door winning a name
    two files answer to) does not depend on where the command runs."""
    from fluid_build.cli.import_workflow._conventions import Conventions
    from fluid_build.cli.import_workflow._scan import scan_dags

    dag = (
        "from airflow import DAG\n"
        "from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator\n"
        "from .settings import SCRIPT\n"
        "from config import ARGS\n"
        "with DAG('{name}', schedule=None) as dag:\n"
        "    SparkSubmitOperator(task_id='t', application=SCRIPT, application_args=ARGS)\n"
    )
    _write(
        tmp_path,
        {
            "proj/dags/a/dag.py": dag.format(name="a"),
            "proj/dags/a/settings.py": "SCRIPT = 's3://code/jobs/a.py'\n",
            "proj/dags/a/config.py": "ARGS = ['--from-a']\n",
            "proj/dags/b/dag.py": dag.format(name="b"),
            "proj/dags/b/settings.py": "SCRIPT = 's3://code/jobs/b.py'\n",
            "proj/dags/b/config.py": "ARGS = ['--from-b']\n",
        },
    )
    (tmp_path / "elsewhere").mkdir()
    cwd = {"project": "proj", "dags": "proj/dags", "outside": "elsewhere"}[where]
    monkeypatch.chdir(tmp_path / cwd)
    scan = scan_dags(tmp_path / "proj" / "dags", Conventions())
    got = {d.dag_id: [(s.script, s.args) for t in d.tasks for s in t.submits] for d in scan.dags}
    assert got == {
        "a": [("s3://code/jobs/a.py", ("--from-a",))],
        "b": [("s3://code/jobs/b.py", ("--from-b",))],
    }
    if where == "project":
        assert {d.file for d in scan.dags} == {"dags/a/dag.py", "dags/b/dag.py"}


def test_an_unreadable_module_is_unresolved_and_remembered(tmp_path):
    _write(tmp_path, {"job.py": "from broken import T\nx = T\n", "broken.py": "def (:\n"})
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    module = index.load(next(f for f in walk.files if f.rel == "job.py"))
    assert module is not None
    node = next(n for n in module.tree.body if isinstance(n, ast.Assign))
    assert "cannot be read" in _why(index.folder(module).fold(node.value))
    assert "broken.py" in index.unreadable


def test_imports_cycle_without_looping(tmp_path):
    _write(tmp_path, {"a.py": "from b import B\nA = B\n", "b.py": "from a import A\nB = A\n"})
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    module = index.load(walk.files[0])
    assert module is not None
    node = next(n for n in module.tree.body if isinstance(n, ast.Assign))
    assert isinstance(index.folder(module).fold(node.value), Unresolved)


def test_deep_expressions_are_cut_not_crashed_on():
    source = "x = " + "(" * 30 + "'a'" + " + 'b')" * 30
    assert _fold(source) == "a" + "b" * 30
    deep = "x = " + " + ".join(["'a'"] * 3000)
    assert "nested too deeply" in _why(_fold(deep))


def test_a_name_from_a_star_import_or_outside_the_files_is_unresolved():
    assert "outside the scanned files" in _why(_fold("from somewhere import T\nx = T"))
    assert "not defined" in _why(_fold("from star import *\nx = T"))


def test_the_module_does_not_import_anything_the_project_names(tmp_path, monkeypatch):
    # a module that really exists on sys.path and does something when it is imported
    marker = tmp_path / "imported"
    (tmp_path / "project_probe_module.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\nT = 'from the module'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("project_probe_module", None)
    value = _fold("import project_probe_module as m\nx = m.T")
    assert isinstance(value, Unresolved)  # a name in another module is not read by running it
    assert not marker.exists() and "project_probe_module" not in sys.modules


# ── work budgets, and names that are not what they were assigned ──────


def _chain(levels: int, fields: int = 100) -> str:
    lines = ['x0 = ""']
    for level in range(1, levels + 1):
        lines.append(f'x{level} = f"' + ("{x%d}" % (level - 1)) * fields + '"')
    return "\n".join(lines) + f"\nx = x{levels}\n"


def test_names_each_built_from_the_one_before_cost_one_pass_each():
    started = time.monotonic()
    value = _fold(_chain(7))
    assert value == ""
    assert time.monotonic() - started < 3  # a tree of 100 ** 7 visits without the memo


def test_a_fan_out_deeper_than_the_budget_is_cut_not_followed_forever():
    module = P.parse_module("m.py", _chain(10, fields=300).encode("utf-8"))
    folder = Folder(module)
    node = next(n for n in module.tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "x")
    started = time.monotonic()
    value = folder.fold(node.value)
    assert time.monotonic() - started < 3
    assert isinstance(value, Unresolved) and "too much work" in value.why
    assert folder.exhausted


def test_a_result_cut_short_by_the_budget_is_not_remembered():
    module = P.parse_module("m.py", _chain(10, fields=300).encode("utf-8"))
    folder = Folder(module)
    nodes = {n.targets[0].id: n.value for n in module.tree.body if isinstance(n, ast.Assign)}
    assert isinstance(folder.fold(nodes["x"]), Unresolved)
    # the lower levels were folded on the way: asked for directly, each is a value, not the cut
    assert folder.fold(nodes["x3"]) == ""


def test_a_huge_f_string_is_refused_as_it_grows_not_after():
    big = "a" * 4000
    started = time.monotonic()
    too_long = _fold(f'b = "{big}"\nx = f"' + "{b}" * 50 + '"')
    too_many = _fold('b = "a"\nx = f"' + "{b}" * 3000 + '"')
    assert "longer than" in _why(too_long) and "too many parts" in _why(too_many)
    assert time.monotonic() - started < 3
    assert "longer than" in _why(_fold(f'b = "{big}"\nx = "{{}}{{}}".format(b, b)'))
    assert "longer than" in _why(_fold(f'b = "{big}"\nx = "%s%s" % (b, b)'))
    assert "longer than" in _why(_fold(f'b = "{big}"\nx = ",".join([b, b, b])'))
    hundred = "[" + ",".join(["1"] * 100) + "]"
    assert "too many items" in _why(_fold("x = " + " + ".join([hundred] * 30)))


@pytest.mark.parametrize(
    "source, why",
    [
        ('CFG = {"t": "a.t"}\nCFG["t"] = "b.t"\nx = CFG["t"]', "changed in place"),
        ('CFG = {"t": "a.t"}\nCFG.update(t="b.t")\nx = CFG["t"]', "changed in place"),
        ('L = ["a.t"]\nL.insert(0, "b.t")\nx = L[0]', "changed in place"),
        ('L = ["a.t"]\ndel L[0]\nx = L', "changed in place"),
        ('T = "a.t"\ndef init():\n    global T\n    T = "b.t"\nx = T', "global or nonlocal"),
        ('class Cfg:\n    A = "a.t"\nCfg.A = "b.t"\nx = Cfg.A', "changed in place"),
    ],
)
def test_a_name_changed_elsewhere_is_not_the_value_it_was_assigned(source, why):
    assert why in _why(_fold(source))


def test_reading_an_item_does_not_change_it():
    assert _fold('D = {"k": "v"}\nprint(D["k"])\nx = D["k"]') == "v"


def _name_in(source: str, ancestor: type, name: str) -> tuple:
    module = P.parse_module("m.py", source.encode("utf-8"))
    for node in ast.walk(module.tree):
        if isinstance(node, ancestor):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Name)
                    and inner.id == name
                    and isinstance(inner.ctx, ast.Load)
                ):
                    return Folder(module), inner
    raise AssertionError("no such name")


def test_a_comprehension_or_a_lambda_that_binds_a_name_hides_the_module_constant():
    folder, name = _name_in('T = "raw.t"\nxs = [t(T) for T in ["x.a"]]\n', ast.ListComp, "T")
    # the name in the element is the loop variable; the module's T is not what it stands for
    elt = next(n for n in ast.walk(name) if isinstance(n, ast.Name) and n.id == "T")
    assert "comprehension variable" in _why(folder.fold(elt))
    folder, name = _name_in('T = "raw.t"\nf = lambda T: T\n', ast.Lambda, "T")
    assert "parameter of a lambda" in _why(folder.fold(name))


def test_the_first_iterable_of_a_comprehension_is_still_the_outer_name():
    module = P.parse_module("m.py", b'T = ["a.t", "b.t"]\nys = [t for t in T]\n')
    folder = Folder(module)
    comp = next(n for n in ast.walk(module.tree) if isinstance(n, ast.ListComp))
    assert list(folder.fold(comp.generators[0].iter)) == ["a.t", "b.t"]


# ── what one scan parses and keeps is bounded ─────────────────────────


def test_a_file_with_more_braces_than_a_job_has_is_not_parsed():
    hostile = ('b = "x"\nc = f"' + "{b}" * 60_000 + '"\n').encode("utf-8")  # 60,000 "{"
    started = time.monotonic()
    with pytest.raises(P.SourceUnreadable) as refused:
        P.parse_module("hostile.py", hostile)
    assert time.monotonic() - started < 1  # not parsed: the parse of this takes seconds
    assert "more than 50,000 '{' characters" in str(refused.value)
    ordinary = ('x = 1\ny = f"{x}-{x}"\nz = {"a": {"b": 1}}\n' * 100).encode("utf-8")
    assert P.parse_module("ordinary.py", ordinary).size == len(ordinary)


def test_the_source_a_scan_reads_is_counted_and_the_files_past_it_are_said_to_be_unread(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _fold as F
    from fluid_build.cli.import_workflow import _scan
    from fluid_build.cli.import_workflow._conventions import Conventions

    _write(
        tmp_path,
        {f"jobs/j{i}.py": f'spark.table("db.t{i}")\n' + "# padding\n" * 20 for i in range(6)},
    )
    one = len((tmp_path / "jobs" / "j0.py").read_bytes())
    monkeypatch.setattr(F, "SourceBudget", lambda: P.SourceBudget(left=3 * one + one // 2))
    scan = _scan.scan_jobs(tmp_path / "jobs", Conventions())
    assert len(scan.jobs) == 3
    assert sorted(scan.unreadable) == ["j3.py", "j4.py", "j5.py"]
    assert all("the most it reads" in why for why in scan.unreadable.values())
    assert any("3 file(s) not read" in note for note in scan.notes)


def test_a_scan_with_room_for_everything_says_nothing_about_a_limit(tmp_path):
    from fluid_build.cli.import_workflow import _scan
    from fluid_build.cli.import_workflow._conventions import Conventions

    _write(tmp_path, {"jobs/a.py": 'spark.table("db.a")\n', "jobs/b.py": 'spark.table("db.b")\n'})
    scan = _scan.scan_jobs(tmp_path / "jobs", Conventions())
    assert len(scan.jobs) == 2 and not any("not read" in n for n in scan.notes)


def test_only_a_few_modules_stay_parsed_and_one_that_was_dropped_is_read_again(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _fold as F

    files = {f"m{i}.py": f"V{i} = 'value-{i}'\n" for i in range(5)}
    files["job.py"] = "from m0 import V0\nx = V0\n"
    _write(tmp_path, files)
    walk = P.find_python_files(tmp_path)
    monkeypatch.setattr(F, "MAX_CACHED_NODES", 11)  # room for two of these files (5 nodes each)
    index = ModuleIndex(walk.files)
    by_rel = {f.rel: f for f in walk.files}
    for i in range(1, 5):
        assert index.load(by_rel[f"m{i}.py"]) is not None
    assert len(index._modules) <= 3 and "m1.py" not in index._modules  # the oldest were dropped
    assert index._cached_nodes == sum(m.nodes for m in index._modules.values())
    assert set(index._folders) <= set(index._modules)  # a folder is not kept without its module
    # m0 was never loaded; the job's import reads it, whatever has been dropped
    job = index.load(by_rel["job.py"])
    assert job is not None
    node = next(n for n in job.tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "x")  # type: ignore[attr-defined]
    assert index.folder(job).fold(node.value) == "value-0"
    # and a dropped module comes back as the same module, parsed again
    again = index.load(by_rel["m1.py"])
    assert again is not None and index.load(by_rel["m1.py"]) is again


def test_a_module_read_again_is_paid_from_the_reload_budget_not_the_jobs_budget(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _fold as F

    _write(tmp_path, {f"m{i}.py": f"V{i} = 'value-{i}'\n" for i in range(3)})
    walk = P.find_python_files(tmp_path)
    monkeypatch.setattr(F, "MAX_CACHED_NODES", 6)  # room for one of these files (5 nodes)
    one = len((tmp_path / "m0.py").read_bytes())
    budget = P.SourceBudget(reload_left=2 * one)
    index = ModuleIndex(walk.files, budget=budget)
    by_rel = {f.rel: f for f in walk.files}
    for i in range(3):
        assert index.load(by_rel[f"m{i}.py"]) is not None  # the first reads: the main budget
    spent = budget.left
    assert index.load(by_rel["m0.py"]) is not None  # two reads again fit
    assert index.load(by_rel["m1.py"]) is not None
    assert budget.left == spent and budget.reload_left == 0
    assert index.load(by_rel["m2.py"]) is None  # a third does not, and the reason says so
    assert "read modules again" in index.unreadable["m2.py"]


def test_the_memo_of_a_module_holds_a_bounded_amount_of_text(monkeypatch):
    from fluid_build.cli.import_workflow import _fold as F

    monkeypatch.setattr(F, "MAX_MEMO_CHARS", 10_000)
    source = f"S = '{'a' * 3000}'\n" + "".join(f"X{i} = S + '{i}'\n" for i in range(10))
    module = P.parse_module("m.py", source.encode("utf-8"))
    folder = Folder(module)
    for i, node in enumerate(module.tree.body[1:]):
        assert folder.fold(node.value) == "a" * 3000 + str(i)  # type: ignore[attr-defined]
    assert folder._memo_chars <= 10_000


def test_a_module_the_index_did_not_load_gets_a_folder_nobody_keeps(tmp_path):
    _write(tmp_path, {"a.py": "X = 1\n"})
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    stranger = P.parse_module("dag.py", b"Y = 2\n")
    first, second = index.folder(stranger), index.folder(stranger)
    assert first is not second and not index._folders


# ── names the file changes in ways a single assignment does not show ──


def _value_of(source: str, name: str = "X"):
    module = P.parse_module("m.py", source.encode("utf-8"))
    for node in module.tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return Folder(module).fold(node.value)
    raise AssertionError(f"no assignment to {name}")


@pytest.mark.parametrize(
    "source",
    [
        'T = "a"\nif (T := "b"):\n    pass\nX = T\n',
        'T = "a"\nmatch 1:\n    case T:\n        pass\nX = T\n',
        'T = "a"\ndel T\nX = T\n',
        'C = {"t": "x"}\nD = C\nD["t"] = "y"\nX = C["t"]\n',
        'C = {"t": "x"}\nD = C\nC.update(t="y")\nX = D["t"]\n',
        'T = "a"\nglobals()["T"] = "b"\nX = T\n',
        'T = "a"\nvars()["T"] = "b"\nX = T\n',
        'T = "a"\nlocals()["T"] = "b"\nX = T\n',
        'T = "a"\nglobals().update(T="b")\nX = T\n',
        'T = "a"\nexec("T = 1")\nX = T\n',
        'import sys\nT = "a"\nsetattr(sys.modules[__name__], "T", "b")\nX = T\n',
        'import sys\nT = "a"\nsys.modules[__name__].__dict__["T"] = "b"\nX = T\n',
        'import sys\nT = "a"\nm = sys.modules[__name__]\nm.T = "b"\nX = T\n',
        'import importlib\nT = "a"\nsetattr(importlib.import_module(__name__), "T", "b")\nX = T\n',
        'T = "a"\nsetattr(make(), "T", "b")\nX = T\n',
        'class K:\n    t = "x"\nsetattr(K, "t", "y")\nX = K.t\n',
        'class K:\n    t = "x"\ndelattr(K, "t")\nX = K.t\n',
        'C = {"t": "x"}\nfor C["t"] in ["y"]:\n    pass\nX = C["t"]\n',
        'C = {"t": "x"}\nwith ctx() as C["t"]:\n    pass\nX = C["t"]\n',
        'C = {"t": "x"}\nitems = [1 for C["t"] in ["y"]]\nX = C["t"]\n',
        'C = {"t": "x"}\n[C][0]["t"] = "y"\nX = C["t"]\n',
        'C = {"t": "x"}\ndict.update(C, t="y")\nX = C["t"]\n',
        'L = ["x"]\nlist.insert(L, 0, "y")\nX = L[0]\n',
        'import operator\nC = {"t": "x"}\noperator.setitem(C, "t", "y")\nX = C["t"]\n',
        'from operator import setitem\nC = {"t": "x"}\nsetitem(C, "t", "y")\nX = C["t"]\n',
        'C = {"t": "x"}\ngetattr(C, "update")(t="y")\nX = C["t"]\n',
        'C = {"t": "x"}\ndef put(d):\n    d["t"] = "y"\nput(C)\nX = C["t"]\n',
        'C = {"t": "x"}\ndef put(d, v):\n    d["t"] = v\nput(d=C, v="y")\nX = C["t"]\n',
        'C = {"t": "x"}\ndef put(d):\n    d["t"] = "y"\ndef put_all(e):\n    put(e)\nput_all(C)\nX = C["t"]\n',
        'C = {"t": "x"}\nD = {"c": C}\nD["c"]["t"] = "y"\nX = C["t"]\n',
        'C = {"t": "x"}\nD = {"c": C}\nD["c"].update(t="y")\nX = C["t"]\n',
    ],
)
def test_a_name_the_file_changes_by_any_route_is_not_a_constant(source):
    value = _value_of(source)
    assert isinstance(value, Unresolved), value


@pytest.mark.parametrize(
    "source",
    [
        'T = "a"\nargs = parse()\nd = vars(args)\nX = T\n',
        'T = "a"\nobj = make()\nsetattr(obj, "x", 1)\nX = T\n',
        'T = "a"\nclass A:\n    def m(self):\n        return self.__dict__\nX = T\n',
        'T = "a"\nclass A:\n    def m(self, k, v):\n        setattr(self, k, v)\nX = T\n',
        'T = "a"\ndef f(x):\n    return "{x}".format(**locals())\nX = T\n',
        'T = "a"\ndef f():\n    d = vars()\n    d["k"] = 1\n    return d\nX = T\n',
        'T = "a"\nname = globals_of_the_job\nX = T\n',
    ],
)
def test_reading_names_by_string_or_changing_an_object_that_is_no_constant_voids_nothing(source):
    assert _value_of(source) == "a"


@pytest.mark.parametrize(
    "source",
    [
        'T = "a"\ndef put(d):\n    d["t"] = "y"\nother = {}\nput(other)\nX = T\n',
        'T = "a"\nL = [T]\nL.append("b")\nX = T\n',
        'T = "a"\nD = {"c": T}\nD["c"] = "z"\nX = T\n',
        'T = "a"\ndef put(d):\n    d["t"] = "y"\nX = T\n',
        'T = "a"\ndef put(d):\n    d["t"] = "y"\nput(make())\nX = T\n',
    ],
)
def test_a_name_held_beside_a_change_but_not_reached_by_it_is_still_a_constant(source):
    assert _value_of(source) == "a"


def test_a_name_nothing_changes_still_folds_beside_names_that_are_changed():
    value = _value_of('T = "a"\nU = "b"\nif (U := "c"):\n    pass\nX = T\n')
    assert value == "a"


def test_a_type_alias_rebinds_its_name_on_python_312():
    if not hasattr(ast, "TypeAlias"):
        pytest.skip("type aliases are Python 3.12")
    assert isinstance(_value_of('T = "a"\ntype T = int\nX = T\n'), Unresolved)


# ── what a string method costs is judged before it is paid ────────────


def test_a_replace_that_would_be_too_long_is_not_built():
    import resource

    big = "a" * 4000
    source = f'A = "{big}"\nB = "{big}"\nX = A.replace("a", B)\nY = A.replace("", B)\n'
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for name in ("X", "Y"):
        value = _value_of(source, name)
        assert isinstance(value, Unresolved) and "longer than" in value.why
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert grown < 40 * 1024 * 1024  # a 16 MB string is not built (ru_maxrss: bytes on macOS)


def test_a_case_change_that_lengthens_a_string_past_the_limit_is_refused():
    text = "\u00df" * 3000  # upper() makes it SS: twice as long
    value = _value_of(f'A = "{text}"\nX = A.upper()\n')
    assert isinstance(value, Unresolved) and "longer than" in value.why


# ── a scan's cost does not grow with what a name could be ─────────────


def test_a_name_many_files_answer_to_is_looked_up_once(tmp_path):
    files = {f"p{i}/config.py": f"T = 'v{i}'\n" for i in range(300)}
    files["job.py"] = "from config import T\nx = T\n"
    _write(tmp_path, files)
    walk = P.find_python_files(tmp_path)
    index = ModuleIndex(walk.files)
    calls = []
    original = index._candidates

    def counting(dotted, from_rel):
        calls.append(dotted)
        return original(dotted, from_rel)

    index._candidates = counting  # type: ignore[method-assign]
    for _ in range(50):
        assert isinstance(index.resolve("config.T", "job.py"), str)  # 300 files answer to it
    assert len(calls) == 1


def test_an_enormous_expression_is_described_without_being_unparsed():
    import time

    source = "x = " + "(" * 150 + "f(" + "a," * 60_000 + ")" + "+1)" * 150 + "\n"
    module = P.parse_module("big.py", source.encode("utf-8"))
    node = module.tree.body[0].value  # type: ignore[attr-defined]
    started = time.monotonic()
    for _ in range(200):
        assert P.snippet(node) == "<expression>"
    assert time.monotonic() - started < 0.5


def test_an_expression_of_a_few_long_lines_is_not_unparsed_to_be_described():
    import time

    row = ",".join(
        "a" for _ in range(3000)
    )  # 30 lines of 6 KB: neither many lines nor one wide one
    source = "x = f(\n" + ",\n".join([row] * 30) + ")\n"
    module = P.parse_module("wide.py", source.encode("utf-8"))
    node = module.tree.body[0].value  # type: ignore[attr-defined]
    started = time.monotonic()
    assert P.snippet(node) == "<expression>"  # about 200 nodes are counted, not 90,000 unparsed
    assert time.monotonic() - started < 0.2
    started = time.monotonic()
    for _ in range(1000):
        assert P.snippet(node) == "<expression>"  # and the answer is kept
    assert time.monotonic() - started < 0.2
    text = "x = '" + "q" * 5000 + "'\n"  # one long string: the text counts, not the nodes
    assert P.snippet(P.parse_module("s.py", text.encode()).tree.body[0].value) == "<expression>"  # type: ignore[attr-defined]
    small = P.parse_module("t.py", b"x = spark.table('db.orders').where(a > 1)\n")
    assert P.snippet(small.tree.body[0].value) == "spark.table('db.orders').where(a > 1)"  # type: ignore[attr-defined]


def test_braces_are_counted_in_the_encoding_the_parser_reads():
    fields = "+AHsAYgB9-" * 60_000  # {b} in UTF-7, 60,000 of them
    hostile = ("# coding: utf-7\n" + "c = f'" + fields + "'\n").encode("ascii")
    assert hostile.count(b"{") == 0  # the bytes hold none
    with pytest.raises(P.SourceUnreadable) as refused:
        P.parse_module("utf7.py", hostile)
    assert "more than 50,000 '{' characters" in str(refused.value)
    latin = "# coding: latin-1\nx = '\xe9'\n".encode("latin-1")
    assert P.parse_module("latin.py", latin).nodes > 0
