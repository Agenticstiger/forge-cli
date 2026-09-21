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

"""A declared `parameters.inputs` entry is bound in the generated script.

The contract says which file backs which name; the author's SQL then says
`FROM <name>`. The local provider has always honoured that during `fluid
apply` -- it registers each input as a DuckDB view -- but the *emitted*
script did not, so a project that applied cleanly still failed on its own
with `Table with name <name> does not exist`. Six shipped examples (02-06
and local/high_value_churn) were in exactly that state.

The statement is the provider's own, shared via
`providers/_duckdb_read.build_register_view_sql`, so `fluid apply` and the
generated script cannot drift apart again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.engines.sql.scripts import _INPUTS_FILENAME, generate_scripts
from fluid_build.providers._duckdb_read import build_register_view_sql

CONTRACT = {"id": "demo.contract"}


def _build(inputs, *, platform="local", sql="SELECT 1 AS a"):
    return {
        "id": "main",
        "properties": {"sql": sql, "parameters": {"inputs": inputs}},
        "execution": {"runtime": {"platform": platform}},
    }


class TestTheBinding:
    def test_a_declared_input_becomes_a_view(self):
        files = generate_scripts(CONTRACT, _build([{"name": "hv", "path": "data/x.csv"}]))
        assert _INPUTS_FILENAME in files
        assert "CREATE OR REPLACE VIEW hv AS" in files[_INPUTS_FILENAME]

    def test_the_inputs_file_sorts_before_the_build_file(self):
        """A runner executes in filename order, so the views have to exist
        before the script that selects from them."""
        files = generate_scripts(CONTRACT, _build([{"name": "hv", "path": "data/x.csv"}]))
        assert sorted(files)[0] == _INPUTS_FILENAME

    def test_each_input_is_named_in_a_comment(self):
        body = generate_scripts(CONTRACT, _build([{"name": "hv", "path": "data/x.csv"}]))[
            _INPUTS_FILENAME
        ]
        assert "-- hv <- data/x.csv" in body

    def test_parquet_uses_read_parquet(self):
        body = generate_scripts(CONTRACT, _build([{"name": "p", "path": "data/x.parquet"}]))[
            _INPUTS_FILENAME
        ]
        assert "read_parquet(" in body

    def test_several_inputs_all_bind(self):
        files = generate_scripts(
            CONTRACT,
            _build([{"name": "hv", "path": "a.csv"}, {"name": "deg", "path": "b.csv"}]),
        )
        body = files[_INPUTS_FILENAME]
        assert "VIEW hv AS" in body and "VIEW deg AS" in body

    def test_it_is_the_providers_own_statement(self):
        """Not a re-implementation: byte-identical to what `fluid apply`
        executes, which is the whole point of sharing it."""
        body = generate_scripts(CONTRACT, _build([{"name": "hv", "path": "data/x.csv"}]))[
            _INPUTS_FILENAME
        ]
        assert build_register_view_sql("hv", Path("data/x.csv"), "csv", None) in body


class TestWhenItRefusesToBind:
    def test_no_inputs_means_no_file(self):
        files = generate_scripts(CONTRACT, _build([]))
        assert _INPUTS_FILENAME not in files

    def test_a_non_local_platform_gets_no_duckdb_readers(self, caplog):
        """`read_csv_auto` is DuckDB's, and a local file path means nothing
        on a warehouse target -- emitting it would be SQL the engine cannot
        parse."""
        with caplog.at_level("WARNING"):
            files = generate_scripts(
                CONTRACT,
                _build([{"name": "hv", "path": "data/x.csv"}], platform="snowflake"),
            )
        assert _INPUTS_FILENAME not in files
        assert "sql_inputs_not_bound_for_platform" in caplog.text

    @pytest.mark.parametrize("platform", ["", "local", "duckdb", "LOCAL"])
    def test_local_aliases_all_bind(self, platform):
        files = generate_scripts(
            CONTRACT, _build([{"name": "hv", "path": "data/x.csv"}], platform=platform)
        )
        assert _INPUTS_FILENAME in files

    def test_an_entry_missing_a_path_does_not_sink_the_others(self):
        files = generate_scripts(
            CONTRACT, _build([{"name": "broken"}, {"name": "ok", "path": "a.csv"}])
        )
        body = files[_INPUTS_FILENAME]
        assert "VIEW ok AS" in body
        assert "-- Not bound: input broken" in body

    def test_nothing_bindable_means_no_file_rather_than_comments_only(self, caplog):
        """A file of only comments has no executable statement; the
        quickstart guard counts that as a failure, and rightly so."""
        with caplog.at_level("WARNING"):
            files = generate_scripts(CONTRACT, _build([{"name": "broken"}]))
        assert _INPUTS_FILENAME not in files
        assert "sql_inputs_none_bindable" in caplog.text

    def test_a_name_that_is_not_an_identifier_is_refused_visibly(self, caplog):
        with caplog.at_level("WARNING"):
            files = generate_scripts(
                CONTRACT,
                _build(
                    [{"name": "not-an-ident", "path": "a.csv"}, {"name": "ok", "path": "b.csv"}]
                ),
            )
        body = files[_INPUTS_FILENAME]
        # The refused name appears only in the stated reason, never as an
        # identifier in a statement.
        statements = [l for l in body.splitlines() if l.startswith("CREATE")]
        assert not any("not-an-ident" in s for s in statements)
        assert "-- Not bound:" in body
        assert "VIEW ok AS" in body
        assert "sql_input_not_bindable" in caplog.text


class TestInjectionSurfaces:
    def test_the_path_is_a_quoted_literal_not_interpolated_sql(self):
        body = generate_scripts(
            CONTRACT, _build([{"name": "hv", "path": "a'; DROP TABLE victim; --"}])
        )[_INPUTS_FILENAME]
        statement = [l for l in body.splitlines() if l.startswith("CREATE")][0]
        assert "''" in statement, "the quote should have been doubled, not closed"

    def test_a_newline_in_a_path_is_inert(self):
        """A line-based reading of the emitted file is misleading here: the
        newline lands INSIDE the quoted literal, so line 2 of a multi-line
        string looks like a statement but is not. The only assertion worth
        making is behavioural."""
        duckdb = pytest.importorskip("duckdb")
        body = generate_scripts(
            CONTRACT, _build([{"name": "hv", "path": "a.csv\nDROP TABLE victim; --"}])
        )[_INPUTS_FILENAME]
        con = duckdb.connect()
        con.execute("CREATE TABLE victim(a int)")
        with pytest.raises(Exception):
            con.execute(body)  # the path does not exist; that is the only failure
        assert (
            con.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name='victim'"
            ).fetchone()[0]
            == 1
        )

    def test_a_newline_in_a_path_cannot_escape_the_comment_line(self):
        """The comment header is a separate surface from the literal, and
        there a newline WOULD end the comment -- so it is flattened."""
        body = generate_scripts(
            CONTRACT, _build([{"name": "hv", "path": "a.csv\nDROP TABLE victim; --"}])
        )[_INPUTS_FILENAME]
        comment = [l for l in body.splitlines() if l.startswith("-- hv <-")][0]
        assert "DROP TABLE victim" in comment, "flattened onto the one comment line"

    def test_an_option_key_cannot_smuggle_ddl(self):
        with pytest.raises(Exception):
            build_register_view_sql("t", Path("a.csv"), "csv", {"x:=1, y": "z"})
