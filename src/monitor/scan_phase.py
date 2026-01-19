"""
Scan Phase
==========

Functions for executing the scan phase of the monitoring loop.

The scan phase runs the full data pipeline:
1. Fetch cohort data
2. Fetch position data
3. Run liquidation filter
4. Detect new positions
5. Send alerts
6. Update watchlist
"""

import csv
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, TYPE_CHECKING

import requests

from src.models import WatchedPosition
from src.utils.paths import validate_file_path
from src.pipeline import (
    fetch_cohorts,
    save_cohort_csv,
    PRIORITY_COHORTS,
    load_cohort_addresses,
    fetch_all_mark_prices_async,
    fetch_all_positions_async,
    save_position_csv,
    filter_positions,
    SCAN_MODES,
)
from config.monitor_settings import COHORT_DATA_PATH
from .watchlist import load_filtered_positions, build_watchlist, detect_new_positions

if TYPE_CHECKING:
    from .orchestrator import MonitorService

logger = logging.getLogger(__name__)


def run_scan_phase(
    service: 'MonitorService',
    mode: Optional[str] = None,
    is_baseline: bool = False,
    notify_cohorts: bool = False,
    send_summary: bool = False
) -> Tuple[int, int]:
    """
    Execute the scan phase of the monitor loop.

    Steps:
    1. Run cohort scraper
    2. Run position scraper
    3. Run liquidation filter
    4. Detect new positions
    5. Send alerts for new positions
    6. Update watchlist

    Args:
        service: MonitorService instance
        mode: Scan mode override ("high-priority", "normal", "comprehensive").
              If None, uses service.scan_mode.
        is_baseline: If True, this is a baseline scan:
                     - Reset baseline_position_keys
                     - Full watchlist replacement
                     - Alerts compare against previous baseline
        notify_cohorts: If True, send Telegram notification when each cohort starts.
                       Only used during startup progressive scan.
        send_summary: If True, send scan summary alert showing watchlist.
                     Only used during startup progressive scan.

    Returns:
        Tuple of (total_positions, new_positions_count)
    """
    # Use provided mode or fall back to instance default
    scan_mode = mode or service.scan_mode

    logger.info("=" * 60)
    logger.info(f"SCAN PHASE STARTING - Mode: {scan_mode}" +
               (" (BASELINE)" if is_baseline else ""))
    logger.info("=" * 60)

    scan_start = datetime.now(timezone.utc)

    # Step 1: Fetch cohort data
    logger.info("Step 1: Fetching cohort data...")
    try:
        traders = fetch_cohorts(PRIORITY_COHORTS, delay=1.0)
        save_cohort_csv(traders, COHORT_DATA_PATH)
        logger.info(f"Saved {len(traders)} traders to {COHORT_DATA_PATH}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Cohort fetch network error: {type(e).__name__}")
        return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Cohort data save error: {type(e).__name__}: {e}")
        return 0, 0

    # Step 2: Fetch position data
    logger.info("Step 2: Fetching position data...")
    position_scan_time = datetime.now(timezone.utc)
    try:
        # Get scan mode config
        mode_config = SCAN_MODES.get(scan_mode, SCAN_MODES["normal"])
        cohorts = mode_config["cohorts"]
        dexes = mode_config["dexes"]
        additional_scans = mode_config.get("additional_scans", [])

        # Fetch mark prices (async for speed)
        logger.info("Fetching mark prices from all exchanges...")
        mark_prices = fetch_all_mark_prices_async()
        if not mark_prices:
            logger.error("Failed to fetch mark prices")
            return 0, 0

        # Create callback for cohort notifications (only during startup)
        progress_callback = None
        if notify_cohorts:
            last_cohort = [None]  # Use list for mutable closure

            def progress_callback(processed, total, positions_found, cohort):
                if cohort != last_cohort[0]:
                    last_cohort[0] = cohort
                    service.alerts.send_cohort_start(
                        cohort=cohort,
                        phase_name=scan_mode.replace("-", " ").title()
                    )

        all_positions = []

        # Main scan: primary cohorts and dexes
        addresses = load_cohort_addresses(COHORT_DATA_PATH, cohorts=cohorts)
        if addresses:
            logger.info(f"Main scan: {len(addresses)} addresses across {len(dexes)} exchanges (async)...")
            positions = fetch_all_positions_async(
                addresses, mark_prices, dexes,
                progress_callback=progress_callback
            )
            all_positions.extend(positions)
            logger.info(f"Main scan found {len(positions)} positions")

        # Additional scans (for incremental modes like shark-incremental)
        for i, extra_scan in enumerate(additional_scans):
            extra_cohorts = extra_scan["cohorts"]
            extra_dexes = extra_scan["dexes"]
            extra_addresses = load_cohort_addresses(COHORT_DATA_PATH, cohorts=extra_cohorts)
            if extra_addresses:
                logger.info(
                    f"Additional scan {i+1}: {len(extra_addresses)} addresses "
                    f"({', '.join(extra_cohorts)}) on {len(extra_dexes)} extra exchanges..."
                )
                extra_positions = fetch_all_positions_async(
                    extra_addresses, mark_prices, extra_dexes,
                    progress_callback=progress_callback
                )
                all_positions.extend(extra_positions)
                logger.info(f"Additional scan found {len(extra_positions)} positions")

        positions = all_positions

        if not positions:
            logger.warning("No positions found in scan")

        # Save to temporary file for filter (with path validation)
        position_file = "data/raw/position_data_monitor.csv"
        validated_position_path = validate_file_path(position_file)
        save_position_csv(positions, str(validated_position_path))
        logger.info(f"Saved {len(positions)} positions to {position_file}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Position fetch network error: {type(e).__name__}")
        # Try to use cached data as fallback
        position_file = "data/raw/position_data_monitor.csv"
        if service._can_use_cached_positions(position_file):
            age = service._get_cached_position_file_age(position_file)
            logger.warning(f"Using cached position data ({age:.1f} min old) due to rate limit")
            service.alerts.send_service_status(
                "error",
                f"Rate limited - using cached data ({age:.1f} min old)"
            )
            validated_position_path = validate_file_path(position_file)
        else:
            logger.error("No valid cached position data available")
            return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Position data save error: {type(e).__name__}: {e}")
        return 0, 0
    except ValueError as e:
        logger.error(f"Invalid file path: {e}")
        return 0, 0

    # Step 3: Run liquidation filter
    logger.info("Step 3: Running liquidation filter...")
    try:
        filtered_file = "data/processed/filtered_position_data_monitor.csv"
        # Validate output path
        validated_filtered_path = validate_file_path(filtered_file)
        stats = filter_positions(str(validated_position_path), str(validated_filtered_path))
        if not stats:
            logger.error("Filter returned no results")
            return 0, 0
        logger.info(f"Filter complete: {stats.get('filtered_count', 0)} positions with liq prices")
    except requests.exceptions.RequestException as e:
        logger.error(f"Filter network error (order book fetch): {type(e).__name__}")
        return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Filter file error: {type(e).__name__}: {e}")
        return 0, 0
    except ValueError as e:
        logger.error(f"Filter error: {e}")
        return 0, 0

    # Step 4: Build new watchlist and detect new positions
    logger.info("Step 4: Building watchlist and detecting new positions...")
    filtered_positions = load_filtered_positions(str(validated_filtered_path))
    new_watchlist = build_watchlist(filtered_positions)

    # Detect new positions (pass is_baseline flag)
    new_positions = detect_new_positions(
        current_watchlist=new_watchlist,
        baseline_keys=service.baseline_position_keys,
        previous_keys=service.previous_position_keys,
        is_baseline=is_baseline,
        manual_mode=service.manual_mode,
    )

    # Step 5: Send alerts for new positions
    if new_positions:
        logger.info(f"Step 5: Alerting {len(new_positions)} new positions...")
        message_id = service.alerts.send_new_positions_alert(new_positions, position_scan_time)
        # Store message_id in each alerted position for reply threading
        if message_id:
            for pos in new_positions:
                pos.alert_message_id = message_id
                # Log alert to database
                service.db.log_alert(
                    position_key=pos.position_key,
                    alert_type="new_position",
                    message_id=message_id,
                    details=f"{pos.token} {pos.side} ${pos.position_value:,.0f}"
                )
    else:
        logger.info("Step 5: No new positions to alert")

    # Step 6: Update state
    if is_baseline:
        # Baseline scan: full watchlist replacement, reset baseline keys
        service.watchlist = new_watchlist
        service.baseline_position_keys = set(new_watchlist.keys())
        logger.info(f"Baseline reset: {len(service.baseline_position_keys)} positions in baseline")
    else:
        # Non-baseline scan: merge into existing watchlist
        # Preserve alert flags and message IDs for positions that still exist
        for key, new_pos in new_watchlist.items():
            if key in service.watchlist:
                old_pos = service.watchlist[key]
                new_pos.alerted_proximity = old_pos.alerted_proximity
                new_pos.alerted_critical = old_pos.alerted_critical
                new_pos.in_critical_zone = old_pos.in_critical_zone
                new_pos.alert_message_id = old_pos.alert_message_id
                new_pos.last_proximity_message_id = old_pos.last_proximity_message_id
            # Add or update position in watchlist
            service.watchlist[key] = new_pos

    service.previous_position_keys = set(new_watchlist.keys())
    service.last_scan_time = scan_start

    # Persist state to database
    service._save_state()

    # Record scan completion time for rate limiting
    service.db.set_last_scan_time(datetime.now(timezone.utc))

    logger.info("=" * 60)
    logger.info(f"SCAN PHASE COMPLETE - {len(service.watchlist)} positions in watchlist")
    logger.info("=" * 60)

    # Send scan summary alert (only during startup)
    if send_summary:
        service.alerts.send_scan_summary_alert(
            watchlist=service.watchlist,
            scan_mode=scan_mode,
            is_baseline=is_baseline,
            scan_time=scan_start
        )

    return len(service.watchlist), len(new_positions)
