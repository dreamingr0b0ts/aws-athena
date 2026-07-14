"""Tests for the pre-flight estimator: SQL parsing + estimation with fakes."""

import pytest

from athena_toolkit.config import AthenaConfig
from athena_toolkit.preflight import (
    CostPreflight,
    check_budget,
    extract_partition_filters,
    extract_tables,
    strip_comments,
)


# -- SQL parsing ------------------------------------------------------------

def test_strip_comments():
    sql = "SELECT 1 -- a line comment\n/* block\ncomment */ FROM t"
    assert strip_comments(sql) == "SELECT 1 FROM t"


def test_extract_tables_simple():
    assert extract_tables("SELECT * FROM logs") == [(None, "logs")]


def test_extract_tables_qualified_and_quoted():
    sql = 'SELECT * FROM "analytics"."logs" JOIN db2.users u ON u.id = logs.uid'
    assert extract_tables(sql) == [("analytics", "logs"), ("db2", "users")]


def test_extract_tables_skips_subquery_and_dedupes():
    sql = "SELECT * FROM (SELECT * FROM logs) x JOIN logs l ON true"
    # the subquery source is skipped; inner + outer 'logs' dedupe to one
    assert extract_tables(sql) == [(None, "logs")]


def test_extract_partition_filters_equality_and_in():
    sql = "SELECT * FROM t WHERE dt = '2024-01-01' AND region IN ('us','eu')"
    filters = extract_partition_filters(sql, ["dt", "region", "other"])
    assert filters["dt"] == {"2024-01-01"}
    assert filters["region"] == {"us", "eu"}
    assert "other" not in filters


def test_extract_partition_filters_numeric():
    sql = "SELECT * FROM t WHERE year = 2024"
    assert extract_partition_filters(sql, ["year"]) == {"year": {"2024"}}


def test_extract_partition_filters_only_where_clause():
    # 'dt' mentioned only in SELECT, not WHERE -> not treated as a filter
    sql = "SELECT dt FROM t"
    assert extract_partition_filters(sql, ["dt"]) == {}


# -- estimator with a fake Catalog -----------------------------------------

class FakeCatalog:
    """Stand-in for schema.Catalog backed by in-memory metadata."""

    def __init__(self, tables, sizes):
        # tables: name -> {"PartitionKeys": [...], "StorageDescriptor": {...}}
        # sizes:  s3 location -> bytes
        self._tables = tables
        self._sizes = sizes
        # partitions: name -> [{"values": [...], "location": "..."}]
        self._partitions = {}

    def set_partitions(self, table, partitions):
        self._partitions[table] = partitions

    def get_table(self, table, database=None):
        if table not in self._tables:
            raise KeyError(f"no such table {table}")
        return self._tables[table]

    def iter_partition_details(self, table, database=None):
        return self._partitions.get(table, [])

    def sum_s3_size(self, s3_uri):
        return self._sizes.get(s3_uri, 0)


def make_preflight(catalog):
    config = AthenaConfig(database="db", output_location="s3://r/")
    return CostPreflight(config, catalog=catalog, price_per_tb=5.0)


def test_non_partitioned_table_uses_full_location_size():
    cat = FakeCatalog(
        tables={"flat": {"StorageDescriptor": {"Location": "s3://b/flat/"}}},
        sizes={"s3://b/flat/": 3_000_000},
    )
    report = make_preflight(cat).estimate("SELECT * FROM flat")
    assert report.total_bytes == 3_000_000
    assert report.tables[0].partitioned is False


def test_partitioned_full_scan_warns_and_sums_all():
    cat = FakeCatalog(
        tables={"events": {
            "PartitionKeys": [{"Name": "dt"}],
            "StorageDescriptor": {"Location": "s3://b/events/"},
        }},
        sizes={"s3://b/events/dt=2024-01-01/": 1_000_000,
               "s3://b/events/dt=2024-01-02/": 2_000_000},
    )
    cat.set_partitions("events", [
        {"values": ["2024-01-01"], "location": "s3://b/events/dt=2024-01-01/"},
        {"values": ["2024-01-02"], "location": "s3://b/events/dt=2024-01-02/"},
    ])
    report = make_preflight(cat).estimate("SELECT * FROM events")
    assert report.total_bytes == 3_000_000
    assert report.tables[0].pruning_applied is False
    assert any("FULL scan" in w for w in report.warnings)


def test_partition_pruning_selects_matching_only():
    cat = FakeCatalog(
        tables={"events": {
            "PartitionKeys": [{"Name": "dt"}],
            "StorageDescriptor": {"Location": "s3://b/events/"},
        }},
        sizes={"s3://b/events/dt=2024-01-01/": 1_000_000,
               "s3://b/events/dt=2024-01-02/": 2_000_000},
    )
    cat.set_partitions("events", [
        {"values": ["2024-01-01"], "location": "s3://b/events/dt=2024-01-01/"},
        {"values": ["2024-01-02"], "location": "s3://b/events/dt=2024-01-02/"},
    ])
    report = make_preflight(cat).estimate(
        "SELECT * FROM events WHERE dt = '2024-01-02'"
    )
    assert report.total_bytes == 2_000_000
    t = report.tables[0]
    assert t.pruning_applied is True
    assert t.partitions_selected == 1
    assert t.partitions_total == 2


def test_multi_key_pruning():
    cat = FakeCatalog(
        tables={"e": {
            "PartitionKeys": [{"Name": "dt"}, {"Name": "region"}],
            "StorageDescriptor": {"Location": "s3://b/e/"},
        }},
        sizes={"s3://b/e/dt=d1/region=us/": 10,
               "s3://b/e/dt=d1/region=eu/": 20,
               "s3://b/e/dt=d2/region=us/": 40},
    )
    cat.set_partitions("e", [
        {"values": ["d1", "us"], "location": "s3://b/e/dt=d1/region=us/"},
        {"values": ["d1", "eu"], "location": "s3://b/e/dt=d1/region=eu/"},
        {"values": ["d2", "us"], "location": "s3://b/e/dt=d2/region=us/"},
    ])
    report = make_preflight(cat).estimate(
        "SELECT * FROM e WHERE dt = 'd1' AND region IN ('us')"
    )
    assert report.total_bytes == 10
    assert report.tables[0].partitions_selected == 1


def test_missing_table_records_error_not_crash():
    cat = FakeCatalog(tables={}, sizes={})
    report = make_preflight(cat).estimate("SELECT * FROM ghost")
    assert report.has_errors
    assert report.tables[0].error is not None


def test_cost_applies_10mb_minimum():
    cat = FakeCatalog(
        tables={"tiny": {"StorageDescriptor": {"Location": "s3://b/tiny/"}}},
        sizes={"s3://b/tiny/": 1000},
    )
    report = make_preflight(cat).estimate("SELECT * FROM tiny")
    # 1 KB scanned but billed at the 10 MB minimum
    assert report.cost.billed_bytes == 10_000_000


def test_check_budget_bytes_and_cost():
    cat = FakeCatalog(
        tables={"t": {"StorageDescriptor": {"Location": "s3://b/t/"}}},
        sizes={"s3://b/t/": 2_000_000_000},  # 2 GB
    )
    report = make_preflight(cat).estimate("SELECT * FROM t")
    assert check_budget(report, max_bytes=1_000_000_000) is not None
    assert check_budget(report, max_bytes=5_000_000_000) is None
    assert check_budget(report, max_cost=0.000001) is not None
    assert check_budget(report, max_cost=999.0) is None
