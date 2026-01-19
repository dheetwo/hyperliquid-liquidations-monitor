"""
Watchlist Management
====================

Functions for building and managing the position watchlist.
"""

import csv
import logging
from datetime import datetime, timezone
from typing import Dict, List, TYPE_CHECKING

from src.models import WatchedPosition
from src.utils.paths import validate_file_path
from src.utils.csv_helpers import sanitize_csv_value
from config.monitor_settings import (
    MAX_WATCH_DISTANCE_PCT,
    MIN_HUNTING_SCORE,
    WATCHLIST_MIN_NOTIONAL_ISOLATED,
    WATCHLIST_MIN_NOTIONAL_CROSS,
    WATCHLIST_MIN_NOTIONAL_BY_TOKEN,
    get_proximity_alert_threshold,
)

if TYPE_CHECKING:
    from .orchestrator import MonitorService

logger = logging.getLogger(__name__)


def load_filtered_positions(filepath: str) -> List[Dict]:
    """
    Load filtered position data from CSV.

    Includes CSV injection protection by sanitizing values.

    Args:
        filepath: Path to filtered positions CSV

    Returns:
        List of position dictionaries (sanitized)
    """
    positions = []
    try:
        # Validate path is within allowed directories
        validated_path = validate_file_path(filepath, must_exist=True)

        with open(validated_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Sanitize all string values to prevent CSV injection
                sanitized_row = {
                    key: sanitize_csv_value(value) if isinstance(value, str) else value
                    for key, value in row.items()
                }
                positions.append(sanitized_row)

        logger.info(f"Loaded {len(positions)} filtered positions from {filepath}")

    except FileNotFoundError:
        logger.error(f"Filtered positions file not found: {filepath}")
    except ValueError as e:
        logger.error(f"Invalid file path: {e}")
    except csv.Error as e:
        logger.error(f"CSV parsing error: {e}")
    except OSError as e:
        logger.error(f"File read error: {e}")

    return positions


def build_watchlist(filtered_positions: List[Dict]) -> Dict[str, WatchedPosition]:
    """
    Build watchlist from filtered position data.

    Filters by:
    - Distance within MAX_WATCH_DISTANCE_PCT
    - Hunting score above MIN_HUNTING_SCORE
    - Notional value thresholds (varies by token and margin type)

    Args:
        filtered_positions: List of position dicts from filtered CSV

    Returns:
        Dict mapping position_key to WatchedPosition
    """
    watchlist = {}
    scan_time = datetime.now(timezone.utc).isoformat()
    filtered_by_notional = 0

    for row in filtered_positions:
        try:
            # Parse required fields
            address = row['Address']
            token = row['Token']
            exchange = row['Exchange']
            side = row['Side']
            liq_price = float(row['Liquidation Price'])
            position_value = float(row['Position Value'])
            is_isolated = row['Isolated'].lower() == 'true'
            hunting_score = float(row['Hunting Score'])
            distance_pct = float(row['Distance to Liq (%)'])
            current_price = float(row['Current Price'])

            # Apply distance filter
            if distance_pct > MAX_WATCH_DISTANCE_PCT:
                continue
            if hunting_score < MIN_HUNTING_SCORE:
                continue

            # Apply notional filters
            if is_isolated:
                min_notional = WATCHLIST_MIN_NOTIONAL_ISOLATED
            else:
                # Check token-specific threshold first, then default
                min_notional = WATCHLIST_MIN_NOTIONAL_BY_TOKEN.get(
                    token, WATCHLIST_MIN_NOTIONAL_CROSS
                )

            if position_value < min_notional:
                filtered_by_notional += 1
                continue

            # Create watched position
            watched = WatchedPosition(
                address=address,
                token=token,
                exchange=exchange,
                side=side,
                liq_price=liq_price,
                position_value=position_value,
                is_isolated=is_isolated,
                hunting_score=hunting_score,
                last_distance_pct=distance_pct,
                last_mark_price=current_price,
                threshold_pct=get_proximity_alert_threshold(),
                first_seen_scan=scan_time,
            )

            watchlist[watched.position_key] = watched

        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping position due to parsing error: {e}")
            continue

    logger.info(f"Built watchlist with {len(watchlist)} positions ({filtered_by_notional} filtered by notional)")
    return watchlist


def detect_new_positions(
    current_watchlist: Dict[str, WatchedPosition],
    baseline_keys: set,
    previous_keys: set,
    is_baseline: bool = False,
    manual_mode: bool = False,
) -> List[WatchedPosition]:
    """
    Detect positions that are new since the baseline scan AND pass alert thresholds.

    In scheduled mode:
    - Baseline (comprehensive) scan: compare against previous baseline
    - Normal/priority scans: compare against current day's baseline

    In manual mode:
    - Always compare against previous scan

    Args:
        current_watchlist: Current watchlist after filtering
        baseline_keys: Position keys from baseline scan
        previous_keys: Position keys from previous scan
        is_baseline: If True, this is a baseline scan
        manual_mode: If True, always compare against previous

    Returns:
        List of new WatchedPositions that pass thresholds
    """
    from config.monitor_settings import passes_new_position_threshold

    current_keys = set(current_watchlist.keys())

    # In manual mode or baseline scan, compare against previous_keys
    # In scheduled mode non-baseline scan, compare against baseline_keys
    if manual_mode or is_baseline:
        compare_keys = previous_keys
    else:
        compare_keys = baseline_keys

    new_keys = current_keys - compare_keys

    # Filter new positions by alert thresholds
    new_positions = []
    for key in new_keys:
        pos = current_watchlist[key]
        if passes_new_position_threshold(
            token=pos.token,
            position_value=pos.position_value,
            distance_pct=pos.last_distance_pct,
            is_isolated=pos.is_isolated
        ):
            new_positions.append(pos)

    # Sort by hunting score descending
    new_positions.sort(key=lambda p: p.hunting_score, reverse=True)

    comparison_type = "previous scan" if (manual_mode or is_baseline) else "baseline"
    logger.info(
        f"Detected {len(new_keys)} new positions (vs {comparison_type}), "
        f"{len(new_positions)} pass alert thresholds"
    )
    return new_positions
