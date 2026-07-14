"""Generate Athena **partition projection** properties from an existing table.

Partition projection lets Athena compute partition locations from rules in the
table's ``TBLPROPERTIES`` instead of listing them in the Glue catalog. That
removes the need for ``MSCK REPAIR``/``ADD PARTITION`` *and* avoids the
"partitioned table, no partition filter → full scan" trap, because Athena only
materialises the partitions a query actually needs.

This module inspects a table's partition keys and a sample of its existing
partition values (from Glue), infers a sensible projection type per key
(``date`` / ``integer`` / ``enum``), derives the ``storage.location.template``,
and emits an ``ALTER TABLE ... SET TBLPROPERTIES`` statement.

The inference is a starting point — review the ranges before applying, since a
``date``/``integer`` range derived from today's data won't auto-extend beyond
what you set (use an open-ended range like ``NOW-3YEARS,NOW`` for dates that
keep growing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from athena_toolkit.config import AthenaConfig
from athena_toolkit.schema import Catalog

_INT_TYPES = {"tinyint", "smallint", "int", "integer", "bigint"}

# Separator-bearing date layouts we can recognise from a partition value.
_DATE_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "yyyy-MM-dd", "DAYS"),
    (re.compile(r"^\d{4}/\d{2}/\d{2}$"), "yyyy/MM/dd", "DAYS"),
    (re.compile(r"^\d{4}-\d{2}$"), "yyyy-MM", "MONTHS"),
]


@dataclass
class ProjectionColumn:
    name: str
    type: str  # date | integer | enum
    properties: dict[str, str] = field(default_factory=dict)

    def tblproperties(self) -> dict[str, str]:
        out = {f"projection.{self.name}.type": self.type}
        for k, v in self.properties.items():
            out[f"projection.{self.name}.{k}"] = v
        return out


@dataclass
class ProjectionPlan:
    database: str
    table: str
    columns: list[ProjectionColumn]
    location_template: str | None
    notes: list[str] = field(default_factory=list)

    def tblproperties(self) -> dict[str, str]:
        props: dict[str, str] = {"projection.enabled": "true"}
        for col in self.columns:
            props.update(col.tblproperties())
        if self.location_template:
            props["storage.location.template"] = self.location_template
        return props

    def to_alter_sql(self) -> str:
        items = ",\n".join(
            f"  '{k}' = '{v}'" for k, v in self.tblproperties().items()
        )
        return (
            f"ALTER TABLE `{self.database}`.`{self.table}` SET TBLPROPERTIES (\n"
            f"{items}\n);"
        )


def _detect_date_format(values: list[str]) -> str | None:
    """Return a Java date format if every value matches one date layout."""
    if not values:
        return None
    for pattern, fmt, _unit in _DATE_PATTERNS:
        if all(pattern.match(v) for v in values):
            return fmt
    # yyyyMMdd: 8 digits with a plausible leading year.
    if all(re.match(r"^\d{8}$", v) for v in values) and all(
        1900 <= int(v[:4]) <= 2100 for v in values
    ):
        return "yyyyMMdd"
    return None


def _unit_for_format(fmt: str) -> str:
    return "MONTHS" if fmt == "yyyy-MM" else "DAYS"


def _is_zero_padded(values: list[str]) -> int | None:
    """If values are fixed-width with a leading zero somewhere, return width."""
    widths = {len(v) for v in values}
    if len(widths) == 1 and any(v[0] == "0" and len(v) > 1 for v in values):
        return widths.pop()
    return None


def infer_projection_column(
    name: str, glue_type: str, values: list[str]
) -> ProjectionColumn:
    """Infer a projection type + properties for one partition key."""
    gtype = (glue_type or "").lower()
    vals = [v for v in values if v not in (None, "")]

    fmt = _detect_date_format(vals)
    if gtype in ("date", "timestamp") or fmt:
        fmt = fmt or "yyyy-MM-dd"
        rng = f"{min(vals)},{max(vals)}" if vals else "NOW-3YEARS,NOW"
        return ProjectionColumn(
            name,
            "date",
            {
                "format": fmt,
                "range": rng,
                "interval": "1",
                "interval.unit": _unit_for_format(fmt),
            },
        )

    is_int = gtype in _INT_TYPES or (bool(vals) and all(
        re.fullmatch(r"-?\d+", v) for v in vals
    ))
    if is_int:
        ints = [int(v) for v in vals] if vals else [0]
        props = {
            "range": f"{min(ints)},{max(ints)}",
            "interval": "1",
        }
        width = _is_zero_padded(vals)
        if width:
            props["digits"] = str(width)
        return ProjectionColumn(name, "integer", props)

    # Fallback: enum of the observed distinct values.
    distinct = sorted(set(vals))
    return ProjectionColumn(name, "enum", {"values": ",".join(distinct)})


def build_location_template(
    table_location: str | None,
    keys: list[str],
    sample_location: str | None,
    sample_values: list[str],
) -> str | None:
    """Derive ``storage.location.template`` for the partition keys.

    Prefers rewriting a real sample partition location (so both Hive-style
    ``k=v`` and bare-segment layouts work) by replacing each value with
    ``${key}``. Falls back to Hive-style paths under the table location.
    """
    base = (table_location or "").rstrip("/")
    if sample_location and sample_values and len(sample_values) == len(keys):
        loc = sample_location.rstrip("/")
        prefix, suffix = "", loc
        if base and loc.startswith(base):
            prefix, suffix = base, loc[len(base):]
        for key, value in zip(keys, sample_values):
            if value:
                suffix = suffix.replace(value, "${" + key + "}", 1)
        return prefix + suffix
    if base:
        return base + "/" + "/".join(f"{k}=${{{k}}}" for k in keys)
    return None


class ProjectionGenerator:
    def __init__(self, config: AthenaConfig, catalog: Catalog | None = None):
        self.config = config
        self.catalog = catalog or Catalog(config)

    def generate(
        self, table: str, database: str | None = None, sample: int = 500
    ) -> ProjectionPlan:
        db = database or self.config.database
        meta = self.catalog.get_table(table, db)
        partition_keys = meta.get("PartitionKeys", [])
        notes: list[str] = []
        if not partition_keys:
            raise ValueError(
                f"Table {table} has no partition keys; partition projection "
                f"only applies to partitioned tables."
            )

        table_location = meta.get("StorageDescriptor", {}).get("Location")
        details = self.catalog.iter_partition_details(table, db)
        if sample and len(details) > sample:
            details = details[:sample]
            notes.append(
                f"Inference based on the first {sample} partitions; "
                f"widen ranges if your data extends beyond them."
            )
        if not details:
            notes.append(
                "No existing partitions found in Glue; ranges/enums are "
                "placeholders — edit them before applying."
            )

        # Column-wise observed values (positional, aligned to partition_keys).
        per_key_values: list[list[str]] = [[] for _ in partition_keys]
        for p in details:
            vals = p.get("values", [])
            for i in range(min(len(vals), len(partition_keys))):
                per_key_values[i].append(vals[i])

        columns = [
            infer_projection_column(k["Name"], k.get("Type", ""), per_key_values[i])
            for i, k in enumerate(partition_keys)
        ]

        sample_detail = details[0] if details else None
        template = build_location_template(
            table_location,
            [k["Name"] for k in partition_keys],
            sample_detail.get("location") if sample_detail else None,
            sample_detail.get("values") if sample_detail else [],
        )
        if not template:
            notes.append(
                "Could not derive storage.location.template (no table/sample "
                "location); set it manually."
            )

        return ProjectionPlan(
            database=db or "",
            table=table,
            columns=columns,
            location_template=template,
            notes=notes,
        )
