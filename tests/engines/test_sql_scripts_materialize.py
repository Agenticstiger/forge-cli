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

"""Each sql-engine stage materialises a view named after its declared output.

The emitter used to write bare SELECTs, so nothing created a relation under
any name and a stage's `FROM <upstream's output>` could never resolve -- the
generated project did not run. Every cross-stage reference in every shipped
quickstart is an OUTPUT name, never a stage name, which is what makes the
output the right thing to name the view after.

`tests/test_quickstart_templates_execute.py` proves the end result against a
real DuckDB. This file pins the emitter's decisions.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.base import TransformationIntent
from fluid_build.engines.sql.scripts import (
    _ALREADY_A_SINK,
    _generate_from_intent,
    _generate_multi_stage,
    _view_ref,
    generate_scripts,
)

CONTRACT = {"id": "demo.contract"}


def _multi(*stages):
    return _generate_multi_stage(CONTRACT, {"properties": {"stages": list(stages)}})


def _stage(name, sql="SELECT 1 AS a", **kw):
    return {"name": name, "properties": {"sql": sql}, **kw}


class TestTheHappyPath:
    def test_a_stage_is_wrapped_in_a_view_named_after_its_output(self):
        body = _multi(_stage("stg_customers", outputs=["customer_base"]))["01_stg_customers.sql"]
        assert "CREATE OR REPLACE VIEW customer_base AS" in body
        assert body.rstrip().endswith(";")

    def test_the_filename_still_comes_from_the_stage_name(self):
        """Only the RELATION is named after the output. Renaming files would
        break anyone whose runner references them positionally."""
        files = _multi(_stage("stg_customers", outputs=["customer_base"]))
        assert list(files) == ["01_stg_customers.sql"]

    def test_the_header_says_what_was_materialised(self):
        body = _multi(_stage("s", outputs=["orders"]))["01_s.sql"]
        assert "-- Materialises: orders  (CREATE OR REPLACE VIEW)" in body

    def test_the_stage_sql_survives_verbatim(self):
        sql = "SELECT a, b\nFROM t\nWHERE a > 1"
        body = _multi(_stage("s", sql=sql, outputs=["o"]))["01_s.sql"]
        assert sql in body

    def test_a_trailing_semicolon_is_not_doubled(self):
        body = _multi(_stage("s", sql="SELECT 1;", outputs=["o"]))["01_s.sql"]
        assert ";\n;" not in body
        assert body.count(";") == 1

    def test_the_first_of_several_outputs_wins(self):
        body = _multi(_stage("s", outputs=["first", "second"]))["01_s.sql"]
        assert "VIEW first AS" in body
        assert "second" not in body


class TestWhenItRefusesToMaterialise:
    """Every refusal is stated in the file itself, not just swallowed."""

    def test_a_stage_with_no_outputs_stays_a_bare_select(self):
        """first-dag's `validate_customers` is exactly this -- a terminal
        assertion stage with nothing to create."""
        body = _multi(_stage("validate_customers"))["01_validate_customers.sql"]
        assert "CREATE OR REPLACE VIEW" not in body
        assert "-- Not materialised: stage declares no output it exclusively owns" in body

    def test_sql_that_already_writes_is_not_double_wrapped(self):
        body = _multi(_stage("s", sql="INSERT INTO t SELECT * FROM u", outputs=["o"]))["01_s.sql"]
        assert "CREATE OR REPLACE VIEW" not in body
        assert "-- Not materialised: SQL already writes to its own target" in body

    def test_two_stages_claiming_one_output_materialise_neither(self):
        """Files are keyed by path so both would still be written, but both
        `CREATE OR REPLACE VIEW orders` run against ONE catalog -- the second
        silently replaces the first. Giving it to neither makes a downstream
        `FROM orders` fail loudly instead."""
        files = _multi(
            _stage("a", sql="SELECT 1 AS a", outputs=["orders"]),
            _stage("b", sql="SELECT 2 AS b", outputs=["orders"]),
        )
        assert len(files) == 2
        assert all("CREATE OR REPLACE VIEW" not in body for body in files.values())
        joined = "\n".join(files.values())
        assert "SELECT 1 AS a" in joined and "SELECT 2 AS b" in joined

    def test_the_collision_is_logged(self, caplog):
        with caplog.at_level("WARNING"):
            _multi(_stage("a", outputs=["orders"]), _stage("b", outputs=["orders"]))
        assert "sql_stage_output_name_collision" in caplog.text

    @pytest.mark.parametrize(
        "bad", ["customer-base", "1st_stage", "drop table x", "customer base", ""]
    )
    def test_a_name_that_is_not_a_usable_identifier_degrades_visibly(self, bad, caplog):
        with caplog.at_level("WARNING"):
            files = _multi(_stage("s", outputs=[bad] if bad else []))
        body = files["01_s.sql"]
        assert "CREATE OR REPLACE VIEW" not in body
        assert "-- Not materialised:" in body

    def test_an_unfinished_stage_is_not_given_a_dummy_view(self):
        """A stage with no SQL emits a TODO. Materialising its `SELECT 1`
        placeholder would let downstream stages run against a dummy relation
        and look like they worked."""
        body = _multi({"name": "s", "outputs": ["o"], "properties": {}})["01_s.sql"]
        assert "CREATE OR REPLACE VIEW" not in body
        assert "TODO" in body


class TestIdentifierSafety:
    def test_a_dotted_name_is_validated_per_segment(self):
        assert _view_ref("analytics.customer_base") == "analytics.customer_base"

    @pytest.mark.parametrize(
        "payload",
        [
            "pwned AS SELECT 1; DROP TABLE victim; --",
            "a--b",
            "x; DELETE FROM y",
            "a.'b",
            "..",
            "a..b",
        ],
    )
    def test_injection_payloads_are_rejected(self, payload):
        with pytest.raises(ValueError):
            _view_ref(payload)

    def test_an_injection_payload_never_reaches_the_ddl(self):
        payload = "pwned AS SELECT 1; DROP TABLE victim; --"
        body = _multi(_stage("s", outputs=[payload]))["01_s.sql"]
        assert "CREATE OR REPLACE VIEW" not in body
        # The payload is echoed back in the refusal reason, which is a
        # comment -- so what matters is that it stays inside one.
        assert all(
            line.startswith("--") or not line.strip() or line.startswith("SELECT")
            for line in body.splitlines()
        )


class TestTheCommentHeaderCannotBeEscaped:
    """A newline in a contract value would close the `--` comment and put the
    rest of the value at statement level. These files are meant to be RUN, so
    that is arbitrary SQL execution, not a formatting wart.

    Measured before the fix: a stage named `s\\nDROP TABLE victim; --`
    dropped the table. Six fields reach a comment line."""

    PAYLOAD = "\nDROP TABLE victim; --"

    def _statement_lines(self, body):
        """Every line that is not a comment or blank -- i.e. what runs."""
        return [
            line
            for line in body.splitlines()
            if line.strip() and not line.lstrip().startswith("--")
        ]

    def test_a_stage_name_cannot_escape(self):
        body = _multi(_stage("s" + self.PAYLOAD, outputs=["o"]))
        assert "DROP TABLE" not in "\n".join(self._statement_lines(next(iter(body.values()))))

    def test_a_dependson_entry_cannot_escape(self):
        body = _multi(_stage("s", outputs=["o"], dependsOn=["a" + self.PAYLOAD]))["01_s.sql"]
        assert "DROP TABLE" not in "\n".join(self._statement_lines(body))

    def test_a_contract_id_cannot_escape(self):
        files = _generate_multi_stage(
            {"id": "x" + self.PAYLOAD},
            {"properties": {"stages": [_stage("s", outputs=["o"])]}},
        )
        assert "DROP TABLE" not in "\n".join(self._statement_lines(files["01_s.sql"]))

    def test_an_output_name_cannot_escape_through_the_refusal_reason(self):
        body = _multi(_stage("s", outputs=["ok" + self.PAYLOAD]))["01_s.sql"]
        assert "DROP TABLE" not in "\n".join(self._statement_lines(body))

    def test_an_unfinished_stages_todo_line_cannot_escape(self):
        files = _multi({"name": "s" + self.PAYLOAD, "outputs": ["o"], "properties": {}})
        body = next(iter(files.values()))
        assert "DROP TABLE" not in "\n".join(self._statement_lines(body))

    def test_the_embedded_pattern_cannot_escape_either(self):
        files = generate_scripts(
            {"id": "x" + self.PAYLOAD},
            {"id": "main", "properties": {"sql": "SELECT 1 AS a"}},
        )
        assert "DROP TABLE" not in "\n".join(self._statement_lines(files["main.sql"]))

    def test_the_guard_runs_against_a_real_engine(self):
        """Structural assertions above are the pin; this proves the premise."""
        duckdb = pytest.importorskip("duckdb")
        body = next(iter(_multi(_stage("s" + self.PAYLOAD, outputs=["o"])).values()))
        con = duckdb.connect()
        con.execute("CREATE TABLE victim(a int)")
        con.execute(body)
        assert (
            con.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name='victim'"
            ).fetchone()[0]
            == 1
        )


class TestTheSinkPattern:
    @pytest.mark.parametrize(
        "sql,is_sink",
        [
            ("SELECT 1", False),
            ("select 1", False),
            ("WITH c AS (SELECT 1) SELECT * FROM c", False),
            ("-- create the daily view\nSELECT 1", False),
            ("/* create */ SELECT 1", False),
            ("INSERT INTO t SELECT 1", True),
            ("insert into t select 1", True),
            ("CREATE TABLE t AS SELECT 1", True),
            ("MERGE INTO t USING s ON 1=1", True),
            ("COPY t TO 'f.parquet'", True),
            ("-- a comment\nINSERT INTO t SELECT 1", True),
        ],
    )
    def test_classification(self, sql, is_sink):
        assert bool(_ALREADY_A_SINK.match(sql)) is is_sink


class TestOrdering:
    def test_out_of_order_declaration_warns(self, caplog):
        with caplog.at_level("WARNING"):
            _multi(
                _stage("b", outputs=["ob"], dependsOn=["a"]),
                _stage("a", outputs=["oa"]),
            )
        assert "sql_stage_order_not_topological" in caplog.text

    def test_in_order_declaration_is_silent(self, caplog):
        with caplog.at_level("WARNING"):
            _multi(_stage("a", outputs=["oa"]), _stage("b", outputs=["ob"], dependsOn=["a"]))
        assert "sql_stage_order_not_topological" not in caplog.text

    def test_an_unknown_dependency_does_not_warn(self, caplog):
        """A dependency on something outside this build is not an ordering
        problem within it."""
        with caplog.at_level("WARNING"):
            _multi(_stage("a", outputs=["oa"], dependsOn=["somewhere_else"]))
        assert "sql_stage_order_not_topological" not in caplog.text


class TestTheEmbeddedPatternIsUntouched:
    """A single-stage build has no `outputs` to name a view after -- all 13
    shipped templates have `builds[].outputs` unset -- and nothing downstream
    inside the contract to chain to. Inventing the build id as a name is the
    guessed-name failure #632 was written to end."""

    def test_no_ddl_is_added(self):
        files = generate_scripts(CONTRACT, {"id": "main", "properties": {"sql": "SELECT 1 AS a"}})
        assert list(files) == ["main.sql"]
        assert "CREATE OR REPLACE VIEW" not in files["main.sql"]


class TestTheFromIntentPath:
    def _intent(self, *stages):
        intent = TransformationIntent()
        intent.stages = list(stages)
        return intent

    def test_intent_stages_materialise_too(self):
        files = _generate_from_intent(
            CONTRACT, self._intent({"name": "s", "sql": "SELECT 1", "outputs": ["orders"]})
        )
        assert "CREATE OR REPLACE VIEW orders AS" in files["01_s.sql"]

    def test_a_malformed_intent_stage_is_skipped_not_raised(self):
        """The contract path has always skipped these; this one raised
        AttributeError on a non-dict entry."""
        files = _generate_from_intent(
            CONTRACT,
            self._intent("junk", None, {"name": "ok", "sql": "SELECT 1", "outputs": ["o"]}),
        )
        assert list(files) == ["03_ok.sql"]
