# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The schema cache folder is made when a schema is written, not when the manager is built."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fluid_build.schema_manager import SchemaCache, SchemaVersion

pytestmark = pytest.mark.unit


def test_building_a_cache_makes_no_folder_until_a_schema_is_written(tmp_path):
    folder = tmp_path / "home" / "schema_cache"

    cache = SchemaCache(folder)
    assert not folder.exists()

    version = SchemaVersion.parse("0.7.7")
    cache.cache_schema(version, {"title": "x"})
    assert (folder / "index.json").is_file()
    assert cache.get_cached_schema(version) == {"title": "x"}


def test_clearing_a_cache_that_was_never_written_is_a_no_op(tmp_path):
    cache = SchemaCache(tmp_path / "never")
    assert cache.clear_cache() == 0
    assert not (tmp_path / "never").exists()


JOB = textwrap.dedent("""
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    spark.table("db.a").write.saveAsTable("db.b")
    """)


def test_an_import_checks_its_proposal_without_creating_the_cache_folder(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow.pyspark import PySparkImporter

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "job.py").write_text(JOB, encoding="utf-8")

    contracts, _report = PySparkImporter().import_to_contracts(str(jobs), options={})

    assert [c["id"] for c in contracts] == ["db.b"]
    assert not (home / ".fluid" / "schema_cache").exists()
