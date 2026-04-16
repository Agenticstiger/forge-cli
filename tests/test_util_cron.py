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

from unittest.mock import patch


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
