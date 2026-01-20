"""
Monitor Phase
=============

Functions for executing the monitor phase of the monitoring loop.

The monitor phase polls prices and sends alerts when positions
approach liquidation.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING

import pytz
import requests

from src.pipeline import fetch_all_positions_for_address
from src.pipeline.step3_filter import calculate_distance_to_liquidation
from src.utils.prices import fetch_all_mark_prices_async, get_current_price
from config.monitor_settings import (
    CRITICAL_ALERT_PCT,
    RECOVERY_PCT,
    PROXIMITY_ALERT_THRESHOLD_PCT,
    CRITICAL_REFRESH_MIN_INTERVAL,
    CRITICAL_REFRESH_MAX_INTERVAL,
    CRITICAL_REFRESH_SCALE_FACTOR,
    MAX_CRITICAL_POSITIONS,
)

if TYPE_CHECKING:
    from .orchestrator import MonitorService

logger = logging.getLogger(__name__)
EST = pytz.timezone('America/New_York')


def get_critical_refresh_interval(critical_count: int) -> float:
    """
    Calculate dynamic refresh interval based on number of critical positions.

    Scales linearly (capped at 30 positions):
    - 1 position: ~2.3s
    - 5 positions: ~3.5s
    - 10+ positions: 5s (max interval)

    Args:
        critical_count: Number of positions in critical zone

    Returns:
        Refresh interval in seconds
    """
    interval = CRITICAL_REFRESH_MIN_INTERVAL + (critical_count * CRITICAL_REFRESH_SCALE_FACTOR)
    return min(interval, CRITICAL_REFRESH_MAX_INTERVAL)


def refresh_critical_positions(service: 'MonitorService', mark_prices: Dict) -> int:
    """
    Refresh position data for positions in high-intensity monitoring zone.

    Positions under PROXIMITY_ALERT_THRESHOLD_PCT get frequent position refresh
    to detect liquidation price changes (margin additions, position closures).

    Args:
        service: MonitorService instance
        mark_prices: Current mark prices by exchange

    Returns:
        Number of positions refreshed
    """
    critical_positions = [
        pos for pos in service.watchlist.values()
        if pos.in_critical_zone
    ]

    if not critical_positions:
        return 0

    # Sort by distance (closest first) and truncate to max
    critical_positions.sort(key=lambda p: p.last_distance_pct)
    if len(critical_positions) > MAX_CRITICAL_POSITIONS:
        logger.info(
            f"Truncating critical positions from {len(critical_positions)} "
            f"to {MAX_CRITICAL_POSITIONS} (prioritizing closest)"
        )
        critical_positions = critical_positions[:MAX_CRITICAL_POSITIONS]

    refreshed = 0
    for position in critical_positions:
        try:
            # Fetch fresh position data for this address
            dexes = [""] if position.exchange == "main" else [position.exchange]
            positions = fetch_all_positions_for_address(
                position.address,
                mark_prices,
                dexes=dexes
            )

            # Find the matching position
            for pos_data in positions:
                pos_dict, exchange = pos_data
                pos_obj = pos_dict.get("position", {})
                token = pos_obj.get("coin", "")

                # Determine side from position size
                szi = float(pos_obj.get("szi", 0))
                side = "Long" if szi > 0 else "Short"

                if (
                    token == position.token and
                    exchange == position.exchange and
                    side == position.side
                ):
                    # Update liquidation price if changed
                    new_liq_px = pos_obj.get("liquidationPx")
                    if new_liq_px is not None:
                        new_liq_price = float(new_liq_px)
                        if new_liq_price != position.liq_price:
                            logger.info(
                                f"Critical position {position.token} liq price updated: "
                                f"{position.liq_price:.4f} -> {new_liq_price:.4f}"
                            )
                            position.liq_price = new_liq_price

                    # Update position value if changed
                    new_value = pos_obj.get("positionValue")
                    if new_value:
                        position.position_value = float(new_value)

                    refreshed += 1
                    break

        except Exception as e:
            logger.warning(f"Failed to refresh critical position {position.token}: {e}")

    if refreshed > 0:
        logger.info(f"Refreshed {refreshed} critical positions")

    return refreshed


def run_monitor_phase(service: 'MonitorService', until: Optional[datetime] = None):
    """
    Execute the monitor phase - poll prices until specified time or next scan.

    High-intensity monitoring for positions under PROXIMITY_ALERT_THRESHOLD_PCT:
    - Refresh position data at dynamic interval (scales with position count)
    - Alert at CRITICAL_ALERT_PCT threshold (imminent liquidation)
    - Recovery alert when liq price changes (manual intervention detected)

    Args:
        service: MonitorService instance
        until: datetime (UTC) to run until. If None, uses service.scan_interval from now.
    """
    if until is None:
        next_scan_time = time.time() + service.scan_interval
        next_scan_display = "in {} minutes".format(service.scan_interval // 60)
    else:
        next_scan_time = until.timestamp()
        next_scan_est = until.astimezone(EST)
        next_scan_display = next_scan_est.strftime("%H:%M EST")

    logger.info("MONITOR PHASE STARTING")
    logger.info(f"Watching {len(service.watchlist)} positions")
    logger.info(f"Poll interval: {service.poll_interval}s")
    logger.info(f"Next scan: {next_scan_display}")

    last_critical_refresh = 0

    while service.running and time.time() < next_scan_time:
        try:
            # Fetch current prices (async for speed)
            mark_prices = fetch_all_mark_prices_async()
            if not mark_prices:
                logger.warning("Failed to fetch mark prices, retrying...")
                time.sleep(service.poll_interval)
                continue

            # Priority: Refresh critical positions if interval elapsed
            critical_count = sum(1 for p in service.watchlist.values() if p.in_critical_zone)
            if critical_count > 0:
                now = time.time()
                # Use capped count for interval calculation (matches truncation in refresh)
                capped_count = min(critical_count, MAX_CRITICAL_POSITIONS)
                refresh_interval = get_critical_refresh_interval(capped_count)
                if now - last_critical_refresh >= refresh_interval:
                    logger.debug(
                        f"Critical refresh: {critical_count} positions, "
                        f"interval: {refresh_interval:.0f}s"
                    )
                    refresh_critical_positions(service, mark_prices)
                    last_critical_refresh = now

            # Check each watched position
            alerts_sent = 0
            for key, position in service.watchlist.items():
                current_price = get_current_price(
                    position.token,
                    position.exchange,
                    mark_prices
                )

                if current_price == 0:
                    continue

                # Calculate new distance
                new_distance = calculate_distance_to_liquidation(
                    current_price,
                    position.liq_price,
                    position.side
                )

                previous_distance = position.last_distance_pct
                was_in_critical_zone = position.in_critical_zone

                # Update position state
                position.last_mark_price = current_price
                position.last_distance_pct = new_distance
                now_in_critical_zone = new_distance < PROXIMITY_ALERT_THRESHOLD_PCT

                # Track liq price when entering high-intensity zone (for recovery detection)
                # Recovery alerts only fire if liq price changed (manual intervention)
                if now_in_critical_zone and not was_in_critical_zone:
                    position.liq_price_at_critical_entry = position.liq_price

                position.in_critical_zone = now_in_critical_zone

                # Determine reply_to for threading
                reply_to = position.last_proximity_message_id or position.alert_message_id

                # Check for RECOVERY: was in high-intensity zone, now recovered (>0.5%)
                if (
                    was_in_critical_zone and
                    new_distance > RECOVERY_PCT
                ):
                    # Only alert if liq_price changed (indicates manual intervention:
                    # margin added for isolated, funds transferred/positions closed for cross)
                    liq_price_changed = (
                        position.liq_price_at_critical_entry is not None and
                        abs(position.liq_price - position.liq_price_at_critical_entry) > 0.0001
                    )

                    if liq_price_changed:
                        logger.info(
                            f"RECOVERY (manual intervention): {position.token} {position.side} "
                            f"recovered from {previous_distance:.3f}% to {new_distance:.2f}% "
                            f"(liq price: {position.liq_price_at_critical_entry:.4f} -> {position.liq_price:.4f})"
                        )
                        msg_id = service.alerts.send_recovery_alert(
                            position,
                            previous_distance,
                            current_price,
                            reply_to_message_id=reply_to
                        )
                        if msg_id:
                            position.last_proximity_message_id = msg_id
                            alerts_sent += 1
                            # Log to database
                            service.db.log_alert(
                                position_key=position.position_key,
                                alert_type="recovery",
                                message_id=msg_id,
                                details=f"{previous_distance:.3f}% -> {new_distance:.2f}% (liq price changed)"
                            )
                    else:
                        logger.info(
                            f"RECOVERY (price movement only): {position.token} {position.side} "
                            f"recovered from {previous_distance:.3f}% to {new_distance:.2f}% - no alert"
                        )

                    # Always reset flags on recovery (regardless of alert)
                    position.alerted_critical = False
                    position.in_critical_zone = False
                    position.liq_price_at_critical_entry = None
                    continue  # Skip other checks for this position

                # Check for CRITICAL alert (crossing below 0.1%)
                if (
                    not position.alerted_critical and
                    new_distance <= CRITICAL_ALERT_PCT and
                    (previous_distance is None or previous_distance > CRITICAL_ALERT_PCT)
                ):
                    logger.info(
                        f"CRITICAL ALERT: {position.token} {position.side} "
                        f"at {new_distance:.3f}% (threshold: {CRITICAL_ALERT_PCT}%)"
                    )
                    msg_id = service.alerts.send_critical_alert(
                        position,
                        previous_distance or new_distance,
                        current_price,
                        reply_to_message_id=reply_to
                    )
                    if msg_id:
                        position.alerted_critical = True
                        position.last_proximity_message_id = msg_id
                        alerts_sent += 1
                        # Log to database
                        service.db.log_alert(
                            position_key=position.position_key,
                            alert_type="critical",
                            message_id=msg_id,
                            details=f"distance={new_distance:.3f}%"
                        )

                # Check for PROXIMITY alert (crossing below threshold)
                elif (
                    not position.alerted_proximity and
                    new_distance <= PROXIMITY_ALERT_THRESHOLD_PCT and
                    (previous_distance is None or previous_distance > PROXIMITY_ALERT_THRESHOLD_PCT)
                ):
                    logger.info(
                        f"PROXIMITY ALERT: {position.token} {position.side} "
                        f"at {new_distance:.2f}% (threshold: {PROXIMITY_ALERT_THRESHOLD_PCT}%)"
                    )
                    msg_id = service.alerts.send_proximity_alert(
                        position,
                        previous_distance or new_distance,
                        current_price
                    )
                    if msg_id:
                        position.alerted_proximity = True
                        position.last_proximity_message_id = msg_id
                        alerts_sent += 1
                        # Log to database
                        service.db.log_alert(
                            position_key=position.position_key,
                            alert_type="proximity",
                            message_id=msg_id,
                            details=f"distance={new_distance:.2f}%"
                        )

            if alerts_sent > 0:
                logger.info(f"Sent {alerts_sent} alerts")

            # Record position snapshots periodically (every 5 min)
            service._record_position_snapshots()

            # Prune old data periodically (once per day)
            service._maybe_prune_data()

            # Log periodic status
            remaining = int(next_scan_time - time.time())
            if remaining % 300 < service.poll_interval:  # Every ~5 minutes
                logger.info(
                    f"Monitor phase: {remaining}s until next scan, "
                    f"{len(service.watchlist)} positions watched, "
                    f"{critical_count} in critical zone"
                )

        except requests.exceptions.RequestException as e:
            logger.error(f"Monitor phase network error: {type(e).__name__}")
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Monitor phase data error: {type(e).__name__}: {e}")

        time.sleep(service.poll_interval)

    logger.info("MONITOR PHASE COMPLETE")
