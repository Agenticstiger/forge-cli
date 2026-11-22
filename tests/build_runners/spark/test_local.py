# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""runtime: local: spark-submit on this machine, driven here by a stub spark-submit.

The stub is a real executable the real supervisor starts in its own process group: it
checks that the runner and the job config it is handed exist where the layout says, then
ends at once for a batch job and runs until SIGTERM for a stream. So the run directories,
the lock, the exit codes and the signals are the ones a real local run goes through.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fluid_build.build_runners.spark import _local
from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import (
    CANCELLED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    SparkRunError,
    adapter_for,
)
from fluid_build.build_runners.spark._desired import LOCAL_ROOT_TOKEN, config_digest, desired_runs
from fluid_build.build_runners.spark._local import (
    LOCAL_ROOT_ENV,
    PROPERTIES_FILE_ENV,
    SUBMIT_EXECUTABLE_ENV,
    LocalHost,
    LocalRuns,
    merge_conf,
    parse_properties,
    start_run,
    submit_argv,
    submitter,
)
from fluid_build.spark import runner_source
from tests.spark.conftest import CONTRACT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="local runs are POSIX only")

STUB = f"""#!{sys.executable}
import json, os, signal, sys, time
argv = sys.argv[1:]
out = os.environ.get("STUB_OUT")
if out:
    with open(os.path.join(out, "argv-%d.json" % os.getpid()), "w") as handle:
        json.dump(dict(argv=argv, safepath=os.environ.get("PYTHONSAFEPATH")), handle)
config = argv[argv.index("--config") + 1]
runner = argv[argv.index("--config") - 1]
assert os.path.isfile(config) and os.path.isfile(runner), (config, runner)
if os.environ.get("STUB_RUN_RUNNER"):
    # As Spark's PythonRunner does: the runner as a script. Without PYTHONSAFEPATH, so only
    # where the runner sits decides what its imports find.
    import subprocess
    env = dict(os.environ)
    env.pop("PYTHONSAFEPATH", None)
    sys.exit(subprocess.run([sys.executable, runner, "--config", config], env=env).returncode)
mode = json.load(open(config))["mode"]
print("stub: started", mode, flush=True)
if mode == "batch":
    print("stub: Py4JJavaError: the batch failed" if os.environ.get("STUB_EXIT", "0") != "0"
          else "stub: done", flush=True)
    sys.exit(int(os.environ.get("STUB_EXIT", "0")))
if os.environ.get("STUB_STREAM_EXIT"):
    print("stub: the stream failed", flush=True)
    sys.exit(int(os.environ["STUB_STREAM_EXIT"]))
if os.environ.get("STUB_GRANDCHILD"):
    # A process of the run that ignores SIGTERM and holds every inherited descriptor,
    # the run's lock among them.
    import subprocess
    subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"],
        close_fds=False,
    )
stop = []
if os.environ.get("STUB_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
# A job config holding STUB_FAIL_CONFIG fails on its first batch, as a bad release would.
fail = os.environ.get("STUB_FAIL_CONFIG")
if fail and fail in open(config).read():
    print("stub: the stream failed on its first batch", flush=True)
    sys.exit(1)
# As the runner does: a stop request beside the job config ends the run (STUB_IGNORE_STOP: a
# run that never answers one).
request = None
if not os.environ.get("STUB_IGNORE_STOP") and "--fluid-stop-key" in argv:
    key = argv[argv.index("--fluid-stop-key") + 1]
    request = os.path.join(os.path.dirname(config), "stop", key)
while not stop:
    if request and os.path.isfile(request):
        print("stub: stopped between batches", flush=True)
        sys.exit(0)
    time.sleep(0.05)
print("stub: stopped", flush=True)
sys.exit(143)
"""


def local_contract(**stream):
    """The compiler tests' stream, on this machine: a Lakekeeper table in bucket ``lake``."""
    contract = copy.deepcopy(CONTRACT)
    build = contract["builds"][0]
    cfg = build["properties"]["parameters"]["spark"]
    cfg["runtime"] = "local"
    cfg["source"] = {"bootstrapServers": "localhost:9092"}
    cfg.pop("maskKeySecretArn")
    for column in build["properties"]["mapping"]["columns"]:
        column.pop("maskAtIngest", None)
    build["properties"]["stream"].update(stream)
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "iceberg",
        "location": {
            "catalog": "lakekeeper",
            "database": "commerce",
            "table": "order_lines",
            "bucket": "lake",
        },
    }
    return contract


def batch_contract():
    contract = local_contract()
    build = contract["builds"][0]
    build["properties"] = {
        "sql": "SELECT order_id, customer_id, email_hash, sku, qty, event_time FROM raw.o",
        "parameters": {"spark": {"runtime": "local", "mode": "batch"}},
    }
    return contract


@pytest.fixture(autouse=True)
def listed_namespace(monkeypatch):
    """The operator lets local runs write the examples' namespace."""
    from fluid_build.spark.compiler import LOCAL_NAMESPACE_ALLOWLIST_ENV

    monkeypatch.setenv(LOCAL_NAMESPACE_ALLOWLIST_ENV, "fluid/commerce")


@pytest.fixture
def stub(tmp_path_factory):
    # Outside every test's project (tmp_path): forge refuses a spark-submit inside it.
    path = tmp_path_factory.mktemp("bin") / "spark-submit"
    path.write_text(STUB, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path, stub, monkeypatch):
    from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV

    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, CONTRACT["id"])
    out = tmp_path / "argv"
    out.mkdir()
    env = {**os.environ, SUBMIT_EXECUTABLE_ENV: str(stub), "STUB_OUT": str(out)}
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return LocalHost(workspace, env)


def with_env(host, **env):
    return LocalHost(host.workspace, {**host.env, **env})


def the_run(contract):
    (run,) = desired_runs(contract)
    return run


def wait_for(read, done, timeout=15.0):
    deadline = time.monotonic() + timeout
    info = read()
    while not done(info) and time.monotonic() < deadline:
        time.sleep(0.05)
        info = read()
    return info


@pytest.fixture
def started():
    """Run ids to stop at the end, whatever the test did."""
    runs = []
    yield runs
    for runs_of, run_id in runs:
        try:
            runs_of.cancel(run_id, grace=2.0)
        except SparkRunError:
            continue  # a run this test already removed or never started: nothing to stop


# --- the run as it is planned ----------------------------------------------------------


def test_a_local_run_has_no_role_region_or_account_and_a_root_token():
    run = the_run(local_contract())
    assert (run.runtime, run.region, run.role_arn, run.target) == ("local", "", "", {})
    assert run.local and run.needs_local_root and not run.needs_account
    assert run.job_uri == (
        f"{LOCAL_ROOT_TOKEN}/_fluid/spark/commerce.orders-stream/ingest-orders/job.json"
    )
    action = run.plan_action(1)
    assert action["provider"] == "local" and action["payload"]["executionRoleArn"] is None


def test_the_root_the_operator_names_never_changes_the_config_hash(tmp_path):
    run = the_run(local_contract())
    bound = run.bind(local_root=str(tmp_path))
    assert bound.config_hash == run.config_hash
    assert bound.job_uri == f"{tmp_path}/_fluid/spark/commerce.orders-stream/ingest-orders/job.json"


def test_a_local_run_ignores_the_aws_runtimes_keys():
    contract = local_contract()
    cfg = contract["builds"][0]["properties"]["parameters"]["spark"]
    plain = the_run(contract)
    cfg.update(
        clusterId="j-ABC123",
        applicationId="00abc",
        virtualClusterId="vc1",
        releaseLabel="emr-7.5.0",
        glueVersion="5.0",
        executionRoleArn="arn:aws:iam::123456789012:role/r",
    )
    assert the_run(contract).config_hash == plain.config_hash


def test_adapter_for_a_local_run_is_local_and_needs_no_aws(tmp_path):
    adapter = adapter_for(the_run(local_contract()), local_host=LocalHost(tmp_path))
    assert adapter.runtime == "local"
    assert adapter.runs() == []
    assert (
        adapter.where()
        == "this machine (.fluid/commerce.orders-stream/runtime/spark/ingest-orders)"
    )


# --- the operator's settings ----------------------------------------------------------


def test_spark_submit_is_found_on_path_by_default(tmp_path, stub):
    sub = submitter(LocalHost(tmp_path, {"PATH": str(stub.parent)}))
    assert sub.executable == str(stub)
    assert sub.root == str((tmp_path / ".fluid" / "spark-artifacts").resolve())


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PATH": "/nonexistent"}, "spark-submit is not on PATH"),
        ({SUBMIT_EXECUTABLE_ENV: "bin/spark-submit"}, "not a relative path"),
        ({SUBMIT_EXECUTABLE_ENV: "/nonexistent/spark-submit"}, "not an executable file"),
        ({SUBMIT_EXECUTABLE_ENV: "SUBMIT", PROPERTIES_FILE_ENV: "spark.conf"}, "absolute path"),
        ({SUBMIT_EXECUTABLE_ENV: "SUBMIT", PROPERTIES_FILE_ENV: "/nonexistent.conf"}, "not a file"),
        ({SUBMIT_EXECUTABLE_ENV: "SUBMIT", LOCAL_ROOT_ENV: "s3://Lake/x"}, "s3://<bucket>"),
        ({SUBMIT_EXECUTABLE_ENV: "SUBMIT", LOCAL_ROOT_ENV: "s3://lake/a/../b"}, "s3://<bucket>"),
        ({SUBMIT_EXECUTABLE_ENV: "SUBMIT", LOCAL_ROOT_ENV: "relative/dir"}, "absolute path"),
    ],
)
def test_an_unusable_operator_setting_is_refused_naming_it(tmp_path, stub, env, message):
    env = {k: str(stub) if v == "SUBMIT" else v for k, v in env.items()}
    with pytest.raises(SparkRunError, match=message):
        submitter(LocalHost(tmp_path, env))


def test_a_properties_file_error_names_the_line_never_the_value(tmp_path, stub):
    conf = tmp_path / "spark.conf"
    conf.write_text("spark.a 1\nspark.sql.catalog.fluid.credential id:s3cr3t\\uZZZZ\n")
    env = {SUBMIT_EXECUTABLE_ENV: str(stub), PROPERTIES_FILE_ENV: str(conf)}
    with pytest.raises(SparkRunError) as caught:
        submitter(LocalHost(tmp_path, env))
    assert "line 2" in str(caught.value) and "s3cr3t" not in str(caught.value)


def test_properties_are_read_as_java_and_spark_read_them():
    text = (
        "# a comment\n"
        "! another\n"
        "spark.master local[2]\n"
        "spark.a=1\n"
        "spark.b : two words  \n"
        "spark.c   \\\n"
        "    continued,\\\n"
        "    again\n"
        "spark.d \\u00e9t\\u00e9\n"
        "spark.e=\\\\path\\:x\n"
        "spark.f\n"
        "spark.a=later\n"
        "   \n"
    )
    assert parse_properties(text) == {
        "spark.master": "local[2]",
        "spark.a": "later",
        "spark.b": "two words",
        "spark.c": "continued,again",
        "spark.d": "été",
        "spark.e": "\\path:x",
        "spark.f": "",
    }


def test_merge_conf_unions_lists_and_lets_the_file_name_the_iceberg_runtime():
    forge = {
        "spark.jars": "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar",
        "spark.jars.packages": "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1",
        "spark.jars.repositories": "https://packages.confluent.io/maven/",
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.shuffle.partitions": "8",
    }
    operator = {
        "spark.jars": "",
        "spark.jars.packages": "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.9.2,"
        "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.7",
        "spark.jars.repositories": "https://mirror.example/maven/",
        "spark.sql.extensions": "com.example.Ext",
    }
    assert merge_conf(forge, operator) == {
        "spark.jars.packages": "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.9.2,"
        "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.7",
        "spark.sql.extensions": "com.example.Ext,"
        "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.shuffle.partitions": "8",
    }
    assert merge_conf(forge, {}) == forge


def argv_of(host):
    (path,) = Path(host.env["STUB_OUT"]).glob("argv-*.json")
    return json.loads(path.read_text())


def test_the_argv_is_client_mode_with_the_file_and_never_a_shell(tmp_path, stub):
    conf = tmp_path / "spark.conf"
    conf.write_text("spark.sql.catalog.fluid.credential id:secret\n")
    sub = submitter(
        LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(stub), PROPERTIES_FILE_ENV: str(conf)})
    )
    run = the_run(local_contract()).bind(local_root=sub.root)
    argv = submit_argv(sub, run)
    assert argv[:7] == [
        str(stub),
        "--name",
        run.run_name,
        "--deploy-mode",
        "client",
        "--properties-file",
        str(conf),
    ]
    tail = [run.runner_uri, *run.entry_args()]
    assert argv[-len(tail) :] == tail
    assert not any("id:secret" in a for a in argv)
    assert f"spark.redaction.regex={_local.REDACTION_REGEX}" in argv


def test_spark_s_redaction_pattern_names_credentials_unless_the_file_sets_its_own(tmp_path, stub):
    # Spark 3.5's default (core/.../internal/config/package.scala); 3.4 lacks credential.
    assert _local.REDACTION_REGEX == "(?i)secret|password|token|access[.]?key|credential"
    conf = tmp_path / "spark.conf"
    conf.write_text("spark.redaction.regex (?i)secret|credential|apikey\n")
    sub = submitter(
        LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(stub), PROPERTIES_FILE_ENV: str(conf)})
    )
    argv = submit_argv(sub, the_run(local_contract()).bind(local_root=sub.root))
    assert not any(a.startswith("spark.redaction.regex=") for a in argv)


def test_a_spark_submit_inside_the_project_is_refused(tmp_path):
    inside = tmp_path / "tools" / "spark-submit"
    inside.parent.mkdir()
    inside.write_text(STUB, encoding="utf-8")
    inside.chmod(0o755)
    for env in ({SUBMIT_EXECUTABLE_ENV: str(inside)}, {"PATH": str(inside.parent)}):
        with pytest.raises(SparkRunError, match="inside the project"):
            submitter(LocalHost(tmp_path, env))


@pytest.mark.parametrize("named", ["absolute", "path"])
def test_a_link_in_the_project_to_a_program_outside_it_is_refused(tmp_path, stub, named):
    link = tmp_path / "lnk" / "spark-submit"
    link.parent.mkdir()
    link.symlink_to(stub)  # the program is outside; the link that names it is the project's
    env = {SUBMIT_EXECUTABLE_ENV: str(link)} if named == "absolute" else {"PATH": str(link.parent)}
    with pytest.raises(SparkRunError, match="inside the project"):
        submitter(LocalHost(tmp_path, env))
    linked_dir = tmp_path / "bin"
    linked_dir.symlink_to(stub.parent)  # a directory of the project's that leads out
    with pytest.raises(SparkRunError, match="inside the project"):
        submitter(LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(linked_dir / stub.name)}))


def test_a_spark_submit_linked_from_outside_into_the_project_is_refused(tmp_path, stub):
    inside = tmp_path / "spark-submit"
    inside.write_text(STUB, encoding="utf-8")
    inside.chmod(0o755)
    link = stub.parent / "linked-spark-submit"
    link.symlink_to(inside)
    with pytest.raises(SparkRunError, match="inside the project"):
        submitter(LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(link)}))


def test_a_spark_submit_found_through_a_relative_path_entry_is_refused(tmp_path, stub):
    os.symlink(stub.parent, tmp_path / "rel")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(SparkRunError, match="relative PATH entry"):
            submitter(LocalHost(tmp_path / "ws", {"PATH": "rel"}))
    finally:
        os.chdir(cwd)


def test_the_run_environment_keeps_only_names_a_shell_could_set():
    env = {
        "PATH": "/usr/bin",
        "AWS_REGION": "eu-west-1",
        "_x1": "kept",
        "BASH_FUNC_spark-submit%%": "() { touch /tmp/pwned; }",
        "BASH_FUNC_x": "() { :; }",
        "BASH_ENV": "/tmp/evil.sh",
        "ENV": "/tmp/evil.sh",
        "SHELLOPTS": "xtrace",
        "BASHOPTS": "extdebug",
        "PS4": "$(touch /tmp/pwned)",
        "BASH_XTRACEFD": "1",
        "1BAD": "x",
        "A-B": "x",
        "A=B": "x",
    }
    assert _local.run_env(env) == {"PATH": "/usr/bin", "AWS_REGION": "eu-west-1", "_x1": "kept"}


# --- artifacts -------------------------------------------------------------------------


def test_publish_writes_the_aws_layout_under_a_directory_root(tmp_path, stub):
    sub = submitter(LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(stub)}))
    run = the_run(local_contract()).bind(local_root=sub.root)
    _local.publish(sub, run, runner_source())
    base = Path(sub.root) / "_fluid/spark/commerce.orders-stream/ingest-orders"
    assert (base / "job.json").read_text(encoding="utf-8") == run.job_text
    runner = base / "runner" / run.runner_sha256[:16] / "forge_spark_runner.py"
    assert runner.read_text(encoding="utf-8") == runner_source()
    _local.publish(sub, run, runner_source())  # again: unchanged
    with pytest.raises(SparkRunError, match="runner changed"):
        _local.publish(sub, run, runner_source() + "# edited\n")


def test_publish_to_s3_uploads_both_objects(tmp_path, stub):
    boto3 = pytest.importorskip("boto3")
    mock_aws = pytest.importorskip("moto").mock_aws

    env = {SUBMIT_EXECUTABLE_ENV: str(stub), LOCAL_ROOT_ENV: "s3://lake/lab"}
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lake")
        sub = submitter(LocalHost(tmp_path, env))
        run = the_run(local_contract()).bind(local_root=sub.root)
        assert (
            run.job_uri
            == "s3://lake/lab/_fluid/spark/commerce.orders-stream/ingest-orders/job.json"
        )
        _local.publish(sub, run, runner_source())
        s3 = boto3.client("s3", region_name="us-east-1")
        key = run.job_uri.removeprefix("s3://lake/")
        assert s3.get_object(Bucket="lake", Key=key)["Body"].read().decode() == run.job_text
        runner_key = run.runner_uri.removeprefix("s3://lake/")
        assert s3.head_object(Bucket="lake", Key=runner_key)["ContentLength"] > 0


def test_an_s3_compatible_endpoint_gets_path_style_requests(tmp_path, stub, monkeypatch):
    boto3 = pytest.importorskip("boto3")

    seen = {}

    class Client:
        def put_object(self, **kwargs):
            seen.setdefault("keys", []).append(kwargs["Key"])

    def client(service, **kwargs):
        seen.update(kwargs)
        return Client()

    monkeypatch.setattr(boto3, "client", client)
    env = {
        SUBMIT_EXECUTABLE_ENV: str(stub),
        LOCAL_ROOT_ENV: "s3://lake",
        "AWS_ENDPOINT_URL_S3": "http://seaweedfs:8333",
    }
    sub = submitter(LocalHost(tmp_path, env))
    _local.publish(sub, the_run(local_contract()).bind(local_root=sub.root), runner_source())
    assert seen["config"].s3 == {"addressing_style": "path"}
    assert seen["region_name"] == "us-east-1" and len(seen["keys"]) == 2
    # An endpoint that is down fails the apply in seconds.
    assert (seen["config"].connect_timeout, seen["config"].read_timeout) == (10, 60)
    assert seen["config"].retries == {"mode": "standard", "max_attempts": 3}


def test_an_s3_client_that_cannot_be_made_is_a_named_error(tmp_path, stub, monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def client(service, **kwargs):
        raise ValueError("Invalid endpoint: not-a-url")

    monkeypatch.setattr(boto3, "client", client)
    sub = submitter(
        LocalHost(tmp_path, {SUBMIT_EXECUTABLE_ENV: str(stub), LOCAL_ROOT_ENV: "s3://lake"})
    )
    run = the_run(local_contract()).bind(local_root=sub.root)
    with pytest.raises(SparkRunError, match="no S3 client could be made for it: ValueError"):
        _local.publish(sub, run, runner_source())


# --- runs ------------------------------------------------------------------------------


def test_spark_submit_is_handed_the_sha256_of_the_config_it_reads(host):
    run = the_run(batch_contract())
    out = rc.run_once(run, local_host=host, timeout=30, say=lambda _: 0)
    assert out.state == SUCCEEDED
    argv = argv_of(host)["argv"]
    config = Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8")
    assert argv[-2:] == ["--fluid-job-sha256", config_digest(config)]


def test_a_batch_run_goes_to_succeeded(host):
    run = the_run(batch_contract())
    said = []
    out = rc.run_once(run, local_host=host, timeout=30, say=said.append)
    assert (out.action, out.state, out.error) == ("succeeded", SUCCEEDED, False), out.message
    assert said[0].startswith(f"Spark ingest-orders: started run {out.run_id} on this machine")


def test_a_failed_batch_run_is_failed_with_its_log(host):
    run = the_run(batch_contract())
    out = rc.run_once(run, local_host=with_env(host, STUB_EXIT="3"), timeout=30, say=lambda _: 0)
    assert (out.action, out.state, out.error) == ("failed", FAILED, True)
    assert "ended FAILED (exit 3)" in out.message
    assert "driver.log" in out.message and "the batch failed" in out.message


def test_run_build_runs_a_local_batch_build_through_the_build_phase(host):
    contract = batch_contract()
    code = rc.run_build(
        contract, contract["builds"][0], dry_run=False, say=lambda _: 0, local_host=host
    )
    assert code == 0


def test_a_stream_is_started_left_alone_restarted_and_stopped(host, started):
    first = the_run(local_contract())
    (out,) = rc.reconcile([first], local_host=host)
    assert (out.action, out.state, out.error) == ("started", RUNNING, False), out.message
    runs = LocalRuns(host, first.product, first.build)
    started.append((runs, out.run_id))

    (again,) = rc.reconcile([first], local_host=host)
    assert (again.action, again.run_id, again.cancelled) == ("unchanged", out.run_id, [])
    log_path = runs.dir / "runs" / out.run_id / "driver.log"
    wait_for(lambda: log_path.read_text(encoding="utf-8"), lambda t: "stub: started" in t)

    changed = the_run(local_contract(trigger={"processingTime": "PT2M"}))
    assert changed.config_hash != first.config_hash
    assert changed.checkpoint == first.checkpoint
    (restart,) = rc.reconcile([changed], local_host=host)
    assert (restart.action, restart.stopped, restart.cancelled, restart.error) == (
        "restarted",
        [out.run_id],
        [],
        False,
    ), restart.message
    started.append((runs, restart.run_id))
    assert runs.get(out.run_id).state == SUCCEEDED  # it ended cleanly when asked
    assert runs.get(restart.run_id).state == RUNNING
    assert "stub: stopped between batches" in log_path.read_text(encoding="utf-8")

    outcomes = rc.reconcile([], local_host=host, stop_removed_of=(first.product, set()))
    assert [(o.build, o.action, o.cancelled, o.error) for o in outcomes] == [
        ("ingest-orders", "stopped", [restart.run_id], False)
    ]
    assert f"stopped run {restart.run_id} CANCELLED on this machine" in outcomes[0].line()
    assert runs.get(restart.run_id).state == CANCELLED
    assert rc.reconcile([], local_host=host, stop_removed_of=(first.product, set())) == []


def test_a_stream_that_does_not_answer_its_stop_request_is_cancelled_after_the_grace(host, started):
    quiet = with_env(host, STUB_IGNORE_STOP="1")
    first = the_run(local_contract())
    (out,) = rc.reconcile([first], local_host=quiet)
    runs = LocalRuns(host, first.product, first.build)
    started.append((runs, out.run_id))
    changed = the_run(local_contract(trigger={"processingTime": "PT2M"}))
    (restart,) = rc.reconcile(
        [changed],
        local_host=quiet,
        env={rc.WAIT_ENV: "30", rc.STOP_GRACE_ENV: "1", rc.PROBATION_ENV: "0"},
    )
    started.append((runs, restart.run_id))
    assert (restart.action, restart.stopped, restart.cancelled) == (
        "restarted",
        [],
        [out.run_id],
    ), restart.message
    assert "did not stop between batches within 1 s" in restart.message
    assert runs.get(out.run_id).state == CANCELLED
    assert not list(runs.host.workspace.joinpath(".fluid").rglob("stop/*"))  # withdrawn


def test_a_build_kept_locally_is_not_stopped(host, started):
    run = the_run(local_contract())
    (out,) = rc.reconcile([run], local_host=host)
    started.append((LocalRuns(host, run.product, run.build), out.run_id))
    assert rc.stop_removed(run.product, {run.build}, host) == []
    (planned,) = rc.stop_removed(run.product, set(), host, dry_run=True)
    assert planned.action == "planned" and out.run_id in planned.message
    assert LocalRuns(host, run.product, run.build).get(out.run_id).state == RUNNING


def test_a_second_start_of_the_same_config_adopts_the_first(host, started):
    run = the_run(local_contract())
    first = start_run(host, run, "a", runner_text=runner_source())
    started.append((LocalRuns(host, run.product, run.build), first))
    assert start_run(host, run, "b", runner_text=runner_source()) == first


def test_a_run_whose_supervisor_dies_stays_running_until_spark_submit_ends(host, started):
    run = the_run(local_contract())
    run_id = start_run(host, run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    status = json.loads((runs.dir / "runs" / run_id / "status.json").read_text())
    os.kill(status["supervisorPid"], signal.SIGKILL)
    time.sleep(0.3)
    assert runs.get(run_id).state == RUNNING  # spark-submit still holds the lock
    os.kill(status["pid"], signal.SIGKILL)
    info = wait_for(lambda: runs.get(run_id), lambda i: i.state == FAILED)
    assert (info.state, info.raw_state) == (FAILED, "LOST")
    assert "without recording how they ended" in info.details


def test_a_run_that_ignores_sigterm_is_killed_after_the_grace(host, started):
    run = the_run(local_contract())
    run_id = start_run(with_env(host, STUB_IGNORE_TERM="1"), run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    begun = time.monotonic()
    runs.cancel(run_id, grace=0.5)
    assert time.monotonic() - begun < 10
    assert runs.get(run_id).state == CANCELLED


def test_a_run_cancelled_before_it_started_starts_nothing(tmp_path):
    where = tmp_path / "run"
    where.mkdir()
    (where / "cancel").write_text("1\n")
    lock = os.open(where / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(_local.SUPERVISOR),
                "--run-dir",
                str(where),
                "--lock-fd",
                str(lock),
                "--",
                "/nonexistent/spark-submit",
            ],
            pass_fds=(lock,),
            check=True,
            timeout=30,
        )
    finally:
        os.close(lock)
    status = json.loads((where / "status.json").read_text())
    assert "ended" in status and status["exitCode"] is None and "pid" not in status


def test_a_cancel_mark_alone_stops_a_run(host, started):
    """A cancel whose signals never reached the run (it came before the run's process group
    was recorded, or the apply that sent it was stopped): the supervisor honours the mark."""
    run = the_run(local_contract())
    run_id = start_run(host, run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    (runs.dir / "runs" / run_id / "cancel").write_text("1\n")
    assert wait_for(lambda: runs.get(run_id), lambda i: i.state == CANCELLED).state == CANCELLED


def test_a_marked_run_that_ignores_sigterm_is_killed_after_the_supervisor_s_grace(
    host, started, monkeypatch
):
    monkeypatch.setattr(_local, "STOP_GRACE_S", 0.5)
    run = the_run(local_contract())
    run_id = start_run(with_env(host, STUB_IGNORE_TERM="1"), run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    (runs.dir / "runs" / run_id / "cancel").write_text("1\n")
    assert wait_for(lambda: runs.get(run_id), lambda i: i.state == CANCELLED).state == CANCELLED


def test_nothing_of_a_stopped_run_outlives_it(host, started, monkeypatch):
    """A process of the run that ignores SIGTERM and holds its lock: the run is still going
    while it lives, and the supervisor's group goes with spark-submit."""
    monkeypatch.setattr(_local, "STOP_GRACE_S", 0.5)
    run = the_run(local_contract())
    run_id = start_run(with_env(host, STUB_GRANDCHILD="1"), run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    (runs.dir / "runs" / run_id / "cancel").write_text("1\n")
    assert wait_for(lambda: runs.get(run_id), lambda i: i.state == CANCELLED).state == CANCELLED


def test_a_cancel_finds_a_run_whose_process_group_was_never_recorded(host, started):
    run = the_run(local_contract())
    run_id = start_run(host, run, "t", runner_text=runner_source())
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, run_id))
    record = runs.dir / "runs" / run_id / "run.json"
    data = json.loads(record.read_text())
    del data["pgid"]  # as when the apply that started it stopped before it recorded it
    record.write_text(json.dumps(data))
    runs.cancel(run_id, grace=2.0)
    assert runs.get(run_id).state == CANCELLED


def test_an_interrupted_start_stops_the_run_it_started(host, monkeypatch):
    run = the_run(local_contract())
    wait = _local._wait_started

    def interrupted(where, process):
        wait(where, process)
        raise KeyboardInterrupt  # Ctrl-C as the run starts: the caller has no run id yet

    monkeypatch.setattr(_local, "_wait_started", interrupted)
    with pytest.raises(KeyboardInterrupt):
        start_run(host, run, "t", runner_text=runner_source())
    (info,) = LocalRuns(host, run.product, run.build).runs()
    assert info.state == CANCELLED


def test_a_late_cancel_leaves_a_finished_run_as_it_ended(host):
    run = the_run(batch_contract())
    out = rc.run_once(run, local_host=host, timeout=30, say=lambda _: 0)
    runs = LocalRuns(host, run.product, run.build)
    runs.cancel(out.run_id)
    where = runs.dir / "runs" / out.run_id
    assert not (where / "cancel").exists()
    (where / "cancel").write_text("1\n")  # a cancel that read the run just before it ended
    assert runs.get(out.run_id).state == SUCCEEDED


def test_an_end_recorded_while_the_run_is_read_is_its_end(tmp_path, monkeypatch):
    """The supervisor records the end and exits between the read of status.json and the
    check of the lock: the run ended, it was not lost."""
    runs = LocalRuns(LocalHost(tmp_path), "p", "b")
    run_id = "20261118T120000Z-0123abcd"
    where = runs.dir / "runs" / run_id
    where.mkdir(parents=True)
    (where / "run.json").write_text(json.dumps({"configHash": "h", "created": 1.0}))
    (where / "status.json").write_text(json.dumps({"started": 1.0}))

    def ended_meanwhile(lock):
        ended = {"started": 1.0, "ended": 2.0, "exitCode": 0}
        (where / "status.json").write_text(json.dumps(ended))
        return False

    monkeypatch.setattr(_local, "_held", ended_meanwhile)
    assert runs.get(run_id).state == SUCCEEDED


@pytest.mark.parametrize(
    "kill, raw, says",
    [
        # The machine shutting down sends every process SIGTERM: a stop from outside forge.
        (
            lambda pid: os.kill(pid, signal.SIGTERM),
            f"STOPPED (signal {int(signal.SIGTERM)})",
            "a signal from outside forge stopped it",
        ),
        # Killed with its machine: no end recorded.
        (
            lambda pid: os.killpg(pid, signal.SIGKILL),
            "LOST",
            "stopped without recording how they ended",
        ),
    ],
    ids=["stopped", "lost"],
)
def test_a_stream_that_died_with_its_machine_starts_again_as_a_restart(
    host, started, kill, raw, says
):
    """Only forge's cancel mark is a cancel: a stream stopped or killed with its machine
    died, so the next reconcile, a keepalive's included, starts it again on its checkpoint,
    marked as a restart of the run it replaces."""
    run = the_run(local_contract())
    (first,) = rc.reconcile([run], local_host=host)
    runs = LocalRuns(host, run.product, run.build)
    started.append((runs, first.run_id))
    status = json.loads((runs.dir / "runs" / first.run_id / "status.json").read_text())
    kill(status["supervisorPid"])
    died = wait_for(lambda: runs.get(first.run_id), lambda i: not i.active)
    assert (died.state, died.raw_state, died.lost) == (FAILED, raw, True)
    (again,) = rc.reconcile([run], local_host=host, only=True)
    started.append((runs, again.run_id))
    assert (again.action, again.error) == ("restarted", False) and again.run_id != first.run_id
    assert says in again.message and "restart 1 of 5 allowed per rolling hour" in again.message
    assert runs.get(again.run_id).restart_of == first.run_id
    record = json.loads((runs.dir / "runs" / again.run_id / "run.json").read_text())
    assert record["argv"][-2:] == ["--fluid-restart-of", first.run_id]


def test_a_spark_submit_that_cannot_start_is_a_failed_run(host, stub):
    run = the_run(batch_contract())
    broken = stub.parent / "not-a-program"
    broken.write_text("not a program\n")
    broken.chmod(0o755)
    out = rc.run_once(
        run,
        local_host=with_env(host, **{SUBMIT_EXECUTABLE_ENV: str(broken)}),
        timeout=30,
        say=lambda _: 0,
    )
    assert (out.action, out.error) == ("failed", True)
    assert "spark-submit did not start" in out.message


def test_old_ended_runs_are_pruned(host, monkeypatch):
    monkeypatch.setattr(_local, "KEEP_ENDED", 2)
    run = the_run(batch_contract())
    ids = []
    for _ in range(4):
        out = rc.run_once(run, local_host=host, timeout=30, say=lambda _: 0)
        assert out.state == SUCCEEDED
        ids.append(out.run_id)
    left = {p.name for p in (LocalRuns(host, run.product, run.build).dir / "runs").iterdir()}
    # Each start prunes the ended runs beyond the newest two: the first is gone by the fourth
    # start, and the second too when the fourth batch had already ended by its own prune.
    assert set(ids[2:]) <= left <= set(ids[1:])


def test_a_run_id_that_is_not_one_is_refused(tmp_path):
    runs = LocalRuns(LocalHost(tmp_path), "p", "b")
    with pytest.raises(SparkRunError, match="not a local run id"):
        runs.get("../../etc")


def test_a_failed_stream_restarts_within_its_budget_then_says_how_to_retry_it_here(host):
    """execution.retries.maxAttempts 1: one restart per rolling hour. The second failure
    within the hour is an error naming the budget, and the run history to remove when the
    cause was this machine's."""
    contract = local_contract()
    contract["builds"][0].setdefault("execution", {})["retries"] = {"maxAttempts": 1}
    run = the_run(contract)
    assert run.restarts_per_hour == 1
    failing = with_env(host, STUB_STREAM_EXIT="1")
    rc.reconcile([run], local_host=failing)
    runs = LocalRuns(host, run.product, run.build)
    (first,) = runs.runs()
    wait_for(lambda: runs.get(first.run_id), lambda i: i.state == FAILED)
    (again,) = rc.reconcile([run], local_host=failing)
    # The stub fails at once: the outcome is an error when the reconcile already saw it end.
    assert again.action == "restarted", again.message
    assert "the stream failed" in again.message and "restart 1 of 1" in again.message
    wait_for(lambda: runs.get(again.run_id), lambda i: i.state == FAILED)
    (spent,) = rc.reconcile([run], local_host=host, only=True)
    assert (spent.action, spent.error, spent.run_id) == ("failed", True, again.run_id)
    assert "budget of 1 restarts per rolling hour" in spent.message
    assert f"remove {runs.dir / 'runs'} (the build's run history here)" in spent.message
    assert len(runs.runs()) == 2  # nothing new started


def test_a_module_placed_beside_the_published_runner_is_never_imported(host):
    """spark-submit runs the runner as a script, so the script's directory is first on the
    driver's sys.path: a json.py a project placed beside the published runner would run."""
    run = the_run(batch_contract())
    sub = submitter(host)
    layout = Path(run.bind(local_root=sub.root).runner_uri).parent
    layout.mkdir(parents=True)
    marker = Path(host.workspace) / "planted-module-ran"
    (layout / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    out = rc.run_once(
        run, local_host=with_env(host, STUB_RUN_RUNNER="1"), timeout=60, say=lambda _: 0
    )
    assert out.run_id and not marker.exists()
    seen = argv_of(host)
    runner = seen["argv"][seen["argv"].index("--config") - 1]
    assert Path(runner).parent.name == out.run_id  # the run's own directory
    assert seen["safepath"] == "1"


def test_the_layout_s_runner_must_be_the_compiled_one(host):
    run = the_run(batch_contract())
    sub = submitter(host)
    bound = run.bind(local_root=sub.root)
    _local.publish(sub, bound, runner_source())
    Path(bound.runner_uri).write_text("print('not the runner')\n", encoding="utf-8")
    where = Path(host.workspace) / "run"
    where.mkdir()
    with pytest.raises(SparkRunError, match="sha256 differs"):
        _local.pull_runner(sub, bound, where)


def test_a_link_out_of_dot_fluid_is_refused(host, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    fluid = Path(host.workspace) / ".fluid"
    fluid.mkdir()
    (fluid / "spark-artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SparkRunError, match="outside"):
        submitter(host)
    assert list(outside.iterdir()) == []


def test_a_linked_state_directory_is_refused_and_skipped(host, tmp_path):
    run = the_run(local_contract())
    outside = tmp_path / "elsewhere"
    (outside / "runs").mkdir(parents=True)
    base = host.runtime_dir(run.product)
    base.mkdir(parents=True)
    (base / run.build).symlink_to(outside, target_is_directory=True)
    with pytest.raises(SparkRunError, match="outside"):
        start_run(host, run, "t", runner_text=runner_source())
    assert [p.name for p in outside.iterdir()] == ["runs"]
    assert rc.stop_removed(run.product, set(), host) == []


def test_a_lock_placed_as_a_link_is_no_run_s(tmp_path):
    import fcntl

    target = tmp_path / "held"
    target.write_text("")
    link = tmp_path / "lock"
    link.symlink_to(target)
    fd = os.open(target, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert _local._held(target) is True
        assert _local._held(link) is False
    finally:
        os.close(fd)


def test_a_linked_dot_fluid_is_refused(host, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path(host.workspace) / ".fluid").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SparkRunError, match="is a link"):
        submitter(host)


def test_publish_never_writes_through_a_link_under_the_default_root(host, tmp_path):
    run = the_run(local_contract())
    sub = submitter(host)
    bound = run.bind(local_root=sub.root)
    outside = tmp_path / "outside"
    outside.mkdir()
    product_dir = Path(bound.job_uri).parents[1]
    product_dir.parent.mkdir(parents=True)
    product_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SparkRunError, match="outside"):
        _local.publish(sub, bound, runner_source())
    assert list(outside.iterdir()) == []


def test_a_planted_temporary_file_name_is_never_written_through(host, tmp_path):
    run = the_run(local_contract())
    sub = submitter(host)
    bound = run.bind(local_root=sub.root)
    job = Path(bound.job_uri)
    job.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    (job.parent / f".job.json.{os.getpid()}.tmp").symlink_to(victim)
    _local.publish(sub, bound, runner_source())
    assert victim.read_text() == "untouched\n"
    assert job.read_text(encoding="utf-8") == bound.job_text


def test_a_linked_run_directory_is_neither_listed_nor_pruned(host, tmp_path):
    run = the_run(local_contract())
    runs = LocalRuns(host, run.product, run.build)
    (runs.dir / "runs").mkdir(parents=True)
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "run.json").write_text('{"runId": "x"}')
    (runs.dir / "runs" / "20261118T000000Z-0000abcd").symlink_to(kept, target_is_directory=True)
    assert runs.runs() == []
    runs.prune()
    assert (kept / "run.json").is_file()


def test_prune_keeps_the_restarts_a_restart_budget_still_counts(tmp_path):
    """Past the newest KEEP_ENDED ended runs, the restarts under an hour old stay."""
    runs = LocalRuns(LocalHost(tmp_path), "p", "b")
    now = time.time()
    created = [now - 60 * i for i in range(12)] + [now - 7200 - i for i in range(3)]
    for i, when in enumerate(created):
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when)) + f"-{i:08x}"
        where = runs.dir / "runs" / run_id
        where.mkdir(parents=True)
        record = {"configHash": "h", "created": when, "restartOf": "20261118T000000Z-00000000"}
        (where / "run.json").write_text(json.dumps(record))
        (where / "status.json").write_text(
            json.dumps({"started": when, "ended": when, "exitCode": 1})
        )
    runs.prune()
    kept = runs.runs()
    assert len(kept) == 12 and all(i.created >= now - 3600 for i in kept)


BASH_STUB = """#!/bin/bash
# A bash spark-submit, as Spark's own is: it records what bash was handed, then runs the
# stub. An exported function or a startup file would have run before this line.
env > "$STUB_OUT/bash-env.txt"
if declare -F stub_hook >/dev/null; then echo function > "$STUB_OUT/bash-function"; fi
exec "{python}" "{stub}" "$@"
"""


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs bash")
def test_a_bash_spark_submit_runs_no_function_or_startup_file_from_the_environment(
    host, tmp_path, stub
):
    out = Path(host.env["STUB_OUT"])
    pwned = tmp_path / "pwned"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch {pwned}\n", encoding="utf-8")
    wrapper = stub.parent / "bash-spark-submit"
    wrapper.write_text(BASH_STUB.format(python=sys.executable, stub=stub), encoding="utf-8")
    wrapper.chmod(0o755)
    hostile = with_env(
        host,
        **{
            SUBMIT_EXECUTABLE_ENV: str(wrapper),
            "BASH_FUNC_stub_hook%%": f"() {{ touch {pwned}; }}",
            "BASH_ENV": str(startup),
            "ENV": str(startup),
            "SHELLOPTS": "xtrace",
            "PS4": f"$(touch {pwned})",
        },
    )
    out_ = rc.run_once(the_run(batch_contract()), local_host=hostile, timeout=30, say=lambda _: 0)
    assert out_.state == SUCCEEDED, out_.message
    seen = (out / "bash-env.txt").read_text(encoding="utf-8")
    assert "BASH_FUNC_" not in seen and "BASH_ENV=" not in seen and "PS4=" not in seen
    assert "SHELLOPTS=xtrace" not in seen
    assert not (out / "bash-function").exists() and not pwned.exists()


def test_a_run_s_directory_status_and_log_are_owner_only(host):
    run = the_run(batch_contract())
    out = rc.run_once(run, local_host=host, timeout=30, say=lambda _: 0)
    assert out.state == SUCCEEDED, out.message
    where = LocalRuns(host, run.product, run.build).dir / "runs" / out.run_id
    for path in (where, where / "status.json", where / "driver.log"):
        assert path.stat().st_mode & 0o077 == 0, (path, oct(path.stat().st_mode))
