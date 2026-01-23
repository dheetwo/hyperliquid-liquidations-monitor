"""
Shared Utilities
================

This package contains shared utility functions used across the project.
"""

from .paths import (
    PROJECT_ROOT,
    DATA_DIR,
    DATA_RAW_DIR,
    DATA_PROCESSED_DIR,
    LOGS_DIR,
    ALLOWED_DATA_DIRS,
    validate_file_path,
    ensure_directories,
    get_data_path,
)

from .csv_helpers import (
    sanitize_csv_value,
    load_csv,
    save_csv,
)

from .prices import (
    HYPERLIQUID_API,
    ALL_DEXES,
    fetch_all_mark_prices,
    fetch_all_mark_prices_async,
    get_current_price,
)

__all__ = [
    # Path utilities
    "PROJECT_ROOT",
    "DATA_DIR",
    "DATA_RAW_DIR",
    "DATA_PROCESSED_DIR",
    "LOGS_DIR",
    "ALLOWED_DATA_DIRS",
    "validate_file_path",
    "ensure_directories",
    "get_data_path",
    # CSV utilities
    "sanitize_csv_value",
    "load_csv",
    "save_csv",
    # Price utilities
    "HYPERLIQUID_API",
    "ALL_DEXES",
    "fetch_all_mark_prices",
    "fetch_all_mark_prices_async",
    "get_current_price",
]
