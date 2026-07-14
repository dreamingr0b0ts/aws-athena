"""Core runner tests using a hand-rolled fake Athena client (no boto3/network)."""

import pytest

from athena_toolkit.client import AwsClients
from athena_toolkit.config import AthenaConfig
from athena_toolkit.core import AthenaRunner, QueryError


def _row(*values):
    return {"Data": [{"VarCharValue": v} if v is not None else {} for v in values]}


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        for p in self._pages:
            yield p


class FakeAthena:
    """Minimal stand-in for the boto3 Athena client."""

    def __init__(self, *, state="SUCCEEDED", statement_type="DML", bytes_scanned=2_000_000):
        self.state = state
        self.statement_type = statement_type
        self.bytes_scanned = bytes_scanned
        self.started = []
        self._result_pages = [
            {
                "ResultSet": {
                    "ResultSetMetadata": {
                        "ColumnInfo": [{"Name": "id"}, {"Name": "name"}]
                    },
                    "Rows": [
                        _row("id", "name"),   # echoed header
                        _row("1", "alice"),
                        _row("2", None),
                    ],
                }
            }
        ]

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "qid-123"}

    def get_query_execution(self, QueryExecutionId):
        return {
            "QueryExecution": {
                "QueryExecutionId": QueryExecutionId,
                "Status": {"State": self.state, "StateChangeReason": "boom"},
                "Statistics": {
                    "DataScannedInBytes": self.bytes_scanned,
                    "TotalExecutionTimeInMillis": 1234,
                },
                "StatementType": self.statement_type,
                "ResultConfiguration": {"OutputLocation": "s3://results/qid-123.csv"},
            }
        }

    def get_paginator(self, name):
        assert name == "get_query_results"
        return FakePaginator(self._result_pages)


def make_runner(fake):
    config = AthenaConfig(output_location="s3://results/", database="db")
    clients = AwsClients(config, session=object())  # session never used
    # Inject the fake client directly.
    clients.client = lambda service: fake  # type: ignore[assignment]
    return AthenaRunner(config, clients=clients)


def test_run_success_strips_header_and_parses_rows():
    fake = FakeAthena()
    runner = make_runner(fake)
    result = runner.run("SELECT * FROM t")
    assert result.state == "SUCCEEDED"
    assert result.columns == ["id", "name"]
    assert result.rows == [["1", "alice"], ["2", None]]
    assert result.bytes_scanned == 2_000_000
    assert result.execution_time_ms == 1234


def test_submit_sends_context_and_workgroup():
    fake = FakeAthena()
    config = AthenaConfig(
        output_location="s3://results/", database="db",
        workgroup="wg", catalog="AwsDataCatalog",
    )
    clients = AwsClients(config, session=object())
    clients.client = lambda service: fake  # type: ignore[assignment]
    runner = AthenaRunner(config, clients=clients)
    runner.submit("SELECT 1")
    sent = fake.started[0]
    assert sent["QueryExecutionContext"] == {"Database": "db", "Catalog": "AwsDataCatalog"}
    assert sent["WorkGroup"] == "wg"
    assert sent["ResultConfiguration"] == {"OutputLocation": "s3://results/"}


def test_failed_query_raises():
    fake = FakeAthena(state="FAILED")
    runner = make_runner(fake)
    with pytest.raises(QueryError) as exc:
        runner.run("SELECT bad")
    assert exc.value.state == "FAILED"
    assert "boom" in str(exc.value)


def test_ddl_skips_result_fetch():
    fake = FakeAthena(statement_type="DDL", bytes_scanned=0)
    runner = make_runner(fake)
    result = runner.run("CREATE TABLE t ...")
    assert result.statement_type == "DDL"
    assert result.columns == []
    assert result.rows == []


def test_max_rows_limits_fetch():
    fake = FakeAthena()
    runner = make_runner(fake)
    result = runner.run("SELECT * FROM t", max_rows=1)
    assert result.rows == [["1", "alice"]]


def test_cost_helper_on_result():
    fake = FakeAthena(bytes_scanned=1_000_000_000_000)
    runner = make_runner(fake)
    result = runner.run("SELECT * FROM t")
    assert round(result.cost(5.0).cost_usd, 6) == 5.0


def test_dicts_helper():
    fake = FakeAthena()
    runner = make_runner(fake)
    result = runner.run("SELECT * FROM t")
    assert result.dicts() == [
        {"id": "1", "name": "alice"},
        {"id": "2", "name": None},
    ]
