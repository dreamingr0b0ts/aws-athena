# athena-toolkit

A small, dependency-light **boto3 CLI toolkit** for exploring and operating
**AWS Athena** across multiple environments. It wraps the awkward parts of
Athena (submit → poll → fetch, result paging, the echoed header row, the 10 MB
billing minimum, Glue metadata) behind a handful of clean commands.

## Features

- **Multi-environment config** — define `dev`, `prod`, etc. in one TOML file and
  switch with `--env`. Every setting can be overridden by an env var or a CLI flag.
- **Query runner** — submit SQL, poll to completion, and print results as a
  **table, CSV, or JSON**. Reads SQL inline, from a file, or from stdin.
- **Cost awareness** — every query prints how much data it scanned and an
  estimated cost (10 MB-per-query minimum, configurable $/TB). A `cost` command
  and a `history` view are included for after-the-fact reporting.
- **Pre-flight cost guardrail** — estimate how many bytes a query will scan
  *before* running it (from Glue + S3), detect missing partition pruning, and
  optionally **block** queries that exceed a scan or cost budget. See below.
- **Catalog introspection** — list databases/tables, describe columns and
  partition keys, list partition values, and generate `CREATE EXTERNAL TABLE`
  DDL — all from the Glue Data Catalog, with **no billable queries**.
- **Partition projection generator** — infer `projection.*` TBLPROPERTIES
  (date / integer / enum) and a `storage.location.template` from an existing
  table's partitions, and emit the `ALTER TABLE` to enable it. See below.
- **CTAS converter** — turn a raw CSV/JSON table into compressed, partitioned
  Parquet/ORC with one command (partition columns placed last automatically).
  Usually the single biggest cost reduction you can make. See below.

## Install

```bash
pip install -e .            # from this directory
# or for development / tests:
pip install -e ".[dev]"
```

This installs two equivalent console scripts: `athena-toolkit` and `athq`.

## Configure

Copy the example and edit it:

```bash
cp athena.toml.example athena.toml
```

The config file is searched for in this order (first match wins):

1. `--config <path>` or `$ATHENA_TOOLKIT_CONFIG`
2. `./athena.toml`
3. `~/.config/athena-toolkit/config.toml`

**Resolution precedence** (highest first): CLI flag → env var → selected
`[environments.<name>]` → `[defaults]` → built-in defaults.

Per-setting environment variables: `ATHENA_PROFILE`, `ATHENA_REGION`,
`ATHENA_WORKGROUP`, `ATHENA_OUTPUT_LOCATION`, `ATHENA_DATABASE`,
`ATHENA_CATALOG`. Select an environment with `--env` or `$ATHENA_TOOLKIT_ENV`.

Check what a given environment resolves to:

```bash
athq config --env prod
```

## Usage

```bash
# Run a query (inline, from a file, or from stdin)
athq query "SELECT * FROM logs LIMIT 10" --env dev
athq query -f report.sql --format csv > out.csv
echo "SELECT count(*) FROM logs" | athq query -f - --format json

# Explore the catalog (uses Glue, no query cost)
athq databases
athq tables analytics
athq describe logs --database analytics
athq ddl logs
athq partitions logs --limit 50

# Cost reporting
athq cost 1500000000                 # estimate cost for ~1.5 GB scanned
athq history --limit 20              # recent executions + per-query/total cost
```

Result output goes to **stdout**; the cost/timing footer goes to **stderr**, so
piping results to a file stays clean. Add `--no-cost` to silence the footer.

## Pre-flight cost guardrail

Athena has no native dry-run that reports bytes scanned — but bytes scanned is
exactly what you pay for. The `preflight` command estimates an **upper bound**
on a query's scan *before* you run it, by reading the referenced tables from
Glue, applying any partition-key filters in the `WHERE` clause, and summing the
matching S3 objects.

```bash
# Estimate scan + cost without running anything
athq preflight "SELECT * FROM events WHERE dt = '2024-01-02'"
athq preflight -f report.sql --format json

# Guardrail on the real query: block accidental full scans
athq query "SELECT * FROM events" --dry-run            # show estimate, don't run
athq query "SELECT * FROM events" --max-scan 5GB       # block if estimate > 5 GB
athq query "SELECT * FROM events" --max-cost 1.00      # block if estimate > $1
athq query "SELECT * FROM events" --max-scan 5GB --force   # estimate, warn, run anyway
```

A blocked query exits with code **3**. Example output:

```
table     | partitioned | partitions | pruning        | est. scan
----------+-------------+------------+----------------+----------
db.events | True        | 3/3        | NO (full scan) | 3.00 GB
estimated total scan: 3.00 GB  (upper bound)
estimated cost      : $0.015000 @ $5.00/TB
warning: [events] partitioned by ['dt'] but no partition filter found ...
```

The estimate is deliberately **conservative** (it assumes a full scan when in
doubt), so the guardrail errs toward blocking rather than letting an expensive
query slip through. Caveats — all of which push the estimate *up*: pruning is
only detected from equality/`IN` predicates on partition columns in the `WHERE`
clause; columnar formats (Parquet/ORC) usually scan *less* than file size via
column projection; subqueries/CTEs and join-based pruning are not resolved.

## Partition projection

Partition projection lets Athena compute partition locations from rules in the
table's `TBLPROPERTIES` instead of listing them in Glue — no more `MSCK REPAIR`,
and no accidental full scans. The `projection` command inspects an existing
table's partitions and emits the `ALTER TABLE` to enable projection:

```bash
athq projection events --database analytics
athq projection events --format json     # just the TBLPROPERTIES map
```

It infers a type per partition key — `date` (with format + range), `integer`
(with range, and `digits` for zero-padded values), or `enum` (distinct values)
— and derives `storage.location.template` from a sample partition location
(works for both `key=value` and bare-segment layouts). Example output:

```sql
ALTER TABLE `analytics`.`events` SET TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.dt.type' = 'date',
  'projection.dt.format' = 'yyyy-MM-dd',
  'projection.dt.range' = '2024-01-01,2024-06-30',
  'projection.dt.interval' = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'projection.region.type' = 'enum',
  'projection.region.values' = 'eu,us',
  'storage.location.template' = 's3://b/events/dt=${dt}/region=${region}'
);
```

Review the ranges before applying: a range inferred from today's data won't
auto-extend, so for dates that keep growing use an open-ended range such as
`NOW-3YEARS,NOW`.

## CTAS converter (raw → Parquet)

Storing data as compressed, partitioned Parquet/ORC is usually the biggest
Athena cost win. The `convert` command reads a source table's schema from Glue
and builds a correct `CREATE TABLE AS SELECT`, automatically placing partition
columns last (an Athena requirement):

```bash
# Print the CTAS (dry run) — review before executing
athq convert raw_logs logs_parquet --location s3://b/curated/logs/

# Actually run it and report the size reduction
athq convert raw_logs logs_parquet --location s3://b/curated/logs/ --run
athq convert raw_logs logs_orc --location s3://b/curated/orc/ \
    --format ORC --compression ZLIB --partitioned-by dt,region --run
```

```sql
CREATE TABLE "analytics"."logs_parquet"
WITH (
  format = 'PARQUET',
  parquet_compression = 'SNAPPY',
  external_location = 's3://b/curated/logs/',
  partitioned_by = ARRAY['dt', 'region']
)
AS SELECT "id", "msg", "ts", "dt", "region"
FROM "analytics"."raw_logs";
```

With `--run`, the converter executes the CTAS and reports the source bytes read
(and CTAS cost) plus the before→after on-disk size ratio. Caveats: the target
`external_location` must be empty and the target table must not exist; a single
CTAS creates at most 100 partitions (use `INSERT INTO` for more).

## Cost model

Athena bills per TB of data scanned, rounded up to a **10 MB minimum per
query**, with **no charge for DDL or failed queries**. The default rate is
**$5.00/TB**; override per command with `--price-per-tb`. Estimates use decimal
units (1 TB = 10¹² bytes) to match AWS billing. These are estimates — always
confirm against your AWS bill / Cost Explorer.

## Project layout

```
athena_toolkit/
  config.py       # multi-environment config resolution
  client.py       # boto3 session + lazy client factory
  core.py         # AthenaRunner: submit / wait / fetch / history
  cost.py         # cost estimation + byte formatting
  schema.py       # Glue catalog introspection + DDL generation
  preflight.py    # pre-flight scan/cost estimator + budget guardrail
  projection.py   # partition-projection TBLPROPERTIES generator
  convert.py      # CTAS builder: raw -> partitioned Parquet/ORC
  formatting.py   # table / csv / json rendering
  cli.py          # argparse entry point
tests/            # unit tests (config, cost, formatting, core w/ fake client)
```

## Testing

```bash
python -m pytest
```

Tests use a hand-rolled fake AWS client (`tests/test_core.py`), so they run
**offline with no credentials**.

## Notes & limitations

- Authentication uses your standard AWS credential chain (profiles, env vars,
  SSO, instance roles). The toolkit never stores credentials.
- `tables`/`describe`/`ddl`/`partitions` read from Glue; tables defined only in
  Hive/external metastores won't appear.
- Cost figures are estimates, not billing truth.
