# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
from typing import Iterator, Tuple

# mysql_scanner was seen to send no password from an option file (~/.my.cnf,
# ~/.mylogin.cnf, ~/my.cnf, /etc/my.cnf, /etc/mysql/my.cnf, $MYSQL_HOME/my.cnf),
# with or without that suffix, so clearing it is a precaution: there is no
# option-file fallback for it to close.
#
# The MYSQL_* and MARIADB_* names after MYSQL_GROUP_SUFFIX are the other
# variables the mysql client libraries read: the user and database (the scanner
# reads MYSQL_USER and MYSQL_DATABASE; ``build_mysql_dsn`` names the user and the
# database only when the connection has them), where option files and client
# plugins are looked up (MYSQL_HOME, MARIADB_HOME, LIBMYSQL_PLUGINS,
# LIBMYSQL_PLUGIN_DIR, MARIADB_PLUGIN_DIR), a debug trace, and MariaDB
# Connector/C's switch that turns off peer verification. USER, LOGNAME and LOGIN
# are the user name Connector/C falls back to when the DSN names none. libpq's
# ``require_auth`` is not here: the libpq in duckdb 1.5.1's postgres extension
# predates it (it needs libpq 16).
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
    "MYSQL_USER",
    "MYSQL_DATABASE",
    "MARIADB_UNIX_PORT",
    "MYSQL_HOME",
    "MARIADB_HOME",
    "LIBMYSQL_PLUGINS",
    "LIBMYSQL_PLUGIN_DIR",
    "MARIADB_PLUGIN_DIR",
    "MYSQL_DEBUG",
    "MARIADB_TLS_DISABLE_PEER_VERIFICATION",
    "USER",
    "LOGNAME",
    "LOGIN",
)

# libpq's variables that choose WHERE it connects (PGHOSTADDR wins over a host
# the DSN names). The acquisition ATTACH hides them as well: its DSN is the
# contract's, the secret policy judged the host the contract names, and libpq
# takes a host from PGHOST only when the DSN has none, so a connection that names
# no host (or only a decoy uri, which this DSN ignores) would otherwise send the
# password to whatever PGHOST a project .env set. With them hidden, a DSN without
# a host reaches only libpq's default local socket. `fluid init --discover` keeps
# them: there the operator, not a contract, names the server.
ATTACH_HOST_ENV = ("PGHOST", "PGHOSTADDR")

# libpq skips a client certificate whose file does not exist (ENOENT or
# ENOTDIR). Pointing a pinned connection's certificate and key below os.devnull
# means it never presents this host's ~/.postgresql/postgresql.crt to a server
# that asks for one.
NO_CLIENT_CERT = os.path.join(os.devnull, "fluid-no-client-certificate")

# The suppression below mutates the process environment. a server thread can run alongside
# another attach, so windows are serialised
# (and re-entrant within one thread) rather than interleaved: two overlapping
# windows could otherwise restore the host credentials while the second attach
# is still inside its own.
HOST_CREDENTIAL_ENV_LOCK = threading.RLock()


@contextlib.contextmanager
def host_credential_env_suppressed(also: Tuple[str, ...] = ()) -> Iterator[None]:
    """Hide this host's ambient DB credential env vars for a pinned ATTACH.

    ``also`` names more variables to hide in the same window: the acquisition
    ATTACH passes :data:`ATTACH_HOST_ENV`.

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
    names = HOST_CREDENTIAL_ENV + tuple(also)
    with HOST_CREDENTIAL_ENV_LOCK:
        saved = {k: os.environ.pop(k) for k in names if k in os.environ}
        try:
            yield
        finally:
            os.environ.update(saved)
