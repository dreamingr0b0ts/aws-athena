"""Convert a raw table (CSV/JSON/text) into partitioned, compressed columnar
storage (Parquet or ORC) using Athena ``CREATE TABLE AS SELECT`` (CTAS).

Storing data as compressed, partitioned Parquet/ORC is usually the single
biggest Athena cost win: queries scan far fewer bytes thanks to columnar
projection, compression, and partition pruning.

This module reads the source table's schema from Glue and builds a correct
CTAS statement. Two Athena rules are handled for you:

  * **Partition columns must be listed last** in the ``SELECT``. We reorder the
    projection so data columns come first and partition columns come last.
  * Format/compression are set via the ``WITH`` properties block, and the
    output location is set with ``external_location``.

Caveats: the target ``external_location`` must be empty and the target table
must not already exist; a single CTAS can create at most 100 partitions (use an
``INSERT INTO`` loop or bucketing for more).
"""

from __future__ import annotations

from dataclasses import dataclass

from athena_toolkit.config import AthenaConfig
from athena_toolkit.schema import Catalog

_PARQUET_COMPRESSION = {"SNAPPY", "GZIP", "ZSTD", "NONE"}
_ORC_COMPRESSION = {"SNAPPY", "ZLIB", "LZ4", "ZSTD", "NONE"}


@dataclass
class CtasPlan:
    source_database: str
    source_table: str
    target_database: str
    target_table: str
    fmt: str
    compression: str
    external_location: str
    data_columns: list[str]
    partition_columns: list[str]
    sql: str


def _quote_ident(name: str) -> str:
    # Athena/Trino identifiers are double-quoted; escape embedded quotes.
    return '"' + name.replace('"', '""') + '"'


def _qualified(db: str | None, table: str) -> str:
    return f"{_quote_ident(db)}.{_quote_ident(table)}" if db else _quote_ident(table)


def _compression_property(fmt: str, compression: str) -> str:
    fmt = fmt.upper()
    if fmt == "PARQUET":
        return "parquet_compression"
    if fmt == "ORC":
        return "orc_compression"
    return "write_compression"


def build_ctas(
    catalog: Catalog,
    source_table: str,
    target_table: str,
    external_location: str,
    *,
    source_database: str | None = None,
    target_database: str | None = None,
    fmt: str = "PARQUET",
    compression: str = "SNAPPY",
    partitioned_by: list[str] | None = None,
) -> CtasPlan:
    """Build a CTAS plan that rewrites ``source_table`` into columnar storage."""
    fmt = fmt.upper()
    compression = compression.upper()
    if fmt not in ("PARQUET", "ORC"):
        raise ValueError(f"format must be PARQUET or ORC, got {fmt!r}")
    valid = _PARQUET_COMPRESSION if fmt == "PARQUET" else _ORC_COMPRESSION
    if compression not in valid:
        raise ValueError(
            f"compression {compression!r} not valid for {fmt} "
            f"(choose from {sorted(valid)})"
        )
    if not external_location.startswith("s3://"):
        raise ValueError("external_location must be an s3:// URI")

    src_db = source_database
    info = catalog.describe(source_table, src_db)
    data_cols = [c.name for c in info["columns"]]
    src_partition_cols = [c.name for c in info["partitions"]]

    # Default: preserve the source's partitioning; allow an explicit override.
    part_cols = partitioned_by if partitioned_by is not None else src_partition_cols

    # Athena requires partition columns to appear LAST in the SELECT, and a
    # column must not be listed twice. Partition keys may live in the source's
    # data columns (re-partitioning a flat table) or partition columns.
    all_source_cols = data_cols + [
        c for c in src_partition_cols if c not in data_cols
    ]
    ordered_data = [c for c in all_source_cols if c not in part_cols]
    select_cols = ordered_data + list(part_cols)

    select_list = ", ".join(_quote_ident(c) for c in select_cols)
    source_ref = _qualified(src_db, source_table)
    target_ref = _qualified(target_database, target_table)

    with_props = [
        f"  format = '{fmt}'",
        f"  {_compression_property(fmt, compression)} = '{compression}'",
        f"  external_location = '{external_location}'",
    ]
    if part_cols:
        arr = ", ".join(f"'{c}'" for c in part_cols)
        with_props.append(f"  partitioned_by = ARRAY[{arr}]")

    sql = (
        f"CREATE TABLE {target_ref}\n"
        f"WITH (\n" + ",\n".join(with_props) + "\n)\n"
        f"AS SELECT {select_list}\n"
        f"FROM {source_ref};"
    )

    return CtasPlan(
        source_database=src_db or "",
        source_table=source_table,
        target_database=target_database or "",
        target_table=target_table,
        fmt=fmt,
        compression=compression,
        external_location=external_location,
        data_columns=ordered_data,
        partition_columns=list(part_cols),
        sql=sql,
    )


def source_size_bytes(catalog: Catalog, source_table: str, database: str | None) -> int:
    """Best-effort size of the source table's data in S3 (for before/after)."""
    meta = catalog.get_table(source_table, database)
    return catalog.sum_s3_size(meta.get("StorageDescriptor", {}).get("Location"))
