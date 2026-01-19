"""
Scrapers Package (DEPRECATED)
=============================

DEPRECATED: This package has been moved to src.pipeline.
Please update imports to use:
    from src.pipeline import fetch_cohorts, fetch_all_positions, ...

This module provides backward compatibility by re-exporting from the new location.
"""

import warnings

warnings.warn(
    "src.scrapers is deprecated, use src.pipeline instead. "
    "Example: from src.pipeline import fetch_cohorts, fetch_all_positions",
    DeprecationWarning,
    stacklevel=2
)

# Re-export from new location for backward compatibility
from src.pipeline.step1_cohort import (
    fetch_cohorts,
    fetch_cohort_data,
    save_to_csv as save_cohort_csv,
    PRIORITY_COHORTS,
    ALL_COHORTS,
)
from src.pipeline.step2_position import (
    load_cohort_addresses,
    fetch_all_mark_prices,
    fetch_all_mark_prices_async,
    fetch_all_positions,
    fetch_all_positions_async,
    fetch_all_positions_for_address,
    save_to_csv as save_position_csv,
    run_scan_mode,
    SCAN_MODES,
)

__all__ = [
    # Cohort
    "fetch_cohorts",
    "fetch_cohort_data",
    "save_cohort_csv",
    "PRIORITY_COHORTS",
    "ALL_COHORTS",
    # Position
    "load_cohort_addresses",
    "fetch_all_mark_prices",
    "fetch_all_mark_prices_async",
    "fetch_all_positions",
    "fetch_all_positions_async",
    "fetch_all_positions_for_address",
    "save_position_csv",
    "run_scan_mode",
    "SCAN_MODES",
]
