"""
CSV Utilities
=============

Shared CSV reading, writing, and sanitization helpers.
"""

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def sanitize_csv_value(value: str) -> str:
    """
    Sanitize a CSV value to prevent CSV injection attacks.

    CSV injection occurs when values starting with =, +, -, @, tab, or carriage
    return are interpreted as formulas by spreadsheet applications.

    Args:
        value: Raw string value from CSV

    Returns:
        Sanitized string safe for spreadsheet use
    """
    if not isinstance(value, str):
        return value

    # Characters that trigger formula interpretation in Excel/Sheets
    dangerous_prefixes = ('=', '+', '-', '@', '\t', '\r', '\n')

    if value.startswith(dangerous_prefixes):
        # Prefix with single quote to force text interpretation
        return "'" + value

    return value


def load_csv(
    filepath: str,
    sanitize: bool = True
) -> List[Dict[str, Any]]:
    """
    Load a CSV file into a list of dictionaries.

    Args:
        filepath: Path to CSV file
        sanitize: If True, sanitize values to prevent CSV injection

    Returns:
        List of row dictionaries
    """
    rows = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if sanitize:
                    sanitized_row = {
                        key: sanitize_csv_value(value) if isinstance(value, str) else value
                        for key, value in row.items()
                    }
                    rows.append(sanitized_row)
                else:
                    rows.append(row)
        logger.debug(f"Loaded {len(rows)} rows from {filepath}")
    except FileNotFoundError:
        logger.error(f"CSV file not found: {filepath}")
    except csv.Error as e:
        logger.error(f"CSV parsing error in {filepath}: {e}")
    except OSError as e:
        logger.error(f"File read error for {filepath}: {e}")

    return rows


def save_csv(
    rows: List[Dict[str, Any]],
    filepath: str,
    fieldnames: List[str] = None
) -> bool:
    """
    Save a list of dictionaries to a CSV file.

    Args:
        rows: List of row dictionaries
        filepath: Output path
        fieldnames: Column names (auto-detected from first row if not provided)

    Returns:
        True if save was successful
    """
    if not rows:
        logger.warning(f"No rows to save to {filepath}")
        return False

    try:
        # Ensure directory exists
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)

        # Get fieldnames from first row if not provided
        if fieldnames is None:
            fieldnames = list(rows[0].keys())

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.debug(f"Saved {len(rows)} rows to {filepath}")
        return True

    except OSError as e:
        logger.error(f"Failed to save CSV to {filepath}: {e}")
        return False
