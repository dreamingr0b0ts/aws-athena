"""Tests for partition-projection inference and plan generation."""

import pytest

from athena_toolkit.config import AthenaConfig
from athena_toolkit.projection import (
    ProjectionGenerator,
    build_location_template,
    infer_projection_column,
)


# -- per-column inference ---------------------------------------------------

def test_infer_date_from_values():
    col = infer_projection_column("dt", "string", ["2024-01-01", "2024-03-15"])
    assert col.type == "date"
    assert col.properties["format"] == "yyyy-MM-dd"
    assert col.properties["range"] == "2024-01-01,2024-03-15"
    assert col.properties["interval.unit"] == "DAYS"


def test_infer_date_from_glue_type():
    col = infer_projection_column("d", "date", [])
    assert col.type == "date"
    assert col.properties["range"] == "NOW-3YEARS,NOW"


def test_infer_month_format():
    col = infer_projection_column("m", "string", ["2024-01", "2024-02"])
    assert col.properties["format"] == "yyyy-MM"
    assert col.properties["interval.unit"] == "MONTHS"


def test_infer_yyyymmdd():
    col = infer_projection_column("d", "string", ["20240101", "20240115"])
    assert col.type == "date"
    assert col.properties["format"] == "yyyyMMdd"


def test_infer_integer_range():
    col = infer_projection_column("year", "int", ["2019", "2024", "2021"])
    assert col.type == "integer"
    assert col.properties["range"] == "2019,2024"
    assert "digits" not in col.properties


def test_infer_integer_zero_padded_adds_digits():
    col = infer_projection_column("hour", "string", ["00", "05", "23"])
    assert col.type == "integer"
    assert col.properties["digits"] == "2"


def test_infer_enum_fallback():
    col = infer_projection_column("region", "string", ["us", "eu", "us"])
    assert col.type == "enum"
    assert col.properties["values"] == "eu,us"


# -- location template ------------------------------------------------------

def test_template_hive_style():
    tmpl = build_location_template(
        "s3://b/events",
        ["dt", "region"],
        "s3://b/events/dt=2024-01-01/region=us",
        ["2024-01-01", "us"],
    )
    assert tmpl == "s3://b/events/dt=${dt}/region=${region}"


def test_template_bare_segments():
    tmpl = build_location_template(
        "s3://b/events",
        ["dt", "region"],
        "s3://b/events/2024-01-01/us",
        ["2024-01-01", "us"],
    )
    assert tmpl == "s3://b/events/${dt}/${region}"


def test_template_fallback_to_hive_under_table_location():
    tmpl = build_location_template("s3://b/events/", ["dt"], None, [])
    assert tmpl == "s3://b/events/dt=${dt}"


# -- end-to-end plan with a fake catalog ------------------------------------

class FakeCatalog:
    def __init__(self, meta, partitions):
        self._meta = meta
        self._partitions = partitions

    def get_table(self, table, database=None):
        return self._meta

    def iter_partition_details(self, table, database=None):
        return self._partitions


def test_generate_plan_emits_alter_sql():
    meta = {
        "PartitionKeys": [{"Name": "dt", "Type": "string"},
                          {"Name": "region", "Type": "string"}],
        "StorageDescriptor": {"Location": "s3://b/events"},
    }
    partitions = [
        {"values": ["2024-01-01", "us"], "location": "s3://b/events/dt=2024-01-01/region=us"},
        {"values": ["2024-01-02", "eu"], "location": "s3://b/events/dt=2024-01-02/region=eu"},
    ]
    config = AthenaConfig(database="db")
    plan = ProjectionGenerator(config, catalog=FakeCatalog(meta, partitions)).generate("events")
    props = plan.tblproperties()
    assert props["projection.enabled"] == "true"
    assert props["projection.dt.type"] == "date"
    assert props["projection.region.type"] == "enum"
    assert props["projection.region.values"] == "eu,us"
    assert props["storage.location.template"] == "s3://b/events/dt=${dt}/region=${region}"

    sql = plan.to_alter_sql()
    assert sql.startswith("ALTER TABLE `db`.`events` SET TBLPROPERTIES (")
    assert "'projection.enabled' = 'true'" in sql


def test_generate_raises_for_unpartitioned_table():
    meta = {"PartitionKeys": [], "StorageDescriptor": {"Location": "s3://b/flat"}}
    config = AthenaConfig(database="db")
    gen = ProjectionGenerator(config, catalog=FakeCatalog(meta, []))
    with pytest.raises(ValueError):
        gen.generate("flat")
