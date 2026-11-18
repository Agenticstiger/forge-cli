# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The supervisor of one local Spark run (``runtime: local``), started detached by forge-cli.

    python -I -S _local_supervisor.py --run-dir <dir> --lock-fd <n> -- spark-submit ...

Standard library only, and never imported: forge-cli runs it by path with ``-I -S``, so
no working directory, ``PYTHON*`` variable or site directory reaches ``sys.path``. It
keeps the run's lock (taken by forge-cli and inherited) and hands it on to spark-submit,
so the lock is held while any of the run's processes lives. It writes ``status.json``
when spark-submit has started and again when it has ended, with its exit code, and passes
SIGTERM, SIGINT and SIGHUP on to spark-submit. A run cancelled before spark-submit
started (``cancel`` in the run directory) starts nothing.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    status_path = os.path.join(args.run_dir, "status.json")
    status = {"supervisorPid": os.getpid()}
    if not command:
        _write(status_path, {**status, "ended": time.time(), "exitCode": 2})
        return 2
    if os.path.exists(os.path.join(args.run_dir, "cancel")):
        _write(status_path, {**status, "ended": time.time(), "exitCode": None})
        return 0
    child = None

    def forward(signum, frame):
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), forward)
    with open(os.path.join(args.run_dir, "driver.log"), "ab", buffering=0) as log:
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(args.lock_fd,),
                cwd=args.run_dir,
            )
        except OSError as exc:
            log.write(f"forge: spark-submit did not start: {exc.strerror or exc}\n".encode())
            _write(status_path, {**status, "ended": time.time(), "exitCode": 127})
            return 1
        status.update(pid=child.pid, started=time.time())
        _write(status_path, status)
        code = child.wait()
    _write(status_path, {**status, "ended": time.time(), "exitCode": code})
    return 0


if __name__ == "__main__":
    sys.exit(main())
