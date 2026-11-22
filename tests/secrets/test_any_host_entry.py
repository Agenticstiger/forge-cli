# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``NAME@*`` sends ``env://NAME`` to any host, as ``env://NAME@*`` and a bare ``NAME`` do."""

from __future__ import annotations

import pytest

from fluid_build.secrets.policy import (
    ALLOWLIST_ENV,
    AcquisitionSecretPolicyError,
    allowlist,
    refuse_unless_allowed,
)

REMOTE = "db.example.com"


@pytest.mark.unit
def test_name_at_star_grants_any_host():
    environ = {ALLOWLIST_ENV: "PG@*"}
    refuse_unless_allowed("env://PG", host=REMOTE, environ=environ)
    refuse_unless_allowed("env://PG", connection={"host": "other.example"}, environ=environ)
    assert allowlist(environ) == [("PG", None)]
    with pytest.raises(AcquisitionSecretPolicyError):
        refuse_unless_allowed("env://OTHER", host=REMOTE, environ=environ)


@pytest.mark.unit
def test_the_fix_names_every_any_host_form():
    with pytest.raises(AcquisitionSecretPolicyError) as info:
        refuse_unless_allowed("env://PG", host=REMOTE, environ={})
    assert f'{ALLOWLIST_ENV}="PG"' in info.value.fix
    assert f'{ALLOWLIST_ENV}="PG@*"' in info.value.fix
