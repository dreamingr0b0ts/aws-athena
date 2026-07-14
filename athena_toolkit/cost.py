"""Cost estimation from Athena query-execution statistics.

Athena (per-query, standard SQL) is billed by the amount of data scanned,
rounded *up* to the nearest 10 MB per query, at a per-TB rate. There is no
charge for DDL statements or for failed queries, and the 10 MB minimum applies
per query. The rate varies by region, so it is configurable.

Reference price used as the default: $5.00 per TB scanned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_PRICE_PER_TB = 5.0
_BYTES_PER_TB = 1_000_000_000_000  # Athena/AWS bill in decimal TB (10^12).
_MIN_BILLED_BYTES = 10 * 1_000_000   # 10 MB minimum per query.


@dataclass
class CostEstimate:
    bytes_scanned: int
    billed_bytes: int
    price_per_tb: float
    cost_usd: float

    @property
    def megabytes_scanned(self) -> float:
        return self.bytes_scanned / 1_000_000

    @property
    def gigabytes_scanned(self) -> float:
        return self.bytes_scanned / 1_000_000_000


def estimate_cost(
    bytes_scanned: int | None,
    price_per_tb: float = DEFAULT_PRICE_PER_TB,
    *,
    apply_minimum: bool = True,
) -> CostEstimate:
    """Estimate the USD cost of a query given the bytes it scanned.

    Args:
        bytes_scanned: Data scanned, from ``Statistics.DataScannedInBytes``.
            ``None`` or 0 (e.g. DDL / fully-cached) yields a zero-cost estimate.
        price_per_tb: Region-specific price per TB scanned.
        apply_minimum: Apply Athena's 10 MB per-query billing minimum. Only
            applied when some data was actually scanned.
    """
    scanned = int(bytes_scanned or 0)
    if scanned <= 0:
        return CostEstimate(0, 0, price_per_tb, 0.0)
    billed = max(scanned, _MIN_BILLED_BYTES) if apply_minimum else scanned
    cost = billed / _BYTES_PER_TB * price_per_tb
    return CostEstimate(scanned, billed, price_per_tb, cost)


def human_bytes(num: int | float | None) -> str:
    """Format a byte count using decimal (SI) units, matching AWS billing."""
    n = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000.0 or unit == "PB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1000.0
    return f"{n:.2f} PB"


_SIZE_UNITS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "PB": 1_000_000_000_000_000,
}


def parse_size(text: str) -> int:
    """Parse a human size like ``500MB``, ``1.5 GB``, or ``1048576`` into bytes.

    Uses decimal units (1 KB = 1000 B) to match AWS billing. A bare number is
    interpreted as bytes.
    """
    s = str(text).strip().upper().replace(" ", "")
    if not s:
        raise ValueError("empty size")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]?B)?", s)
    if not match:
        raise ValueError(f"invalid size: {text!r} (try e.g. 500MB, 1.5GB, 1048576)")
    value, unit = match.group(1), match.group(2) or "B"
    return int(float(value) * _SIZE_UNITS[unit])
