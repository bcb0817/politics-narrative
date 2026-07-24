"""Single source of truth for xAI cost-unit conversion."""

from __future__ import annotations

TICKS_PER_USD = 10_000_000_000


def ticks_to_usd(cost_in_usd_ticks: int | float | None) -> float:
    """Convert one response's xAI cost ticks to USD."""
    return max(0, int(cost_in_usd_ticks or 0)) / TICKS_PER_USD
