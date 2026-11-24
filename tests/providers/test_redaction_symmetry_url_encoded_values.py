# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A credential the process holds is masked in its URL-encoded spellings too, in every layer.

A password with a character a URL escapes (``@``, a space, ``/``) is printed escaped when a log
line or an error shows a URL that holds it: ``quote`` in a path or a userinfo, ``quote_plus`` or
Java's ``URLEncoder`` in a query. The exact-value layer (``redact_known_values``) is shared by the
global redactor, the Snowflake twin and the local provider's logger, so each is checked, and so
is a real logger with the redacting filter installed.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterator
from urllib.parse import quote, quote_plus

import pytest

from fluid_build.observability.secret_redactor import (
    forget_known_secrets,
    install_secret_redacting_filter,
    redact_secret_text,
    register_secret,
)
from fluid_build.providers.local.util.logging import redact_string as redact_local
from fluid_build.providers.snowflake.util.logging import redact_string as redact_snowflake

pytestmark = [pytest.mark.unit]

# Built from parts, so no credential-shaped literal sits in the source.
_HELD = "".join(("Zk4q@", "R9 tW/", "vL2x~*", "Pn8m"))
_SPELLINGS = {
    "quote": quote(_HELD),
    "quote, slash escaped": quote(_HELD, safe=""),
    "quote_plus": quote_plus(_HELD),
    "URLEncoder": quote_plus(_HELD, safe="*").replace("~", "%7E"),
}
_LAYERS: dict[str, Callable[[str], str]] = {
    "global": redact_secret_text,
    "snowflake": redact_snowflake,
    "local": redact_local,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """The exact-value registry is process-wide; keep every test here hermetic."""
    forget_known_secrets()
    yield
    forget_known_secrets()


def test_each_spelling_differs_from_the_value_and_the_others() -> None:
    assert len(set(_SPELLINGS.values())) == len(_SPELLINGS)
    assert _HELD not in _SPELLINGS.values()


@pytest.mark.parametrize("layer", sorted(_LAYERS))
@pytest.mark.parametrize("spelling", sorted(_SPELLINGS))
def test_a_registered_value_is_masked_in_its_url_spelling(layer: str, spelling: str) -> None:
    register_secret(_HELD, credential_key=True)
    text = f"POST https://sink.example/hooks/{_SPELLINGS[spelling]}/events refused"

    shown = _LAYERS[layer](text)

    assert _SPELLINGS[spelling] not in shown
    assert shown.startswith("POST https://sink.example/hooks/") and shown.endswith("refused")


@pytest.mark.parametrize("spelling", sorted(_SPELLINGS))
def test_a_value_passed_for_one_call_is_masked_in_its_url_spelling(spelling: str) -> None:
    """The way a runner masks what it handed a server without registering it."""
    text = f"retrying {_SPELLINGS[spelling]} later"

    assert redact_secret_text(text, extra_secrets=[_HELD]) == "retrying ***REDACTED*** later"


def test_a_short_value_is_masked_in_its_url_spelling_only_as_a_whole_word() -> None:
    short = "".join(("p w", "@"))
    register_secret(short, credential_key=True)

    assert redact_secret_text("x p%20w%40 y") == "x ***REDACTED*** y"
    assert redact_secret_text("xp%20w%40y") == "xp%20w%40y"


def test_a_log_line_with_the_url_spelling_never_reaches_the_stream() -> None:
    register_secret(_HELD, credential_key=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.Logger("fluid.test.redaction.url_spellings", level=logging.DEBUG)
    logger.addHandler(handler)
    install_secret_redacting_filter(logger)

    for spelling in _SPELLINGS.values():
        logger.warning("connecting to jdbc:postgresql://db.example/app?pw=%s", spelling)
        logger.warning("sink https://sink.example/" + spelling + "/x refused")

    written = stream.getvalue()
    assert not any(spelling in written for spelling in _SPELLINGS.values())
    assert written.count("jdbc:postgresql://db.example/app") == len(_SPELLINGS)


def test_other_text_with_a_percent_or_a_plus_is_left_alone() -> None:
    register_secret(_HELD, credential_key=True)
    text = "100% done, 3+4 rows, /a%20b?q=x+y"

    assert redact_secret_text(text) == text
