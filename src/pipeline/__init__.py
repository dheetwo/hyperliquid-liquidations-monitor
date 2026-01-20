"""
Data Pipeline
=============

This package contains the 3-step data pipeline:
- Step 1: Cohort Scraper - fetch wallet addresses from Hyperdash
- Step 2: Position Scraper - fetch positions from Hyperliquid
- Step 3: Liquidation Filter - calculate hunting metrics

Each step can be run independently or orchestrated by the monitor service.
"""

# Step 1: Cohort scraper
from .step1_cohort import (
    fetch_cohorts,
    fetch_cohort_data,
    save_to_csv as save_cohort_csv,
    process_trader,
    PRIORITY_COHORTS,
    SECONDARY_COHORTS,
    ALL_COHORTS,
)

# Step 2: Position scraper
from .step2_position import (
    load_cohort_addresses,
    fetch_all_positions,
    fetch_all_positions_async,
    fetch_all_positions_for_address,
    fetch_all_mark_prices,
    fetch_all_mark_prices_async,
    save_to_csv as save_position_csv,
    run_scan_mode,
    run_cohort_scan,
    parse_position,
    SCAN_MODES,
    HIGH_PRIORITY_COHORTS,
    NORMAL_COHORTS,
    ALL_DEXES,
    CORE_DEXES,
)

# Step 3: Liquidation filter
from .step3_filter import (
    filter_positions,
    calculate_distance_to_liquidation,
    calculate_estimated_liquidatable_value,
    calculate_notional_to_trigger,
    calculate_price_impact,
    fetch_order_book,
    fetch_order_books_for_tokens,
    CROSS_POSITION_LIQUIDATABLE_RATIO,
    ORDERBOOK_DELAY,
)

__all__ = [
    # Step 1
    "fetch_cohorts",
    "fetch_cohort_data",
    "save_cohort_csv",
    "process_trader",
    "PRIORITY_COHORTS",
    "SECONDARY_COHORTS",
    "ALL_COHORTS",
    # Step 2
    "load_cohort_addresses",
    "fetch_all_positions",
    "fetch_all_positions_async",
    "fetch_all_positions_for_address",
    "fetch_all_mark_prices",
    "fetch_all_mark_prices_async",
    "save_position_csv",
    "run_scan_mode",
    "run_cohort_scan",
    "parse_position",
    "SCAN_MODES",
    "HIGH_PRIORITY_COHORTS",
    "NORMAL_COHORTS",
    "ALL_DEXES",
    "CORE_DEXES",
    # Step 3
    "filter_positions",
    "calculate_distance_to_liquidation",
    "calculate_estimated_liquidatable_value",
    "calculate_notional_to_trigger",
    "calculate_price_impact",
    "fetch_order_book",
    "fetch_order_books_for_tokens",
    "CROSS_POSITION_LIQUIDATABLE_RATIO",
    "ORDERBOOK_DELAY",
]
