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

"""Tests for fluid_build.util.cron."""

import re
from unittest.mock import patch

import pytest


class TestGetCron:
    """Tests for get_cron()."""

    @patch("fluid_build.util.cron.get_primary_build")
    def test_returns_cron_when_present(self, mock_gpb):
        from fluid_build.util.cron import get_cron

        mock_gpb.return_value = {"execution": {"trigger": {"cron": "0 6 * * *"}}}
        assert get_cron({}) == "0 6 * * *"

    @patch("fluid_build.util.cron.get_primary_build")
    def test_returns_none_when_no_trigger(self, mock_gpb):
        from fluid_build.util.cron import get_cron

        mock_gpb.return_value = {"execution": {}}
        assert get_cron({}) is None

    @patch("fluid_build.util.cron.get_primary_build")
    def test_returns_none_when_no_build(self, mock_gpb):
        from fluid_build.util.cron import get_cron

        mock_gpb.return_value = None
        assert get_cron({}) is None

    @patch("fluid_build.util.cron.get_primary_build")
    def test_returns_none_on_exception(self, mock_gpb):
        from fluid_build.util.cron import get_cron

        mock_gpb.side_effect = KeyError("bad contract")
        assert get_cron({}) is None

    @patch("fluid_build.util.cron.get_primary_build")
    def test_returns_none_when_no_execution(self, mock_gpb):
        from fluid_build.util.cron import get_cron

        mock_gpb.return_value = {}
        assert get_cron({}) is None


class TestUnixToQuartz:
    """Tests for unix_to_quartz(): the five-field cron Databricks Jobs runs as Quartz."""

    @pytest.mark.parametrize(
        "expression, quartz",
        [
            ("@daily", "0 0 0 * * ?"),
            ("@midnight", "0 0 0 * * ?"),
            ("@hourly", "0 0 * * * ?"),
            ("@weekly", "0 0 0 ? * SUN"),
            ("@monthly", "0 0 0 1 * ?"),
            ("@yearly", "0 0 0 1 1 ?"),
            ("@DAILY", "0 0 0 * * ?"),
            ("0 6 * * *", "0 0 6 * * ?"),
            ("  0   6  *  * * ", "0 0 6 * * ?"),
            ("*/15 * * * *", "0 */15 * * * ?"),
        ],
    )
    def test_nicknames_and_plain_expressions_gain_a_seconds_field_and_a_question_mark(
        self, expression, quartz
    ):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz(expression) == quartz

    def test_weekday_range_is_written_by_name_with_the_day_of_month_a_question_mark(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 9 * * 1-5") == "0 0 9 ? * MON,TUE,WED,THU,FRI"

    def test_seven_and_zero_are_both_sunday(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 0 * * 7") == "0 0 0 ? * SUN"
        assert unix_to_quartz("0 0 * * 0") == "0 0 0 ? * SUN"
        assert unix_to_quartz("0 0 * * 0,7") == "0 0 0 ? * SUN"

    def test_five_to_seven_covers_friday_saturday_and_sunday(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 0 * * 5-7") == "0 0 0 ? * SUN,FRI,SAT"

    def test_a_step_in_day_of_week_selects_every_nth_day(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 0 * * */2") == "0 0 0 ? * SUN,TUE,THU,SAT"

    def test_day_names_are_accepted_in_any_case(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 0 * * mon,Wed") == "0 0 0 ? * MON,WED"

    def test_month_names_pass_through(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("0 0 1 JAN,jul *") == "0 0 0 1 JAN,jul ?"

    def test_a_restricted_day_of_month_alone_is_accepted(self):
        from fluid_build.util.cron import unix_to_quartz

        assert unix_to_quartz("30 2 15 * *") == "0 30 2 15 * ?"

    @pytest.mark.parametrize(
        "expression, fragment",
        [
            ("0 0 1 * 1", "both the day of the month and the day of the week"),
            ("60 * * * *", "outside 0-59"),
            ("* 24 * * *", "outside 0-23"),
            ("* * 0 * *", "outside 1-31"),
            ("* * 32 * *", "outside 1-31"),
            ("* * * 13 *", "outside 1-12"),
            ("* * * * 8", "outside 0-7"),
            ("* * * FOO *", "outside 1-12"),
            ("* * * * FUNDAY", "outside 0-7"),
            ("30-10 * * * *", "runs backwards"),
            ("* * * * 5-1", "runs backwards"),
            ("*/0 * * * *", "step of 0"),
            ("0 0 * * */0", "step of 0"),
            ("0 0 L * *", "outside 1-31"),
            ("0 0 * * 5L", "outside 0-7"),
            ("0 0 * * 1#2", "not cron syntax"),
            ("0 0 ? * *", "not cron syntax"),
            ("0 0 * * ?", "not cron syntax"),
            ("* * * *", "4 field(s)"),
            ("* * * * * *", "6 field(s)"),
            ("", "1 field(s)"),
            ("@sometimes", "1 field(s)"),
        ],
    )
    def test_malformed_expressions_are_a_value_error_naming_the_problem(self, expression, fragment):
        from fluid_build.util.cron import unix_to_quartz

        with pytest.raises(ValueError, match=re.escape(fragment)):
            unix_to_quartz(expression)

    def test_none_is_a_value_error_not_a_type_error(self):
        from fluid_build.util.cron import unix_to_quartz

        with pytest.raises(ValueError):
            unix_to_quartz(None)  # type: ignore[arg-type]
