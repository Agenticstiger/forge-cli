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

"""This host's ambient database credentials, hidden for the length of one attach.

A contract names the host its source lives on. libpq and mysql_scanner then read
credentials from the PROCESS environment for whatever host they are handed, so a
contract naming a host of its own choosing could be authenticated with this
machine's ``PGPASSWORD`` -- a credential the contract was never given, belonging
to a different server. No allowlist over the contract's own ``secretRef`` closes
that, because the credential never passes through ``secretRef`` at all.

``fluid init --discover`` has suppressed these since its JDBC introspection
landed. The acquisition runners had not, so the same contract fields reached the
same client libraries with the environment intact.

This is where the one implementation lives so both use it. It sits under
``credentials`` rather than beside the discover code because ``build_runners``
may not import ``cli`` (the import-linter contract "build_runners must not
depend on cli"), and because hiding a credential is a credentials concern rather
than a discover detail. ``cli/discover/_jdbc_introspect`` re-exports these under
their original private spellings, so the tests and patch points that grew up
there keep working unchanged.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator

# mysql_scanner was seen to send no password from an option file (~/.my.cnf,
# ~/.mylogin.cnf, ~/my.cnf, /etc/my.cnf, /etc/mysql/my.cnf, $MYSQL_HOME/my.cnf),
# with or without that suffix, so clearing it is a precaution: there is no
# option-file fallback for it to close.
HOST_CREDENTIAL_ENV = (
    "PGPASSWORD",
    "PGPASSFILE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGSSLCERT",
    "PGSSLKEY",
    "PGOAUTHTOKEN",
    "PGGSSDELEGATION",
    "MYSQL_PWD",
    "MYSQL_HOST",
    "MYSQL_TCP_PORT",
    "MYSQL_UNIX_PORT",
    "MYSQL_GROUP_SUFFIX",
)

# libpq skips a client certificate whose file does not exist (ENOENT or
# ENOTDIR). Pointing a pinned connection's certificate and key below os.devnull
# means it never presents this host's ~/.postgresql/postgresql.crt to a server
# that asks for one.
NO_CLIENT_CERT = os.path.join(os.devnull, "fluid-no-client-certificate")

# The suppression below mutates the process environment. forge_run's in-process
# worker thread can run alongside forge_from_source, so windows are serialised
# (and re-entrant within one thread) rather than interleaved: two overlapping
# windows could otherwise restore the host credentials while the second attach
# is still inside its own.
HOST_CREDENTIAL_ENV_LOCK = threading.RLock()


@contextlib.contextmanager
def host_credential_env_suppressed() -> Iterator[None]:
    """Hide this host's ambient DB credential env vars for a pinned ATTACH.

    The saved values are put back when the window closes, also when the attach
    raises. Windows opened through this function in different threads are
    serialised by :data:`HOST_CREDENTIAL_ENV_LOCK`, so they never interleave
    their save and restore; within one thread a nested window is safe when it
    closes before the window around it.

    That is all the lock covers. Code that does not go through this function
    is not serialised with it, so there is no guarantee against:

    * another thread reading these variables, which sees them unset while a
      window is open;
    * ``os.environ.copy()`` in another thread, which can race the pop and the
      restore (and raise ``KeyError``);
    * an ``engine._run_sync_stage(env_snapshot=True)`` whose snapshot and
      restore overlap a window, which can delete the variables for good or
      bring them back inside the window;
    * same-thread windows that do not close in LIFO order, where the first to
      close restores the variables inside the other.
    """
    with HOST_CREDENTIAL_ENV_LOCK:
        saved = {k: os.environ.pop(k) for k in HOST_CREDENTIAL_ENV if k in os.environ}
        try:
            yield
        finally:
            os.environ.update(saved)
