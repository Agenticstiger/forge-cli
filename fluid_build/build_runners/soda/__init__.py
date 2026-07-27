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

"""Soda Core integration — shells out to the user-installed ``soda`` binary."""

from .runner import SodaNotInstalled, SodaResult, resolve_soda_executable, run_soda_scan

__all__ = [
    "SodaNotInstalled",
    "SodaResult",
    "resolve_soda_executable",
    "run_soda_scan",
]
