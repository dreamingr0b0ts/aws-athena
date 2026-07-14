"""Command-line interface for athena-toolkit.

Subcommands:
    query        Run a SQL query and print results (table/csv/json).
    preflight    Estimate bytes/cost a query will scan, before running it.
    databases    List databases in the catalog.
    tables       List tables in a database.
    describe     Show a table's columns and partition keys.
    ddl          Print CREATE EXTERNAL TABLE DDL for a table.
    projection   Generate partition-projection TBLPROPERTIES for a table.
    convert      Build/run a CTAS to a partitioned Parquet/ORC table.
    partitions   List a table's partition values.
    history      Show recent query executions with cost.
    cost         Estimate the cost of an arbitrary byte count.
    config       Show the resolved configuration for an environment.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from athena_toolkit import __version__
from athena_toolkit.config import AthenaConfig, ConfigError, load_config
from athena_toolkit.cost import estimate_cost, human_bytes, parse_size
from athena_toolkit.formatting import render


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Flags shared by every subcommand for selecting/overriding config."""
    g = parser.add_argument_group("environment")
    g.add_argument("--env", help="Named environment from the config file.")
    g.add_argument("--config", help="Path to a TOML config file.")
    g.add_argument("--profile", help="AWS credentials profile.")
    g.add_argument("--region", help="AWS region.")
    g.add_argument("--workgroup", help="Athena workgroup.")
    g.add_argument("--output-location", help="S3 URI for query results.")
    g.add_argument("--database", help="Default database / schema.")
    g.add_argument("--catalog", help="Data catalog (default AwsDataCatalog).")


def _config_from_args(args: argparse.Namespace) -> AthenaConfig:
    overrides = {
        "profile": args.profile,
        "region": args.region,
        "workgroup": args.workgroup,
        "output_location": args.output_location,
        "database": args.database,
        "catalog": args.catalog,
    }
    return load_config(
        environment=args.env, overrides=overrides, config_path=args.config
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="athena-toolkit",
        description="A boto3-based CLI toolkit for AWS Athena.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # query
    p = sub.add_parser("query", help="Run a SQL query and print results.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("sql", nargs="?", help="SQL text to execute.")
    src.add_argument("-f", "--file", help="Read SQL from a file ('-' for stdin).")
    p.add_argument("--format", default="table", choices=["table", "csv", "json"])
    p.add_argument("--max-rows", type=int, help="Limit number of rows fetched.")
    p.add_argument("--price-per-tb", type=float, default=5.0)
    p.add_argument("--no-cost", action="store_true", help="Hide the cost footer.")
    g = p.add_argument_group("cost guardrail (pre-flight estimate via Glue+S3)")
    g.add_argument(
        "--dry-run", action="store_true",
        help="Estimate scan/cost and exit without running the query.",
    )
    g.add_argument(
        "--max-scan", type=parse_size, metavar="SIZE",
        help="Refuse to run if the estimate exceeds this (e.g. 500MB, 10GB).",
    )
    g.add_argument(
        "--max-cost", type=float, metavar="USD",
        help="Refuse to run if the estimated cost (USD) exceeds this.",
    )
    g.add_argument(
        "--force", action="store_true",
        help="Run even if a budget is exceeded (the estimate is still shown).",
    )
    _add_common(p)
    p.set_defaults(func=cmd_query)

    # preflight
    p = sub.add_parser(
        "preflight",
        help="Estimate the bytes/cost a query will scan, before running it.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("sql", nargs="?", help="SQL text to analyse.")
    src.add_argument("-f", "--file", help="Read SQL from a file ('-' for stdin).")
    p.add_argument("--price-per-tb", type=float, default=5.0)
    p.add_argument("--format", default="table", choices=["table", "json"])
    _add_common(p)
    p.set_defaults(func=cmd_preflight)

    # databases
    p = sub.add_parser("databases", help="List databases in the catalog.")
    _add_common(p)
    p.set_defaults(func=cmd_databases)

    # tables
    p = sub.add_parser("tables", help="List tables in a database.")
    p.add_argument("database", nargs="?", help="Database (defaults to config).")
    _add_common(p)
    p.set_defaults(func=cmd_tables)

    # describe
    p = sub.add_parser("describe", help="Show a table's columns/partitions.")
    p.add_argument("table")
    p.add_argument("--format", default="table", choices=["table", "csv", "json"])
    _add_common(p)
    p.set_defaults(func=cmd_describe)

    # ddl
    p = sub.add_parser("ddl", help="Print CREATE TABLE DDL for a table.")
    p.add_argument("table")
    _add_common(p)
    p.set_defaults(func=cmd_ddl)

    # projection
    p = sub.add_parser(
        "projection",
        help="Generate partition-projection TBLPROPERTIES for a table.",
    )
    p.add_argument("table")
    p.add_argument(
        "--sample", type=int, default=500,
        help="Max existing partitions to inspect for inference (default 500).",
    )
    p.add_argument("--format", default="sql", choices=["sql", "json"])
    _add_common(p)
    p.set_defaults(func=cmd_projection)

    # convert
    p = sub.add_parser(
        "convert",
        help="Build (and optionally run) a CTAS to Parquet/ORC partitioned table.",
    )
    p.add_argument("source", help="Source table to read from.")
    p.add_argument("target", help="New table to create.")
    p.add_argument(
        "--location", required=True, metavar="S3_URI",
        help="external_location for the new table (must be empty).",
    )
    p.add_argument("--format", default="PARQUET", choices=["PARQUET", "ORC"])
    p.add_argument("--compression", default="SNAPPY")
    p.add_argument(
        "--partitioned-by", metavar="COLS",
        help="Comma-separated partition columns (default: source's).",
    )
    p.add_argument(
        "--target-database", help="Database for the new table (default: same).",
    )
    p.add_argument(
        "--run", action="store_true",
        help="Execute the CTAS (default just prints the SQL).",
    )
    p.add_argument("--price-per-tb", type=float, default=5.0)
    _add_common(p)
    p.set_defaults(func=cmd_convert)

    # partitions
    p = sub.add_parser("partitions", help="List a table's partition values.")
    p.add_argument("table")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--format", default="table", choices=["table", "csv", "json"])
    _add_common(p)
    p.set_defaults(func=cmd_partitions)

    # history
    p = sub.add_parser("history", help="Show recent query executions with cost.")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--price-per-tb", type=float, default=5.0)
    p.add_argument("--format", default="table", choices=["table", "csv", "json"])
    _add_common(p)
    p.set_defaults(func=cmd_history)

    # cost
    p = sub.add_parser("cost", help="Estimate cost for a given bytes-scanned count.")
    p.add_argument("bytes_scanned", type=int, help="Bytes scanned.")
    p.add_argument("--price-per-tb", type=float, default=5.0)
    p.set_defaults(func=cmd_cost)

    # config
    p = sub.add_parser("config", help="Show the resolved configuration.")
    _add_common(p)
    p.set_defaults(func=cmd_config)

    return parser


# -- command handlers -------------------------------------------------------

def _read_sql(args: argparse.Namespace) -> str:
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        with open(args.file, "r", encoding="utf-8") as fh:
            return fh.read()
    return args.sql


def _render_preflight(report, fmt: str = "table") -> str:
    """Render a PreflightReport as a table (default) or JSON."""
    if fmt == "json":
        import json

        return json.dumps(
            {
                "total_bytes": report.total_bytes,
                "total_human": human_bytes(report.total_bytes),
                "estimated_cost_usd": round(report.cost.cost_usd, 6),
                "warnings": report.warnings,
                "tables": [
                    {
                        "database": t.database,
                        "table": t.table,
                        "partitioned": t.partitioned,
                        "partition_keys": t.partition_keys,
                        "matched_keys": t.matched_keys,
                        "partitions_selected": t.partitions_selected,
                        "partitions_total": t.partitions_total,
                        "bytes_estimated": t.bytes_estimated,
                        "pruning_applied": t.pruning_applied,
                        "error": t.error,
                    }
                    for t in report.tables
                ],
            },
            indent=2,
        )

    headers = ["table", "partitioned", "partitions", "pruning", "est. scan"]
    rows = []
    for t in report.tables:
        name = f"{t.database}.{t.table}" if t.database else t.table
        if t.error:
            rows.append([name, "?", "?", "ERROR", t.error])
            continue
        if t.partitioned:
            parts = f"{t.partitions_selected}/{t.partitions_total}"
            pruning = "yes" if t.pruning_applied else "NO (full scan)"
        else:
            parts = "-"
            pruning = "n/a"
        rows.append([name, str(t.partitioned), parts, pruning, human_bytes(t.bytes_estimated)])
    out = [render(headers, rows, "table")]
    out.append(
        f"\nestimated total scan: {human_bytes(report.total_bytes)}  "
        f"(upper bound)\nestimated cost      : ${report.cost.cost_usd:.6f} "
        f"@ ${report.cost.price_per_tb:.2f}/TB"
    )
    for w in report.warnings:
        out.append(f"warning: {w}")
    return "\n".join(out)


def cmd_query(args: argparse.Namespace) -> int:
    from athena_toolkit.core import AthenaRunner, QueryError

    config = _config_from_args(args)
    sql = _read_sql(args)

    # Pre-flight guardrail: only when the user asks for it (--dry-run or a budget).
    if args.dry_run or args.max_scan is not None or args.max_cost is not None:
        from athena_toolkit.preflight import CostPreflight, check_budget

        report = CostPreflight(config, price_per_tb=args.price_per_tb).estimate(sql)
        print(_render_preflight(report), file=sys.stderr)
        if args.dry_run:
            return 0
        violation = check_budget(report, args.max_scan, args.max_cost)
        if violation and not args.force:
            print(
                f"\nblocked: {violation}\n"
                f"re-run with --force to override, or narrow the query "
                f"(e.g. add a partition filter).",
                file=sys.stderr,
            )
            return 3

    runner = AthenaRunner(config)
    try:
        result = runner.run(sql, max_rows=args.max_rows)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.statement_type == "DDL" or not result.columns:
        print(f"OK ({result.state}).")
    else:
        print(render(result.columns, result.rows, args.format))

    if not args.no_cost:
        est = estimate_cost(result.bytes_scanned, args.price_per_tb)
        print(
            f"\nscanned {human_bytes(est.bytes_scanned)} | "
            f"est. cost ${est.cost_usd:.6f} | "
            f"{result.execution_time_ms} ms | id {result.execution_id}",
            file=sys.stderr,
        )
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    from athena_toolkit.preflight import CostPreflight

    config = _config_from_args(args)
    sql = _read_sql(args)
    report = CostPreflight(config, price_per_tb=args.price_per_tb).estimate(sql)
    print(_render_preflight(report, args.format))
    return 0


def cmd_databases(args: argparse.Namespace) -> int:
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    names = Catalog(config).list_databases()
    print(render(["database"], [[n] for n in names], "table"))
    return 0


def cmd_tables(args: argparse.Namespace) -> int:
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    names = Catalog(config).list_tables(args.database)
    print(render(["table"], [[n] for n in names], "table"))
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    info = Catalog(config).describe(args.table)
    rows = [[c.name, c.type, "column", c.comment or ""] for c in info["columns"]]
    rows += [[c.name, c.type, "partition", c.comment or ""] for c in info["partitions"]]
    print(render(["name", "type", "kind", "comment"], rows, args.format))
    return 0


def cmd_ddl(args: argparse.Namespace) -> int:
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    print(Catalog(config).generate_ddl(args.table))
    return 0


def cmd_projection(args: argparse.Namespace) -> int:
    from athena_toolkit.projection import ProjectionGenerator

    config = _config_from_args(args)
    plan = ProjectionGenerator(config).generate(args.table, sample=args.sample)
    if args.format == "json":
        import json

        print(json.dumps(plan.tblproperties(), indent=2))
    else:
        print(plan.to_alter_sql())
    for note in plan.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    from athena_toolkit.convert import build_ctas, source_size_bytes
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    catalog = Catalog(config)
    partitioned_by = (
        [c.strip() for c in args.partitioned_by.split(",") if c.strip()]
        if args.partitioned_by is not None
        else None
    )
    plan = build_ctas(
        catalog,
        args.source,
        args.target,
        args.location,
        source_database=config.database,
        target_database=args.target_database or config.database,
        fmt=args.format,
        compression=args.compression,
        partitioned_by=partitioned_by,
    )
    print(plan.sql)
    print(
        f"\n-- partitioned by: {plan.partition_columns or '(none)'}",
        file=sys.stderr,
    )

    if not args.run:
        print(
            "-- dry run: re-run with --run to execute this CTAS.",
            file=sys.stderr,
        )
        return 0

    from athena_toolkit.core import AthenaRunner, QueryError

    try:
        before = source_size_bytes(catalog, args.source, config.database)
    except Exception:  # noqa: BLE001 - sizing is best-effort context
        before = 0
    runner = AthenaRunner(config)
    try:
        result = runner.run(plan.sql)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    est = estimate_cost(result.bytes_scanned, args.price_per_tb)
    after = catalog.sum_s3_size(args.location)
    print(f"created {plan.target_table} ({result.state}).", file=sys.stderr)
    print(
        f"source read: {human_bytes(result.bytes_scanned)} "
        f"(CTAS cost ${est.cost_usd:.6f})",
        file=sys.stderr,
    )
    if before and after:
        ratio = before / after if after else 0
        print(
            f"size: {human_bytes(before)} -> {human_bytes(after)} "
            f"({ratio:.1f}x smaller)",
            file=sys.stderr,
        )
    return 0


def cmd_partitions(args: argparse.Namespace) -> int:
    from athena_toolkit.schema import Catalog

    config = _config_from_args(args)
    catalog = Catalog(config)
    info = catalog.describe(args.table)
    headers = [c.name for c in info["partitions"]] or ["partition_values"]
    values = catalog.list_partitions(args.table, limit=args.limit)
    print(render(headers, values, args.format))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from athena_toolkit.core import AthenaRunner

    config = _config_from_args(args)
    runner = AthenaRunner(config)
    headers = ["execution_id", "state", "type", "scanned", "est_cost_usd", "submitted"]
    rows: list[list[Any]] = []
    for ex in runner.recent_executions(limit=args.limit):
        stats = ex.get("Statistics", {})
        est = estimate_cost(stats.get("DataScannedInBytes", 0), args.price_per_tb)
        status = ex.get("Status", {})
        submitted = status.get("SubmissionDateTime")
        rows.append([
            ex.get("QueryExecutionId", ""),
            status.get("State", ""),
            ex.get("StatementType", ""),
            human_bytes(est.bytes_scanned),
            f"{est.cost_usd:.6f}",
            submitted.isoformat() if hasattr(submitted, "isoformat") else str(submitted or ""),
        ])
    print(render(headers, rows, args.format))
    if rows:
        total = sum(float(r[4]) for r in rows)
        print(f"\ntotal est. cost across {len(rows)} queries: ${total:.6f}", file=sys.stderr)
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    est = estimate_cost(args.bytes_scanned, args.price_per_tb)
    print(f"bytes scanned : {est.bytes_scanned} ({human_bytes(est.bytes_scanned)})")
    print(f"billed bytes  : {est.billed_bytes} ({human_bytes(est.billed_bytes)})")
    print(f"price per TB  : ${est.price_per_tb:.2f}")
    print(f"estimated cost: ${est.cost_usd:.6f}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    data = config.to_dict()
    rows = [[k, "" if v is None else str(v)] for k, v in data.items()]
    print(render(["setting", "value"], rows, "table"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
