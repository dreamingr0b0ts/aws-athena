"""Catalog and schema introspection via the Glue Data Catalog.

These helpers read metadata (databases, tables, columns, partitions) without
running billable Athena queries, and can generate ``CREATE EXTERNAL TABLE``
DDL from existing table metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena_toolkit.client import AwsClients
from athena_toolkit.config import AthenaConfig


@dataclass
class Column:
    name: str
    type: str
    comment: str | None = None


class Catalog:
    """Thin wrapper over Glue for metadata operations."""

    def __init__(self, config: AthenaConfig, clients: AwsClients | None = None):
        self.config = config
        self.clients = clients or AwsClients(config)

    def list_databases(self) -> list[str]:
        names: list[str] = []
        paginator = self.clients.glue.get_paginator("get_databases")
        for page in paginator.paginate():
            names.extend(db["Name"] for db in page.get("DatabaseList", []))
        return names

    def list_tables(self, database: str | None = None) -> list[str]:
        db = self._require_db(database)
        names: list[str] = []
        paginator = self.clients.glue.get_paginator("get_tables")
        for page in paginator.paginate(DatabaseName=db):
            names.extend(t["Name"] for t in page.get("TableList", []))
        return names

    def get_table(self, table: str, database: str | None = None) -> dict[str, Any]:
        db = self._require_db(database)
        return self.clients.glue.get_table(DatabaseName=db, Name=table)["Table"]

    def describe(self, table: str, database: str | None = None) -> dict[str, list[Column]]:
        """Return data columns and partition columns for a table."""
        meta = self.get_table(table, database)
        sd = meta.get("StorageDescriptor", {})
        cols = [
            Column(c["Name"], c["Type"], c.get("Comment"))
            for c in sd.get("Columns", [])
        ]
        parts = [
            Column(c["Name"], c["Type"], c.get("Comment"))
            for c in meta.get("PartitionKeys", [])
        ]
        return {"columns": cols, "partitions": parts}

    def list_partitions(
        self, table: str, database: str | None = None, limit: int = 100
    ) -> list[list[str]]:
        """Return partition value tuples for a table (up to ``limit``)."""
        db = self._require_db(database)
        values: list[list[str]] = []
        paginator = self.clients.glue.get_paginator("get_partitions")
        for page in paginator.paginate(DatabaseName=db, TableName=table):
            for p in page.get("Partitions", []):
                values.append(p.get("Values", []))
                if len(values) >= limit:
                    return values
        return values

    def iter_partition_details(
        self, table: str, database: str | None = None
    ) -> "list[dict[str, Any]]":
        """Return each partition's values and S3 location.

        Each item: ``{"values": [...], "location": "s3://..."}`` where values
        are positional, matching the order of the table's PartitionKeys.
        """
        db = self._require_db(database)
        out: list[dict[str, Any]] = []
        paginator = self.clients.glue.get_paginator("get_partitions")
        for page in paginator.paginate(DatabaseName=db, TableName=table):
            for p in page.get("Partitions", []):
                out.append(
                    {
                        "values": p.get("Values", []),
                        "location": p.get("StorageDescriptor", {}).get("Location"),
                    }
                )
        return out

    def sum_s3_size(self, s3_uri: str | None) -> int:
        """Sum the size in bytes of all objects under an ``s3://`` prefix.

        Returns 0 for an empty/missing URI. Hidden Hive/Spark marker files
        (``_$folder$``, ``_SUCCESS``, and ``.``-prefixed) are ignored.
        """
        if not s3_uri or not s3_uri.startswith("s3://"):
            return 0
        without_scheme = s3_uri[len("s3://") :]
        bucket, _, prefix = without_scheme.partition("/")
        total = 0
        paginator = self.clients.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                base = key.rsplit("/", 1)[-1]
                if base.startswith(".") or base.startswith("_") or "_$folder$" in key:
                    continue
                total += int(obj.get("Size", 0))
        return total

    def generate_ddl(self, table: str, database: str | None = None) -> str:
        """Build a ``CREATE EXTERNAL TABLE`` statement from Glue metadata."""
        db = self._require_db(database)
        meta = self.get_table(table, database)
        sd = meta.get("StorageDescriptor", {})

        def col_lines(cols: list[dict[str, Any]]) -> str:
            rendered = []
            for c in cols:
                line = f"  `{c['Name']}` {c['Type']}"
                if c.get("Comment"):
                    line += f" COMMENT '{c['Comment']}'"
                rendered.append(line)
            return ",\n".join(rendered)

        parts = [f"CREATE EXTERNAL TABLE `{db}`.`{table}` ("]
        parts.append(col_lines(sd.get("Columns", [])))
        parts.append(")")

        pk = meta.get("PartitionKeys", [])
        if pk:
            parts.append("PARTITIONED BY (")
            parts.append(col_lines(pk))
            parts.append(")")

        serde = sd.get("SerdeInfo", {})
        if serde.get("SerializationLibrary"):
            parts.append(f"ROW FORMAT SERDE '{serde['SerializationLibrary']}'")
            serde_params = serde.get("Parameters", {})
            if serde_params:
                kv = ",\n".join(
                    f"  '{k}' = '{v}'" for k, v in serde_params.items()
                )
                parts.append("WITH SERDEPROPERTIES (")
                parts.append(kv)
                parts.append(")")

        if sd.get("InputFormat"):
            parts.append("STORED AS INPUTFORMAT")
            parts.append(f"  '{sd['InputFormat']}'")
            parts.append("OUTPUTFORMAT")
            parts.append(f"  '{sd.get('OutputFormat', '')}'")

        if sd.get("Location"):
            parts.append(f"LOCATION\n  '{sd['Location']}'")

        tbl_params = meta.get("Parameters", {})
        if tbl_params:
            kv = ",\n".join(f"  '{k}' = '{v}'" for k, v in tbl_params.items())
            parts.append("TBLPROPERTIES (")
            parts.append(kv)
            parts.append(")")

        return "\n".join(parts) + ";"

    def _require_db(self, database: str | None) -> str:
        db = database or self.config.database
        if not db:
            raise ValueError(
                "No database specified. Pass one explicitly or set 'database' "
                "in your config / via --database."
            )
        return db
