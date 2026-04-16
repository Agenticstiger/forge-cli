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

"""Tests for resilient Forge built-in registration."""

from unittest.mock import ANY, MagicMock, patch


def test_register_builtin_components_continues_after_provider_registration_failure():
    from fluid_build.forge.registration import register_builtin_components

    template_registry = MagicMock()
    provider_registry = MagicMock()
    extension_registry = MagicMock()
    generator_registry = MagicMock()

    provider_registry.list_available.return_value = ["local", "gcp", "snowflake"]

    def _register_provider(name, provider_class, source="builtin"):
        if name == "aws":
            raise RuntimeError("aws unavailable")

    provider_registry.register.side_effect = _register_provider

    with patch(
        "fluid_build.forge.registration.get_template_registry", return_value=template_registry
    ):
        with patch(
            "fluid_build.forge.registration.get_provider_registry", return_value=provider_registry
        ):
            with patch(
                "fluid_build.forge.registration.get_extension_registry",
                return_value=extension_registry,
            ):
                with patch(
                    "fluid_build.forge.registration.get_generator_registry",
                    return_value=generator_registry,
                ):
                    with patch("fluid_build.forge.registration.LOG") as mock_log:
                        mock_log.isEnabledFor.return_value = False
                        register_builtin_components()

    provider_names = [call.args[0] for call in provider_registry.register.call_args_list]
    assert provider_names[:4] == ["local", "gcp", "aws", "snowflake"]
    mock_log.warning.assert_any_call(
        "Skipping built-in provider '%s' during registration: %s",
        "aws",
        ANY,
    )
