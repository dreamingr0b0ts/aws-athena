"""Pre-flight cost estimation and guardrails for Athena queries.

Athena has no native dry-run that reports bytes scanned, yet bytes scanned is
exactly what you pay for. This module estimates an **upper bound** on the data
a query will scan *before* you run it, by:

  1. parsing the referenced tables out of the SQL,
  2. reading each table's layout from the Glue Data Catalog,
  3. applying any partition-key filters found in the query to select only the
     matching partitions, and
  4. summing the size of the relevant S3 objects.

The estimate is deliberately conservative (an upper bound): when in doubt it
assumes a full scan, so a guardrail built on it errs on the side of blocking
rather than letting an expensive query through.

Known limitations (all push the estimate *up*, keeping it safe as a guardrail):
  - Pruning is only detected from equality/``IN`` predicates in the ``WHERE``
    clause on partition columns; pruning via joins or functions is ignored.
  - Columnar formats (Parquet/ORC) usually scan *less* than the file size
    because of column projection and predicate pushdown.
  - ``CREATE TABLE AS`` / ``INSERT`` read sizes are estimated from their
    ``SELECT``; the write side is not costed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from athena_toolkit.config import AthenaConfig
from athena_toolkit.cost import CostEstimate, estimate_cost
from athena_toolkit.schema import Catalog

# --------------------------------------------------------------------------
# SQL parsing (best-effort, regex based)
# --------------------------------------------------------------------------

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_IDENT = r'[`"]?[A-Za-z_][\w]*[`"]?(?:\.[`"]?[A-Za-z_][\w]*[`"]?)?'
_TABLE_REF = re.compile(rf"\b(?:from|join)\s+(?!\()({_IDENT})", re.IGNORECASE)
_WHERE_SPLIT = re.compile(r"\bwhere\b", re.IGNORECASE)


def strip_comments(sql: str) -> str:
    """Remove block and line comments and collapse to single spaces."""
    sql = _COMMENT_BLOCK.sub(" ", sql)
    sql = _COMMENT_LINE.sub(" ", sql)
    return re.sub(r"\s+", " ", sql).strip()


def _unquote(token: str) -> str:
    return token.strip().strip('`"')


def extract_tables(sql: str) -> list[tuple[str | None, str]]:
    """Return ``(database, table)`` pairs referenced after FROM/JOIN.

    ``database`` is ``None`` when the reference is unqualified. Subquery
    sources (``FROM (SELECT ...)``) are skipped. Results are de-duplicated
    while preserving order.
    """
    cleaned = strip_comments(sql)
    seen: set[tuple[str | None, str]] = set()
    out: list[tuple[str | None, str]] = []
    for match in _TABLE_REF.finditer(cleaned):
        raw = match.group(1)
        parts = [_unquote(p) for p in raw.split(".")]
        if len(parts) == 2:
            ref: tuple[str | None, str] = (parts[0], parts[1])
        else:
            ref = (None, parts[0])
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _where_clause(sql: str) -> str:
    """Return the text from the first WHERE keyword onward (else "")."""
    parts = _WHERE_SPLIT.split(strip_comments(sql), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def extract_partition_filters(
    sql: str, partition_keys: Iterable[str]
) -> dict[str, set[str]]:
    """Find equality / ``IN`` constraints on the given partition keys.

    Only the ``WHERE`` clause is inspected. Returns a mapping of partition key
    -> the set of allowed string values. Keys absent from the result are
    unconstrained (treated as "scan all" by the estimator).
    """
    where = _where_clause(sql)
    if not where:
        return {}
    filters: dict[str, set[str]] = {}
    for key in partition_keys:
        k = re.escape(key)
        # key = 'literal'
        eq_str = re.search(rf"\b{k}\s*=\s*'([^']*)'", where, re.IGNORECASE)
        # key = numeric
        eq_num = re.search(rf"\b{k}\s*=\s*(\d+)\b", where, re.IGNORECASE)
        # key IN ( ... )
        in_match = re.search(rf"\b{k}\s+in\s*\(([^)]*)\)", where, re.IGNORECASE)
        values: set[str] = set()
        if in_match:
            for item in in_match.group(1).split(","):
                item = item.strip().strip("'")
                if item:
                    values.add(item)
        elif eq_str:
            values.add(eq_str.group(1))
        elif eq_num:
            values.add(eq_num.group(1))
        if values:
            filters[key] = values
    return filters


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------


@dataclass
class TableEstimate:
    database: str | None
    table: str
    partitioned: bool = False
    partition_keys: list[str] = field(default_factory=list)
    matched_keys: list[str] = field(default_factory=list)
    partitions_total: int = 0
    partitions_selected: int = 0
    bytes_estimated: int = 0
    pruning_applied: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class PreflightReport:
    sql: str
    tables: list[TableEstimate]
    total_bytes: int
    cost: CostEstimate
    warnings: list[str]

    @property
    def has_errors(self) -> bool:
        return any(t.error for t in self.tables)


class CostPreflight:
    """Estimate the bytes/cost a query will scan, before running it."""

    def __init__(
        self,
        config: AthenaConfig,
        catalog: Catalog | None = None,
        price_per_tb: float = 5.0,
    ):
        self.config = config
        self.catalog = catalog or Catalog(config)
        self.price_per_tb = price_per_tb

    def estimate(self, sql: str) -> PreflightReport:
        refs = extract_tables(sql)
        table_estimates: list[TableEstimate] = []
        warnings: list[str] = []

        if not refs:
            warnings.append(
                "No source tables detected; estimate may be incomplete "
                "(subqueries/CTEs are not resolved)."
            )

        for database, table in refs:
            est = self._estimate_table(sql, database, table)
            table_estimates.append(est)
            warnings.extend(f"[{est.table}] {w}" for w in est.warnings)

        total = sum(t.bytes_estimated for t in table_estimates)
        # Apply Athena's per-query 10 MB minimum once, to the whole query.
        cost = estimate_cost(total, self.price_per_tb)
        return PreflightReport(
            sql=sql,
            tables=table_estimates,
            total_bytes=total,
            cost=cost,
            warnings=warnings,
        )

    def _estimate_table(
        self, sql: str, database: str | None, table: str
    ) -> TableEstimate:
        db = database or self.config.database
        est = TableEstimate(database=db, table=table)
        try:
            meta = self.catalog.get_table(table, db)
        except Exception as exc:  # noqa: BLE001 - surface as a per-table note
            est.error = f"could not read Glue metadata: {exc}"
            est.warnings.append(est.error)
            return est

        partition_keys = [k["Name"] for k in meta.get("PartitionKeys", [])]
        est.partition_keys = partition_keys
        sd = meta.get("StorageDescriptor", {})

        if not partition_keys:
            est.partitioned = False
            est.bytes_estimated = self.catalog.sum_s3_size(sd.get("Location"))
            return est

        est.partitioned = True
        filters = extract_partition_filters(sql, partition_keys)
        est.matched_keys = [k for k in partition_keys if k in filters]

        details = self.catalog.iter_partition_details(table, db)
        est.partitions_total = len(details)

        selected = self._select_partitions(details, partition_keys, filters)
        est.partitions_selected = len(selected)
        est.pruning_applied = bool(filters)
        est.bytes_estimated = sum(
            self.catalog.sum_s3_size(p.get("location")) for p in selected
        )

        if not filters:
            est.warnings.append(
                f"partitioned by {partition_keys} but no partition filter "
                f"found — estimating a FULL scan of all "
                f"{est.partitions_total} partitions."
            )
        return est

    @staticmethod
    def _select_partitions(
        details: list[dict], partition_keys: list[str], filters: dict[str, set[str]]
    ) -> list[dict]:
        if not filters:
            return details
        idx = {k: i for i, k in enumerate(partition_keys)}
        selected = []
        for p in details:
            values = p.get("values", [])
            ok = True
            for key, allowed in filters.items():
                i = idx.get(key)
                if i is None or i >= len(values):
                    ok = False
                    break
                if values[i] not in allowed:
                    ok = False
                    break
            if ok:
                selected.append(p)
        return selected


def check_budget(
    report: PreflightReport,
    max_bytes: int | None = None,
    max_cost: float | None = None,
) -> str | None:
    """Return a violation message if the report exceeds a budget, else None."""
    if max_bytes is not None and report.total_bytes > max_bytes:
        return (
            f"estimated scan {report.total_bytes} bytes exceeds limit "
            f"{max_bytes} bytes"
        )
    if max_cost is not None and report.cost.cost_usd > max_cost:
        return (
            f"estimated cost ${report.cost.cost_usd:.6f} exceeds limit "
            f"${max_cost:.6f}"
        )
    return None
