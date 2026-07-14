"""Tests for the CTAS converter SQL generation."""

import pytest

from athena_toolkit.convert import build_ctas
from athena_toolkit.schema import Column


class FakeCatalog:
    def __init__(self, columns, partitions):
        self._info = {"columns": columns, "partitions": partitions}

    def describe(self, table, database=None):
        return self._info


def cat(data_cols, part_cols):
    return FakeCatalog(
        [Column(n, "string") for n in data_cols],
        [Column(n, "string") for n in part_cols],
    )


def test_basic_ctas_partitions_last():
    c = cat(["id", "msg", "ts"], ["dt", "region"])
    plan = build_ctas(
        c, "raw_logs", "logs_parquet", "s3://b/out/",
        source_database="db", target_database="db",
    )
    assert plan.partition_columns == ["dt", "region"]
    # partition columns appear LAST in the SELECT
    assert '"id", "msg", "ts", "dt", "region"' in plan.sql
    assert "format = 'PARQUET'" in plan.sql
    assert "parquet_compression = 'SNAPPY'" in plan.sql
    assert "external_location = 's3://b/out/'" in plan.sql
    assert "partitioned_by = ARRAY['dt', 'region']" in plan.sql
    assert 'CREATE TABLE "db"."logs_parquet"' in plan.sql
    assert 'FROM "db"."raw_logs"' in plan.sql


def test_override_partitioned_by_reorders_data_column():
    # Re-partition a flat table by one of its data columns.
    c = cat(["id", "msg", "event_date"], [])
    plan = build_ctas(
        c, "flat", "flat_part", "s3://b/out/",
        source_database="db", partitioned_by=["event_date"],
    )
    assert plan.partition_columns == ["event_date"]
    assert plan.data_columns == ["id", "msg"]
    assert '"id", "msg", "event_date"' in plan.sql
    assert "partitioned_by = ARRAY['event_date']" in plan.sql


def test_no_partitions_omits_partitioned_by():
    c = cat(["id", "msg"], [])
    plan = build_ctas(c, "flat", "flat_pq", "s3://b/out/", source_database="db")
    assert "partitioned_by" not in plan.sql
    assert plan.partition_columns == []


def test_orc_uses_orc_compression():
    c = cat(["id"], [])
    plan = build_ctas(
        c, "t", "t_orc", "s3://b/out/", source_database="db",
        fmt="ORC", compression="ZLIB",
    )
    assert "format = 'ORC'" in plan.sql
    assert "orc_compression = 'ZLIB'" in plan.sql


def test_invalid_format_raises():
    c = cat(["id"], [])
    with pytest.raises(ValueError):
        build_ctas(c, "t", "t2", "s3://b/out/", fmt="AVRO")


def test_invalid_compression_for_format_raises():
    c = cat(["id"], [])
    with pytest.raises(ValueError):
        # ZLIB is an ORC compression, not valid for PARQUET
        build_ctas(c, "t", "t2", "s3://b/out/", fmt="PARQUET", compression="ZLIB")


def test_non_s3_location_raises():
    c = cat(["id"], [])
    with pytest.raises(ValueError):
        build_ctas(c, "t", "t2", "/local/path")


def test_identifier_quote_escaping():
    c = cat(['we"ird'], [])
    plan = build_ctas(c, "t", "t2", "s3://b/out/", source_database="db")
    assert '"we""ird"' in plan.sql
