# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The secret shapes of an alert route's connection, masked by BOTH redaction layers.

An alert route names an Airflow connection and never carries the secret, but the secret can
still reach a forge log: pasted into the wrong field, quoted by a library's exception, or in
an operator's own debugging. The global ``SecretRedactingFilter`` and the Snowflake
provider's ``redact_string`` mask each shape (CLAUDE.md: extend both).
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.observability.secret_redactor import (
    install_secret_redacting_filter,
    redact_secret_text,
    redact_value,
)
from fluid_build.providers.snowflake.util.logging import redact_dict, redact_string

pytestmark = pytest.mark.unit

# Built from parts, so no file holds a whole key-shaped literal.
_KEY = "0f8fad5b" + "d9cb469f" + "a1657086" + "7728950e"
_UUID = "0f8fad5b-d9cb-469f-" + "a165-70867728950e"

SECRETS = {
    "zenduty generic integration": (
        f"https://events.zenduty.com/integration/ab12cd34/generic/{_UUID}/",
        _UUID,
    ),
    "zenduty events api": (f"https://www.zenduty.com/api/events/{_KEY}/", _KEY),
    "pagerduty x-ere": (f"https://events.pagerduty.com/x-ere/R{_KEY}", _KEY),
    "pagerduty eu integration": (
        f"https://events.eu.pagerduty.com/integration/{_KEY}/enqueue",
        _KEY,
    ),
    "opsgenie header": (f"Authorization: GenieKey {_UUID}", _UUID),
    "opsgenie bare header value": (f"headers GenieKey {_UUID} sent", _UUID),
    "pagerduty routing key json": (f'{{"routing_key": "{_KEY}", "event_action": "trigger"}}', _KEY),
    "pagerduty integration key": (f"integration_key={_KEY}", _KEY),
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_both_layers_mask(name: str) -> None:
    text, secret = SECRETS[name]
    line = f"alert route connection: {text} (end)"
    assert secret not in redact_secret_text(line), f"{name} leaked through the global layer"
    assert secret not in redact_string(line), f"{name} leaked through the Snowflake layer"


def test_both_layers_mask_the_keys_by_name() -> None:
    payload = {"routing_key": _KEY, "integration_key": _KEY, "summary": "kept"}
    for redacted in (redact_value(payload), redact_dict(payload)):
        assert _KEY not in str(redacted) and redacted["summary"] == "kept"


def test_the_logging_filter_masks_them(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("test.alerts.redaction")
    install_secret_redacting_filter(log)
    with caplog.at_level(logging.INFO, logger=log.name):
        for text, _secret in SECRETS.values():
            log.info("sending %s", text)
    rendered = " ".join(r.getMessage() for r in caplog.records)
    assert _KEY not in rendered and _UUID not in rendered


def test_a_connection_id_is_not_masked() -> None:
    """A route's own text stays readable: only the secret shapes are masked."""
    line = "route oncall (webhook, through Airflow connection zenduty_oncall)"
    assert redact_secret_text(line) == line and redact_string(line) == line
