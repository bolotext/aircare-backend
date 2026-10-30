"""Shared "N/A" rendering used by the dashboard JSON's presentation layer, the
CSV export, and (Phase 3) the HTML email — so an empty-denominator ratio
reads as N/A identically everywhere, never as 0 or a crash.
"""


def format_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}%"


def format_seconds(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}s"
