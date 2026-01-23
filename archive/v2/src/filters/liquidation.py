"""
Liquidation Filter (DEPRECATED)
===============================

DEPRECATED: This module has been moved to src.pipeline.step3_filter.
Please use the new location instead.

This file is kept for backward compatibility with direct module invocation.
Running this module will delegate to src.pipeline.step3_filter.
"""

import warnings

warnings.warn(
    "src.filters.liquidation is deprecated. Use src.pipeline.step3_filter instead.",
    DeprecationWarning,
    stacklevel=2
)

# Re-export from new location
from src.pipeline.step3_filter import (
    filter_positions,
    calculate_distance_to_liquidation,
    calculate_estimated_liquidatable_value,
    CROSS_POSITION_LIQUIDATABLE_RATIO,
    main,
)

__all__ = [
    "filter_positions",
    "calculate_distance_to_liquidation",
    "calculate_estimated_liquidatable_value",
    "CROSS_POSITION_LIQUIDATABLE_RATIO",
]

if __name__ == "__main__":
    main()
