"""Core query execution: submit -> poll -> fetch results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from athena_toolkit.client import AwsClients
from athena_toolkit.config import AthenaConfig
from athena_toolkit.cost import CostEstimate, estimate_cost

_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class QueryError(Exception):
    """Raised when a query fails or is cancelled."""

    def __init__(self, message: str, execution_id: str, state: str):
        super().__init__(message)
        self.execution_id = execution_id
        self.state = state


@dataclass
class QueryResult:
    execution_id: str
    state: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    bytes_scanned: int = 0
    execution_time_ms: int = 0
    output_location: str | None = None
    statement_type: str | None = None

    def cost(self, price_per_tb: float = 5.0) -> CostEstimate:
        return estimate_cost(self.bytes_scanned, price_per_tb)

    def dicts(self) -> list[dict[str, str | None]]:
        """Rows as a list of column->value dicts."""
        return [dict(zip(self.columns, row)) for row in self.rows]


class AthenaRunner:
    """Submit queries and retrieve results against a configured environment."""

    def __init__(self, config: AthenaConfig, clients: AwsClients | None = None):
        self.config = config
        self.clients = clients or AwsClients(config)

    # -- submission ---------------------------------------------------------
    def submit(self, sql: str, database: str | None = None) -> str:
        """Start a query and return its execution id (does not wait)."""
        params: dict[str, Any] = {"QueryString": sql}

        db = database or self.config.database
        if db:
            ctx: dict[str, str] = {"Database": db}
            if self.config.catalog:
                ctx["Catalog"] = self.config.catalog
            params["QueryExecutionContext"] = ctx

        if self.config.workgroup:
            params["WorkGroup"] = self.config.workgroup

        output = self.config.output_location
        if output:
            params["ResultConfiguration"] = {"OutputLocation": output}
        elif not self.config.workgroup:
            # Mirror the config guard so callers get a clear message.
            self.config.require_output()

        resp = self.clients.athena.start_query_execution(**params)
        return resp["QueryExecutionId"]

    # -- polling ------------------------------------------------------------
    def wait(self, execution_id: str) -> dict[str, Any]:
        """Poll until the query reaches a terminal state; return its detail."""
        deadline = time.monotonic() + self.config.max_wait
        interval = self.config.poll_interval
        while True:
            detail = self.clients.athena.get_query_execution(
                QueryExecutionId=execution_id
            )["QueryExecution"]
            state = detail["Status"]["State"]
            if state in _TERMINAL_STATES:
                return detail
            if time.monotonic() >= deadline:
                raise QueryError(
                    f"Query {execution_id} did not finish within "
                    f"{self.config.max_wait}s (last state: {state})",
                    execution_id,
                    state,
                )
            time.sleep(interval)

    # -- result paging ------------------------------------------------------
    def fetch_results(
        self, execution_id: str, max_rows: int | None = None
    ) -> tuple[list[str], list[list[str | None]]]:
        """Page through GetQueryResults, returning (columns, rows).

        The first data row returned by Athena repeats the column headers; this
        method strips that header row so ``rows`` contains only data.
        """
        paginator = self.clients.athena.get_paginator("get_query_results")
        columns: list[str] = []
        rows: list[list[str | None]] = []
        first_page = True
        for page in paginator.paginate(QueryExecutionId=execution_id):
            rs = page["ResultSet"]
            if not columns:
                meta = rs.get("ResultSetMetadata", {}).get("ColumnInfo", [])
                columns = [c["Name"] for c in meta]
            page_rows = rs.get("Rows", [])
            if first_page and page_rows:
                # Athena's first row echoes the header labels.
                header = [d.get("VarCharValue") for d in page_rows[0]["Data"]]
                if header == columns:
                    page_rows = page_rows[1:]
                first_page = False
            for r in page_rows:
                rows.append([d.get("VarCharValue") for d in r["Data"]])
                if max_rows is not None and len(rows) >= max_rows:
                    return columns, rows
        return columns, rows

    # -- one-shot convenience ----------------------------------------------
    def run(
        self,
        sql: str,
        database: str | None = None,
        max_rows: int | None = None,
        fetch: bool = True,
    ) -> QueryResult:
        """Submit, wait, and (optionally) fetch results in one call."""
        execution_id = self.submit(sql, database=database)
        detail = self.wait(execution_id)
        state = detail["Status"]["State"]
        stats = detail.get("Statistics", {})
        result = QueryResult(
            execution_id=execution_id,
            state=state,
            bytes_scanned=stats.get("DataScannedInBytes", 0) or 0,
            execution_time_ms=stats.get("TotalExecutionTimeInMillis", 0) or 0,
            output_location=detail.get("ResultConfiguration", {}).get("OutputLocation"),
            statement_type=detail.get("StatementType"),
        )
        if state != "SUCCEEDED":
            reason = detail["Status"].get("StateChangeReason", "no reason given")
            raise QueryError(
                f"Query {execution_id} {state}: {reason}", execution_id, state
            )
        # DDL/DML statements have no result rows to fetch.
        if fetch and result.statement_type != "DDL":
            result.columns, result.rows = self.fetch_results(
                execution_id, max_rows=max_rows
            )
        return result

    # -- history ------------------------------------------------------------
    def recent_executions(self, limit: int = 25) -> Iterator[dict[str, Any]]:
        """Yield detailed records for the most recent query executions."""
        list_kwargs: dict[str, Any] = {}
        if self.config.workgroup:
            list_kwargs["WorkGroup"] = self.config.workgroup
        ids: list[str] = []
        paginator = self.clients.athena.get_paginator("list_query_executions")
        for page in paginator.paginate(**list_kwargs):
            ids.extend(page.get("QueryExecutionIds", []))
            if len(ids) >= limit:
                break
        ids = ids[:limit]
        # batch_get_query_execution accepts up to 50 ids per call.
        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            resp = self.clients.athena.batch_get_query_execution(
                QueryExecutionIds=chunk
            )
            yield from resp.get("QueryExecutions", [])
