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
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, TYPE_CHECKING

import requests

from src.models import WatchedPosition
from src.utils.paths import validate_file_path
from src.utils.debug_logger import ScanDebugLog
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
    clear_position_scan_progress,
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

    # Initialize debug log for this scan
    debug_log = ScanDebugLog(
        scan_mode=scan_mode,
        is_baseline=is_baseline,
        timestamp=scan_start
    )

    # Step 1: Fetch cohort data
    logger.info("Step 1: Fetching cohort data...")
    phase1_start = time.time()
    try:
        traders = fetch_cohorts(PRIORITY_COHORTS, delay=1.0)
        save_cohort_csv(traders, COHORT_DATA_PATH)
        phase1_elapsed = time.time() - phase1_start
        logger.info(f"Saved {len(traders)} traders to {COHORT_DATA_PATH}")

        # Log cohort phase details
        debug_log.log_cohort_phase(
            traders=traders,
            cohorts_requested=PRIORITY_COHORTS,
            elapsed_seconds=phase1_elapsed
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Cohort fetch network error: {type(e).__name__}")
        debug_log.log_error("cohort", f"Network error: {type(e).__name__}")
        debug_log.save()
        return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Cohort data save error: {type(e).__name__}: {e}")
        debug_log.log_error("cohort", f"Save error: {type(e).__name__}: {e}")
        debug_log.save()
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

        # For baseline scans, clear any previous progress to ensure fresh scan
        # For non-baseline scans, allow resume if interrupted
        if is_baseline:
            clear_position_scan_progress()
            logger.info("Baseline scan: cleared previous progress for fresh scan")

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
        # Resume is enabled for non-baseline scans to recover from interruptions
        allow_resume = not is_baseline
        addresses = load_cohort_addresses(COHORT_DATA_PATH, cohorts=cohorts)
        if addresses:
            # Send phase start alert during deployment (when notify_cohorts is True)
            if notify_cohorts:
                # Calculate estimated time: ~30 concurrent requests, ~0.5s each
                total_requests = len(addresses) * len(dexes)
                estimated_seconds = (total_requests / 30) * 0.5

                # Build cohort breakdown
                cohort_breakdown = {}
                for addr, cohort in addresses:
                    cohort_breakdown[cohort] = cohort_breakdown.get(cohort, 0) + 1

                service.alerts.send_phase_start_alert(
                    phase_name="Phase 2: Position Scan",
                    address_count=len(addresses),
                    exchange_count=len(dexes),
                    estimated_seconds=estimated_seconds,
                    cohort_breakdown=cohort_breakdown
                )

            logger.info(f"Main scan: {len(addresses)} addresses across {len(dexes)} exchanges (async)...")
            phase2_start = time.time()
            positions = fetch_all_positions_async(
                addresses, mark_prices, dexes,
                progress_callback=progress_callback,
                resume=allow_resume
            )
            phase2_elapsed = time.time() - phase2_start
            all_positions.extend(positions)
            logger.info(f"Main scan found {len(positions)} positions in {phase2_elapsed:.1f}s")

            # Send phase complete alert during deployment
            if notify_cohorts:
                exchange_counts = {}
                for p in positions:
                    exchange_counts[p.exchange] = exchange_counts.get(p.exchange, 0) + 1
                extra_info = f"📈 By exchange: {', '.join(f'{k}={v}' for k, v in exchange_counts.items())}"
                service.alerts.send_phase_complete_alert(
                    phase_name="Phase 2: Position Scan",
                    results_count=len(positions),
                    elapsed_seconds=phase2_elapsed,
                    extra_info=extra_info
                )

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
                    progress_callback=progress_callback,
                    resume=allow_resume
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

        # Log position phase details
        debug_log.log_position_phase(
            positions=positions,
            addresses_scanned=len(addresses) if addresses else 0,
            exchanges_scanned=dexes,
            elapsed_seconds=phase2_elapsed if 'phase2_elapsed' in dir() else None,
            resumed=allow_resume
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Position fetch network error: {type(e).__name__}")
        debug_log.log_error("position", f"Network error: {type(e).__name__}")
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
            debug_log.save()
            return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Position data save error: {type(e).__name__}: {e}")
        debug_log.log_error("position", f"Save error: {type(e).__name__}: {e}")
        debug_log.save()
        return 0, 0
    except ValueError as e:
        logger.error(f"Invalid file path: {e}")
        debug_log.log_error("position", f"Path error: {e}")
        debug_log.save()
        return 0, 0

    # Step 3: Run liquidation filter
    logger.info("Step 3: Running liquidation filter...")
    phase3_start = time.time()
    try:
        filtered_file = "data/processed/filtered_position_data_monitor.csv"
        # Validate output path
        validated_filtered_path = validate_file_path(filtered_file)
        stats = filter_positions(str(validated_position_path), str(validated_filtered_path))
        phase3_elapsed = time.time() - phase3_start
        if not stats:
            logger.error("Filter returned no results")
            debug_log.log_error("filter", "No results returned")
            debug_log.save()
            return 0, 0
        logger.info(f"Filter complete: {stats.get('filtered_count', 0)} positions with liq prices")

        # Log filter phase details
        debug_log.log_filter_phase(
            stats=stats,
            elapsed_seconds=phase3_elapsed
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Filter network error (order book fetch): {type(e).__name__}")
        debug_log.log_error("filter", f"Network error: {type(e).__name__}")
        debug_log.save()
        return 0, 0
    except (OSError, csv.Error) as e:
        logger.error(f"Filter file error: {type(e).__name__}: {e}")
        debug_log.log_error("filter", f"File error: {type(e).__name__}: {e}")
        debug_log.save()
        return 0, 0
    except ValueError as e:
        logger.error(f"Filter error: {e}")
        debug_log.log_error("filter", f"Value error: {e}")
        debug_log.save()
        return 0, 0

    # Step 4: Build new watchlist and detect new positions
    logger.info("Step 4: Building watchlist and detecting new positions...")
    phase4_start = time.time()
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
    phase4_elapsed = time.time() - phase4_start

    # Log watchlist phase details
    debug_log.log_watchlist_phase(
        watchlist=new_watchlist,
        new_positions=new_positions,
        baseline_size=len(service.baseline_position_keys),
        previous_size=len(service.previous_position_keys),
        elapsed_seconds=phase4_elapsed
    )

    # Send combined Phase 3+4 completion alert during deployment
    if notify_cohorts:
        watchlist_isolated = sum(1 for p in new_watchlist.values() if p.is_isolated)
        watchlist_cross = len(new_watchlist) - watchlist_isolated
        service.alerts.send_pipeline_complete_alert(
            filter_stats=stats,
            watchlist_size=len(new_watchlist),
            watchlist_isolated=watchlist_isolated,
            watchlist_cross=watchlist_cross,
            new_positions=new_positions,
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

    # Save debug log
    debug_log.save()

    # Send scan summary alert (only during startup)
    if send_summary:
        service.alerts.send_scan_summary_alert(
            watchlist=service.watchlist,
            scan_mode=scan_mode,
            is_baseline=is_baseline,
            scan_time=scan_start
        )

    return len(service.watchlist), len(new_positions)
