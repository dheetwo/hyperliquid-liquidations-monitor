"""
Filters Package (DEPRECATED)
============================

DEPRECATED: This package has been moved to src.pipeline.
Please update imports to use:
    from src.pipeline import filter_positions, calculate_distance_to_liquidation, ...

This module provides backward compatibility by re-exporting from the new location.
"""

import warnings

warnings.warn(
    "src.filters is deprecated, use src.pipeline instead. "
    "Example: from src.pipeline import filter_positions",
    DeprecationWarning,
    stacklevel=2
)

# Re-export from new location for backward compatibility
from src.pipeline.step3_filter import (
    filter_positions,
    calculate_distance_to_liquidation,
)
from src.utils.prices import (
    get_current_price,
    fetch_all_mark_prices,
)

__all__ = [
    "filter_positions",
    "calculate_distance_to_liquidation",
    "get_current_price",
    "fetch_all_mark_prices",
]
