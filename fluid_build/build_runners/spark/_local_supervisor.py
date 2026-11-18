# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The supervisor of one local Spark run (``runtime: local``), started detached by forge-cli.

    python -I -S _local_supervisor.py --run-dir <dir> --lock-fd <n> [--grace <s>] -- spark-submit ...

Standard library only, and never imported: forge-cli runs it by path with ``-I -S``, so
no working directory, ``PYTHON*`` variable or site directory reaches ``sys.path``. It
keeps the run's lock (taken by forge-cli and inherited) and hands it on to spark-submit,
so the lock is held while any of the run's processes lives. It writes ``status.json``
when it starts (its pid, which is its process group's id), when spark-submit has started,
and again when it has ended, with its exit code.

A stop is honoured whenever it comes: SIGTERM, SIGINT or SIGHUP, or the ``cancel`` mark
forge-cli writes in the run directory before it signals the run's process group. Before
spark-submit has started, nothing starts. After, the run's processes get SIGTERM, and
spark-submit SIGKILL once ``--grace`` seconds have passed, so a run stops even when the
signals of the ``fluid apply`` that cancelled it came too early or were cut short.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

POLL_S = 0.5


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, path)


def _signal(child, signum, group):
    """``signum`` to the run's processes: the whole group when this supervisor leads it
    (forge-cli starts it in a session of its own), else spark-submit alone."""
    try:
        if group and signum != signal.SIGKILL:
            os.killpg(os.getpgrp(), signum)  # this supervisor's handler only notes it
        else:
            child.send_signal(signum)
    except OSError:
        pass


def _wait(child, cancel_path, stopping, grace):
    """spark-submit's exit code once it has ended. A stop that came at any time gets the
    run SIGTERM, then spark-submit SIGKILL after ``grace`` seconds."""
    group = os.getpgrp() == os.getpid()
    since = None
    while True:
        if since is None and (stopping or os.path.exists(cancel_path)):
            since = time.monotonic()
            _signal(child, signal.SIGTERM, group)
        elif since is not None and time.monotonic() - since > grace:
            _signal(child, signal.SIGKILL, group)
        try:
            return child.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            continue


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--grace", type=float, default=60.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    status_path = os.path.join(args.run_dir, "status.json")
    cancel_path = os.path.join(args.run_dir, "cancel")
    status = {"supervisorPid": os.getpid()}
    stopping = []  # the stop signals that came, before spark-submit existed or after

    def stop(signum, frame):
        stopping.append(signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), stop)
    # First, so a cancel that finds no process group in run.json yet signals this one.
    _write(status_path, status)
    if not command:
        _write(status_path, {**status, "ended": time.time(), "exitCode": 2})
        return 2
    if stopping or os.path.exists(cancel_path):
        _write(status_path, {**status, "ended": time.time(), "exitCode": None})
        return 0
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
        code = _wait(child, cancel_path, stopping, args.grace)
    _write(status_path, {**status, "ended": time.time(), "exitCode": code})
    return 0


if __name__ == "__main__":
    sys.exit(main())
