"""
Monitor Service Orchestrator
============================

Main monitoring service for liquidation hunting opportunities.

Architecture:
- Initial comprehensive scan to populate position cache
- Continuous tiered refresh based on liquidation distance:
  - Critical (≤0.125%): Continuous (~5 req/sec)
  - High (0.125-0.25%): Every 2-3 seconds
  - Normal (>0.25%): Every 30 seconds
- Dynamic discovery scans for new addresses (frequency based on API pressure)
- Daily summary at 6am EST
- No intraday "new position" alerts - quiet backend updates
"""

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytz
import requests

from src.models import WatchedPosition
from src.pipeline import (
    fetch_cohorts,
    save_cohort_csv,
    fetch_all_mark_prices_async,
    fetch_all_positions_async,
    fetch_all_positions_for_address,
    parse_position,
    ALL_COHORTS,
    ALL_DEXES,
    SIZE_COHORTS,
    PNL_COHORTS,
)
from src.pipeline.step3_filter import calculate_distance_to_liquidation
from src.utils.prices import get_current_price
from .alerts import TelegramAlerts, AlertConfig
from .database import MonitorDatabase
from .liquidation_feed import LiquidationHistoryDB
from .cache import (
    CachedPosition,
    PositionCache,
    TieredRefreshScheduler,
    DiscoveryScheduler,
    classify_tier,
)
from config.monitor_settings import (
    POLL_INTERVAL_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CACHE_TIER_CRITICAL_PCT,
    CACHE_TIER_HIGH_PCT,
    CACHE_REFRESH_CRITICAL_SEC,
    CACHE_REFRESH_HIGH_SEC,
    CACHE_REFRESH_NORMAL_SEC,
    CACHE_MAX_AGE_MINUTES,
    CACHE_PRUNE_AGE_HOURS,
    DISCOVERY_MIN_INTERVAL_MINUTES,
    DISCOVERY_MAX_INTERVAL_MINUTES,
    DISCOVERY_PRESSURE_CRITICAL_WEIGHT,
    DISCOVERY_PRESSURE_HIGH_WEIGHT,
    DAILY_SUMMARY_TIMES,
    CRITICAL_ZONE_PCT,
    CRITICAL_ALERT_PCT,
    RECOVERY_PCT,
    ALERT_NATURAL_RECOVERY,
    COHORT_DATA_PATH,
    MAX_WATCH_DISTANCE_PCT,
    MIN_WALLET_POSITION_VALUE,
    WALLET_ACTIVE_THRESHOLD,
    INFREQUENT_SCAN_INTERVAL_HOURS,
    get_watchlist_threshold,
)

# Timezone for scheduling
EST = pytz.timezone('America/New_York')

logger = logging.getLogger(__name__)


def passes_watchlist_threshold(
    token: str,
    exchange: str,
    is_isolated: bool,
    position_value: float
) -> bool:
    """
    Check if a position meets the minimum notional threshold for monitoring.

    Uses tier-based thresholds from monitor_settings.py:
    - Main exchange: BTC $100M, ETH $75M, tier1 $25M, tier2 $10M, etc.
    - XYZ exchange: Indices $5M, mega equities $3M, etc.
    - Other sub-exchanges: Flat $500K

    Isolated positions use lower thresholds (cross_threshold / 5).

    Args:
        token: Token symbol (e.g., "BTC", "ETH", "DOGE")
        exchange: Exchange name ("main", "xyz", "flx", etc.)
        is_isolated: Whether the position uses isolated margin
        position_value: Position notional value in USD

    Returns:
        True if position meets threshold, False otherwise
    """
    threshold = get_watchlist_threshold(token, exchange, is_isolated)
    return position_value >= threshold


class MonitorService:
    """
    Continuous monitoring service for liquidation hunting.

    Uses cache-based architecture with tiered refresh:
    - Initial comprehensive scan to populate cache
    - Continuous tiered refresh based on liquidation distance
    - Dynamic discovery for new positions
    - Daily summary at 6am EST
    """

    def __init__(
        self,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
        dry_run: bool = False,
        send_startup_summary: bool = False,
    ):
        """
        Initialize the monitor service.

        Args:
            poll_interval_seconds: Time between price polls
            dry_run: If True, print alerts instead of sending to Telegram
            send_startup_summary: If True, send watchlist summary on startup (default: False to avoid duplicates on redeployment)
        """
        self.poll_interval = poll_interval_seconds
        self.dry_run = dry_run
        self.send_startup_summary = send_startup_summary

        # Initialize alert system
        self.alerts = TelegramAlerts(AlertConfig(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
            dry_run=dry_run,
        ))

        # Initialize databases
        self.db = MonitorDatabase()
        self.liq_history_db = LiquidationHistoryDB(
            Path(__file__).parent.parent.parent / "data" / "liquidation_history.db"
        )

        # Initialize cache components
        self.position_cache = PositionCache(self.db)
        self.refresh_scheduler = TieredRefreshScheduler(
            self.position_cache,
            critical_interval=CACHE_REFRESH_CRITICAL_SEC,
            high_interval=CACHE_REFRESH_HIGH_SEC,
            normal_interval=CACHE_REFRESH_NORMAL_SEC,
        )
        self.discovery_scheduler = DiscoveryScheduler(
            self.position_cache,
            self.db,
            min_interval_minutes=DISCOVERY_MIN_INTERVAL_MINUTES,
            max_interval_minutes=DISCOVERY_MAX_INTERVAL_MINUTES,
            critical_weight=DISCOVERY_PRESSURE_CRITICAL_WEIGHT,
            high_weight=DISCOVERY_PRESSURE_HIGH_WEIGHT,
        )

        # State tracking
        self.running = False
        self._last_snapshot_time: float = 0
        self._last_prune_time: float = 0

        # Daily summary tracking
        self._last_summary_date: Optional[date] = None
        self._summaries_sent_today: Set[Tuple[int, int]] = set()

        # Alert tracking for proximity alerts
        self._alerted_positions: Dict[str, dict] = {}  # position_key -> alert state

        # Filter statistics from last scan (for daily summary)
        self._last_scan_stats: Dict[str, int] = {
            'total_positions': 0,
            'no_liq_price': 0,
            'distance_too_far': 0,
            'below_notional': 0,
            'multiple_filters': 0,
            'passed_filters': 0,
        }

        # Signal handling for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Shutdown signal received, stopping monitor...")
        self.running = False

    def _restore_from_cache(self) -> bool:
        """
        Restore state from position cache on startup.

        Returns:
            True if cache was valid and loaded, False if initial scan needed
        """
        try:
            # Load position cache
            loaded = self.position_cache.load_from_db()
            if loaded == 0:
                logger.info("No cached positions found, initial scan required")
                return False

            # Check cache freshness
            oldest_refresh = self.position_cache.get_oldest_refresh()
            if oldest_refresh is None:
                logger.info("No refresh timestamps in cache, initial scan required")
                return False

            age_minutes = (datetime.now(timezone.utc) - oldest_refresh).total_seconds() / 60
            if age_minutes > CACHE_MAX_AGE_MINUTES:
                logger.info(f"Cache too old ({age_minutes:.1f} min > {CACHE_MAX_AGE_MINUTES} min), initial scan required")
                return False

            # Filter cached positions against current thresholds and sanitize tokens
            filtered_count, sanitized_count = self._filter_and_sanitize_cached_positions()
            remaining = len(self.position_cache.positions)

            if filtered_count > 0 or sanitized_count > 0:
                logger.info(
                    f"Cache cleanup: {filtered_count} positions removed (below threshold), "
                    f"{sanitized_count} tokens sanitized, {remaining} positions remaining"
                )

            # Load known addresses
            self.discovery_scheduler.load_known_addresses()
            self.discovery_scheduler.restore_last_discovery()

            # Load alert state
            self._restore_alert_state()

            logger.info(
                f"Restored from cache: {remaining} positions, "
                f"cache age: {age_minutes:.1f} min"
            )
            return True

        except Exception as e:
            logger.warning(f"Failed to restore from cache: {e}, initial scan required")
            return False

    def _filter_and_sanitize_cached_positions(self) -> tuple:
        """
        Filter cached positions against current thresholds and sanitize token names.

        Filters by:
        - Notional threshold (based on token tier)
        - Distance to liquidation (MAX_WATCH_DISTANCE_PCT)

        Returns:
            Tuple of (filtered_count, sanitized_count)
        """
        known_exchanges = {"xyz", "flx", "hyna", "km"}
        positions_to_remove = []
        positions_to_update = []
        sanitized_count = 0

        for key, pos in list(self.position_cache.positions.items()):
            # Sanitize token: strip exchange prefix if present (fixes "flx:XMR" -> "XMR")
            if ":" in pos.token:
                prefix, rest = pos.token.split(":", 1)
                if prefix in known_exchanges:
                    pos.token = rest
                    # Update position_key to match new token
                    old_key = pos.position_key
                    pos.position_key = f"{pos.address}:{pos.token}:{pos.exchange}:{pos.side}"
                    positions_to_update.append((old_key, pos))
                    sanitized_count += 1

            # Check against current notional threshold
            # Sub-exchanges (xyz, flx, etc.) are always isolated regardless of leverage_type
            is_isolated = pos.leverage_type.lower() == "isolated" or pos.exchange != "main"
            if not passes_watchlist_threshold(
                token=pos.token,
                exchange=pos.exchange,
                is_isolated=is_isolated,
                position_value=pos.position_value
            ):
                positions_to_remove.append(key)
                continue

            # Check against distance threshold
            if pos.distance_pct is not None and pos.distance_pct > MAX_WATCH_DISTANCE_PCT:
                positions_to_remove.append(key)

        # Remove positions that don't meet thresholds
        for key in positions_to_remove:
            self.position_cache.remove_position(key)

        # Update sanitized positions in cache and database
        for old_key, pos in positions_to_update:
            if old_key in positions_to_remove:
                continue  # Skip if already removed due to threshold
            # Remove old key and add with new key
            if old_key in self.position_cache.positions:
                del self.position_cache.positions[old_key]
                # Remove old tier entry
                for tier in self.position_cache.tier_queues:
                    if old_key in self.position_cache.tier_queues[tier]:
                        self.position_cache.tier_queues[tier].remove(old_key)
            # Add with corrected key
            self.position_cache.positions[pos.position_key] = pos
            self.position_cache.tier_queues[pos.refresh_tier].append(pos.position_key)
            # Persist to database (save new, delete old)
            self.db.save_cached_position(pos.to_dict())
            self.db.delete_cached_positions([old_key])

        return len(positions_to_remove), sanitized_count

    def _restore_alert_state(self):
        """Restore proximity alert state from database."""
        # Load from service_state
        for pos in self.position_cache.positions.values():
            self._alerted_positions[pos.position_key] = {
                'alerted_proximity': False,
                'alerted_critical': False,
                'in_critical_zone': False,
            }

    def _run_initial_scan(self):
        """
        One-time comprehensive scan to populate position cache.

        Scans all cohorts across all exchanges.
        """
        logger.info("=" * 60)
        logger.info("INITIAL COMPREHENSIVE SCAN STARTING")
        logger.info("=" * 60)

        scan_start = datetime.now(timezone.utc)

        # Step 1: Fetch all cohorts and filter by minimum position value and leverage
        logger.info("Step 1: Fetching cohort data...")
        try:
            traders = fetch_cohorts(ALL_COHORTS, delay=1.0)
            total_traders = len(traders)

            # Filter traders:
            # 1. Must have position value >= MIN_WALLET_POSITION_VALUE
            # 2. Skip long-only wallets with leverage <= 1.0 (no liquidation risk)
            #    Keep short/neutral wallets even at low leverage (shorts can still be liquidated)
            def passes_filters(t):
                if t.position_value < MIN_WALLET_POSITION_VALUE:
                    return False
                # Long-only wallets with leverage <= 1.0 can't be liquidated
                if t.leverage <= 1.0 and t.perp_bias.startswith("Long"):
                    return False
                return True

            traders = [t for t in traders if passes_filters(t)]
            filtered_out = total_traders - len(traders)

            # Save only filtered traders to CSV
            save_cohort_csv(traders, COHORT_DATA_PATH)
            logger.info(f"Fetched {total_traders} traders, filtered {filtered_out} "
                       f"(below ${MIN_WALLET_POSITION_VALUE/1_000:.0f}K or low-leverage long), "
                       f"saved {len(traders)}")
        except Exception as e:
            logger.error(f"Cohort fetch error: {e}")
            raise

        # Build address list with cohorts (already in priority order from fetch_cohorts)
        addresses = [(t.address, t.cohort) for t in traders]

        # Build map of address -> all cohorts (wallets can be in both size AND pnl cohorts)
        address_cohorts: Dict[str, List[str]] = {}
        for addr, cohort in addresses:
            if addr not in address_cohorts:
                address_cohorts[addr] = []
            if cohort not in address_cohorts[addr]:
                address_cohorts[addr].append(cohort)

        # Merge cohorts: combine pnl cohort + size cohort into display string
        # e.g., "profitable shark", "rekt kraken", or just "whale" if no pnl cohort
        def merge_cohorts(cohorts: List[str]) -> str:
            size_cohort = None
            pnl_cohort = None
            for c in cohorts:
                if c in SIZE_COHORTS and size_cohort is None:
                    size_cohort = c
                elif c in PNL_COHORTS and pnl_cohort is None:
                    pnl_cohort = c
            if size_cohort and pnl_cohort:
                return f"{pnl_cohort} {size_cohort}"
            return size_cohort or pnl_cohort or cohorts[0]

        # Dedupe and merge cohort info
        unique_addresses = []
        for addr, cohorts in address_cohorts.items():
            merged_cohort = merge_cohorts(cohorts)
            unique_addresses.append((addr, merged_cohort))

        logger.info(f"Unique addresses to scan: {len(unique_addresses)}")

        # Step 2: Fetch mark prices
        logger.info("Step 2: Fetching mark prices from all exchanges...")
        try:
            mark_prices = fetch_all_mark_prices_async(ALL_DEXES)
            logger.info(f"Fetched {len(mark_prices)} prices")
        except Exception as e:
            logger.error(f"Price fetch error: {e}")
            raise

        # Step 3: Fetch positions for all addresses
        logger.info("Step 3: Fetching positions (this may take several minutes)...")
        try:
            positions = fetch_all_positions_async(unique_addresses, mark_prices, dexes=ALL_DEXES)
            logger.info(f"Fetched {len(positions)} positions")
        except Exception as e:
            logger.error(f"Position fetch error: {e}")
            raise

        # Step 4: Populate cache (with filtering and statistics tracking)
        logger.info("Step 4: Populating position cache...")
        cached_positions = []

        # Track filter statistics
        stats = {
            'total_positions': len(positions),
            'no_liq_price': 0,
            'below_notional': 0,
            'distance_too_far': 0,
            # Multi-filter combinations
            'no_liq_and_below_notional': 0,
            'no_liq_and_distance': 0,
            'below_notional_and_distance': 0,
            'all_three_filters': 0,
            'passed_filters': 0,
        }

        # Known exchange prefixes for token sanitization
        known_exchanges = {"xyz", "flx", "vntl", "hyna", "km"}

        for pos in positions:
            # Get mark price for this position
            token = pos.token
            exchange = pos.exchange
            price = get_current_price(token, exchange, mark_prices)

            if price and price > 0:
                # Sanitize token: strip exchange prefix if present (e.g., "xyz:GOLD" -> "GOLD")
                clean_token = token
                if ":" in token:
                    prefix, rest = token.split(":", 1)
                    if prefix in known_exchanges:
                        clean_token = rest

                # Track filter reasons
                has_no_liq = pos.liquidation_price is None
                below_notional = not passes_watchlist_threshold(
                    token=clean_token,
                    exchange=exchange,
                    is_isolated=pos.is_isolated,
                    position_value=pos.position_value
                )

                # Calculate distance for distance filter
                distance_too_far = False
                if not has_no_liq and pos.liquidation_price and pos.liquidation_price > 0:
                    if pos.side.lower() == "long":
                        distance_pct = ((price - pos.liquidation_price) / price) * 100
                    else:  # short
                        distance_pct = ((pos.liquidation_price - price) / price) * 100
                    distance_too_far = distance_pct > MAX_WATCH_DISTANCE_PCT
                elif has_no_liq:
                    # No liq price means infinite distance (already filtered by has_no_liq)
                    distance_too_far = False  # Don't double-count

                # Count filter combinations
                filters_applied = sum([has_no_liq, below_notional, distance_too_far])

                if filters_applied == 3:
                    stats['all_three_filters'] += 1
                    continue
                elif filters_applied == 2:
                    if has_no_liq and below_notional:
                        stats['no_liq_and_below_notional'] += 1
                    elif has_no_liq and distance_too_far:
                        stats['no_liq_and_distance'] += 1
                    elif below_notional and distance_too_far:
                        stats['below_notional_and_distance'] += 1
                    continue
                elif filters_applied == 1:
                    if has_no_liq:
                        stats['no_liq_price'] += 1
                    elif below_notional:
                        stats['below_notional'] += 1
                    elif distance_too_far:
                        stats['distance_too_far'] += 1
                    continue

                # Position passes all filters
                stats['passed_filters'] += 1
                cached = CachedPosition.from_position_dict(
                    vars(pos) if hasattr(pos, '__dict__') else pos,
                    pos.cohort,
                    price
                )
                cached_positions.append(cached)

        # Store stats for daily summary
        self._last_scan_stats = stats

        # Calculate totals for logging
        single_filter = stats['no_liq_price'] + stats['below_notional'] + stats['distance_too_far']
        multi_filter = (stats['no_liq_and_below_notional'] + stats['no_liq_and_distance'] +
                        stats['below_notional_and_distance'] + stats['all_three_filters'])

        logger.info(f"Filter stats: {stats['total_positions']} total, "
                   f"{stats['no_liq_price']} no liq, "
                   f"{stats['below_notional']} below notional, "
                   f"{stats['distance_too_far']} distance >{MAX_WATCH_DISTANCE_PCT}%, "
                   f"{multi_filter} multi-filter, "
                   f"{stats['passed_filters']} passed")

        # Batch save to cache
        self.position_cache.update_positions_batch(cached_positions)

        # Step 5: Save known addresses and register in wallet registry
        logger.info("Step 5: Recording known addresses and wallet registry...")
        self.discovery_scheduler.known_addresses = {addr for addr, _ in unique_addresses}
        self.db.save_known_addresses_batch(unique_addresses)

        # Register all wallets in the unified wallet registry (Column A - non-decreasing)
        wallet_registrations = [
            {'address': addr, 'source': 'hyperdash', 'cohort': cohort}
            for addr, cohort in unique_addresses
        ]
        self.db.register_wallets_batch(wallet_registrations)

        # Calculate per-wallet position values for scan frequency classification
        wallet_position_values: Dict[str, float] = {}
        wallet_position_counts: Dict[str, int] = {}
        for pos in cached_positions:
            addr = pos.address
            if addr not in wallet_position_values:
                wallet_position_values[addr] = 0.0
                wallet_position_counts[addr] = 0
            wallet_position_values[addr] += pos.position_value
            wallet_position_counts[addr] += 1

        # Update wallet registry with scan results (for frequency classification)
        wallet_results = [
            {
                'address': addr,
                'position_value': wallet_position_values.get(addr, 0.0),
                'total_collateral': 0.0,  # Not tracked separately yet
                'position_count': wallet_position_counts.get(addr, 0),
            }
            for addr, _ in unique_addresses
        ]
        self.db.update_wallet_scan_results_batch(wallet_results, WALLET_ACTIVE_THRESHOLD)

        # Record scan time
        self.db.set_last_scan_time(scan_start)

        # Prune old data immediately after initial scan
        logger.info("Step 6: Pruning old database data...")
        try:
            deleted = self.db.prune_old_data()
            stale_count = self.db.delete_stale_positions(CACHE_PRUNE_AGE_HOURS)
            logger.info(f"Pruned: {deleted.get('position_history', 0)} history, "
                       f"{deleted.get('alert_log', 0)} alerts, "
                       f"{deleted.get('service_logs', 0)} logs, "
                       f"{stale_count} stale positions")
            self._last_prune_time = time.time()
        except Exception as e:
            logger.warning(f"Failed to prune old data: {e}")

        scan_duration = (datetime.now(timezone.utc) - scan_start).total_seconds()
        tier_counts = self.position_cache.get_tier_counts()

        # Get wallet registry stats for snapshot
        wallet_stats = self.db.get_wallet_registry_stats()

        # Calculate total position value
        total_position_value = sum(p.position_value for p in cached_positions)

        # Log comprehensive scan snapshot (Column A scan complete)
        self.db.log_scan_snapshot(
            scan_type='comprehensive',
            total_wallets_scanned=len(unique_addresses),
            wallets_from_hyperdash=len(unique_addresses),  # All from hyperdash in initial scan
            wallets_from_liq_history=0,
            wallets_normal_frequency=wallet_stats.get('by_frequency', {}).get('normal', 0),
            wallets_infrequent=wallet_stats.get('by_frequency', {}).get('infrequent', 0),
            positions_found=len(cached_positions),
            positions_with_liq_price=len(cached_positions),  # All have liq price (filtered)
            total_position_value=total_position_value,
            scan_duration_seconds=scan_duration,
            notes=f"Initial scan: {stats['total_positions']} total positions, "
                  f"{stats['passed_filters']} passed filters"
        )

        logger.info("=" * 60)
        logger.info(f"INITIAL SCAN COMPLETE ({scan_duration:.1f}s)")
        logger.info(f"Cached positions: {len(self.position_cache.positions)}")
        logger.info(f"  Critical (≤{CACHE_TIER_CRITICAL_PCT}%): {tier_counts['critical']}")
        logger.info(f"  High ({CACHE_TIER_CRITICAL_PCT}-{CACHE_TIER_HIGH_PCT}%): {tier_counts['high']}")
        logger.info(f"  Normal (>{CACHE_TIER_HIGH_PCT}%): {tier_counts['normal']}")
        logger.info(f"Wallet registry: {wallet_stats.get('total', 0)} total, "
                   f"{wallet_stats.get('by_frequency', {}).get('normal', 0)} normal, "
                   f"{wallet_stats.get('by_frequency', {}).get('infrequent', 0)} infrequent")
        logger.info("=" * 60)

        # Send startup notification
        self.alerts.send_service_status("started")

    def _run_main_loop(self):
        """
        Continuous main loop with tiered refresh and dynamic discovery.

        1. Check for daily summary
        2. Fetch mark prices (single API call)
        3. Update cache with new prices
        4. Process tiered refresh queue
        5. Check proximity alerts
        6. Maybe run discovery
        """
        logger.info("Entering main monitoring loop...")

        last_price_fetch = 0
        price_fetch_interval = 1.0  # Fetch prices every second

        while self.running:
            try:
                loop_start = time.time()

                # 1. Check for daily summary
                self._maybe_send_daily_summary()

                # 2. Fetch mark prices (throttled)
                if time.time() - last_price_fetch >= price_fetch_interval:
                    try:
                        mark_prices = fetch_all_mark_prices_async(ALL_DEXES)
                        last_price_fetch = time.time()

                        # 3. Update cache with new prices
                        self.position_cache.update_prices(mark_prices)
                    except Exception as e:
                        logger.warning(f"Price fetch failed: {e}")
                        mark_prices = {}

                # 4. Process tiered refresh queue
                self._process_refresh_queue(mark_prices if 'mark_prices' in dir() else {})

                # 5. Check proximity alerts
                self._check_proximity_alerts()

                # 6. Maybe run discovery (if API budget allows)
                if self.discovery_scheduler.should_run_discovery():
                    self._run_discovery()

                # 7. Periodic maintenance
                self._record_position_snapshots()
                self._maybe_prune_data()

                # Short sleep for responsive critical refresh
                elapsed = time.time() - loop_start
                sleep_time = max(0.1, CACHE_REFRESH_CRITICAL_SEC - elapsed)
                time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(5)

    def _process_refresh_queue(self, mark_prices: Dict[str, float]):
        """Process tiered refresh queue, refreshing positions that need it."""
        # Get positions needing refresh (up to 5 per cycle to stay within rate limits)
        to_refresh = self.refresh_scheduler.get_positions_to_refresh(max_count=5)

        if not to_refresh:
            return

        positions_to_remove = []

        for position_key in to_refresh:
            pos = self.position_cache.positions.get(position_key)
            if not pos:
                continue

            try:
                # Fetch fresh position data
                dexes = [""] if pos.exchange == "main" else [pos.exchange]
                raw_positions = fetch_all_positions_for_address(
                    pos.address,
                    mark_prices,
                    dexes=dexes
                )

                # Find matching position - raw_positions is List[(position_dict, exchange_name)]
                position_found = False
                for position_data, exchange_name in raw_positions:
                    # Parse raw API data into Position object
                    fresh_pos = parse_position(
                        pos.address,
                        pos.cohort,
                        position_data,
                        mark_prices,
                        exchange_name
                    )
                    if fresh_pos is None:
                        continue

                    fresh_key = f"{fresh_pos.address}:{fresh_pos.token}:{fresh_pos.exchange}:{fresh_pos.side}"
                    if fresh_key == position_key:
                        position_found = True
                        # Update cached position
                        price = get_current_price(fresh_pos.token, fresh_pos.exchange, mark_prices)
                        if price and price > 0:
                            # Check for partial liquidation (position value dropped significantly)
                            old_position_value = pos.position_value
                            new_position_value = fresh_pos.position_value

                            if old_position_value > 0 and new_position_value > 0:
                                value_drop_pct = (old_position_value - new_position_value) / old_position_value
                                # If value dropped more than 10%, might be partial liquidation
                                if value_drop_pct >= 0.10:
                                    # Check if this is in critical zone (likely liquidation vs voluntary close)
                                    is_critical = pos.distance_pct is not None and pos.distance_pct <= CRITICAL_ZONE_PCT
                                    if is_critical:
                                        logger.warning(
                                            f"PARTIAL LIQUIDATION: {pos.token} {pos.side} "
                                            f"value dropped {value_drop_pct*100:.1f}% "
                                            f"(${old_position_value/1e6:.2f}M -> ${new_position_value/1e6:.2f}M)"
                                        )
                                        is_isolated = pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'
                                        self.alerts.send_liquidation_alert_simple(
                                            token=pos.token,
                                            side=pos.side,
                                            address=pos.address,
                                            position_value=old_position_value,
                                            liq_price=pos.liq_price or 0,
                                            liquidation_type="partial",
                                            new_value=new_position_value,
                                            is_isolated=is_isolated,
                                            exchange=pos.exchange,
                                        )

                            # Update position data
                            pos.size = fresh_pos.size
                            pos.leverage = fresh_pos.leverage
                            pos.leverage_type = fresh_pos.leverage_type
                            pos.entry_price = fresh_pos.entry_price
                            pos.position_value = fresh_pos.position_value
                            pos.liq_price = fresh_pos.liquidation_price
                            pos.margin_used = fresh_pos.margin_used
                            pos.unrealized_pnl = fresh_pos.unrealized_pnl
                            pos.update_price(price)
                            pos.last_full_refresh = datetime.now(timezone.utc)

                            # Persist update
                            self.position_cache.update_position(pos)
                        break

                # Position not found - check if it was liquidated
                if not position_found:
                    # Only alert if the position was in critical zone (high probability of liquidation)
                    was_critical = pos.distance_pct is not None and pos.distance_pct <= CRITICAL_ZONE_PCT
                    if was_critical:
                        logger.warning(
                            f"FULL LIQUIDATION: {pos.token} {pos.side} position disappeared "
                            f"(was ${pos.position_value/1e6:.2f}M at {pos.distance_pct:.3f}%)"
                        )
                        is_isolated = pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'
                        self.alerts.send_liquidation_alert_simple(
                            token=pos.token,
                            side=pos.side,
                            address=pos.address,
                            position_value=pos.position_value,
                            liq_price=pos.liq_price or 0,
                            liquidation_type="full",
                            is_isolated=is_isolated,
                            exchange=pos.exchange,
                        )
                    else:
                        logger.info(f"Position {position_key} closed (was at {pos.distance_pct:.2f}% - likely voluntary)")

                    # Mark for removal from cache
                    positions_to_remove.append(position_key)

                self.refresh_scheduler.mark_refreshed(position_key)

            except Exception as e:
                logger.warning(f"Failed to refresh {position_key}: {e}")
                self.refresh_scheduler.mark_refreshed(position_key)  # Mark to avoid immediate retry

        # Remove liquidated/closed positions from cache
        for key in positions_to_remove:
            self.position_cache.remove_position(key)
            # Also clean up alert state
            if key in self._alerted_positions:
                del self._alerted_positions[key]
            logger.info(f"Removed position from cache: {key}")

    def _check_proximity_alerts(self):
        """Check positions for proximity alerts and send notifications."""
        for key, pos in self.position_cache.positions.items():
            if pos.distance_pct is None or pos.liq_price is None:
                continue

            # Check watchlist threshold
            # Sub-exchanges are always isolated, regardless of leverage_type
            is_isolated = pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'
            threshold = get_watchlist_threshold(pos.token, pos.exchange, is_isolated)
            if pos.position_value < threshold:
                continue

            alert_state = self._alerted_positions.get(key, {
                'alerted_proximity': False,
                'alerted_critical': False,
                'in_critical_zone': False,
            })

            distance = pos.distance_pct
            was_critical = alert_state.get('in_critical_zone', False)

            # Recovery: was critical, now > RECOVERY_PCT
            # Only alert if ALERT_NATURAL_RECOVERY is enabled (default: False)
            if was_critical and distance > RECOVERY_PCT:
                logger.info(f"RECOVERY: {pos.token} {pos.side} recovered to {distance:.3f}%")
                if ALERT_NATURAL_RECOVERY:
                    self.alerts.send_recovery_alert_simple(
                        token=pos.token,
                        side=pos.side,
                        address=pos.address,
                        distance_pct=distance,
                        liq_price=pos.liq_price,
                        mark_price=pos.mark_price,
                        position_value=pos.position_value,
                        is_isolated=(pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'),
                    )
                alert_state['alerted_proximity'] = False
                alert_state['alerted_critical'] = False
                alert_state['in_critical_zone'] = False

            # Critical alert: crossed below CRITICAL_ALERT_PCT
            elif distance <= CRITICAL_ALERT_PCT and not alert_state.get('alerted_critical', False):
                logger.warning(f"CRITICAL: {pos.token} {pos.side} at {distance:.3f}%")
                self.alerts.send_critical_alert_simple(
                    token=pos.token,
                    side=pos.side,
                    address=pos.address,
                    distance_pct=distance,
                    liq_price=pos.liq_price,
                    mark_price=pos.mark_price,
                    position_value=pos.position_value,
                    is_isolated=(pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'),
                    previous_distance=pos.distance_at_last_summary,
                )
                alert_state['alerted_critical'] = True
                alert_state['in_critical_zone'] = True

            # Proximity alert: crossed below CRITICAL_ZONE_PCT
            elif distance <= CRITICAL_ZONE_PCT and not alert_state.get('alerted_proximity', False):
                logger.info(f"PROXIMITY: {pos.token} {pos.side} at {distance:.3f}%")
                self.alerts.send_proximity_alert_simple(
                    token=pos.token,
                    side=pos.side,
                    address=pos.address,
                    distance_pct=distance,
                    liq_price=pos.liq_price,
                    mark_price=pos.mark_price,
                    position_value=pos.position_value,
                    is_isolated=(pos.leverage_type.lower() == 'isolated' or pos.exchange != 'main'),
                    previous_distance=pos.distance_at_last_summary,
                )
                alert_state['alerted_proximity'] = True
                alert_state['in_critical_zone'] = True

            # Update critical zone tracking
            alert_state['in_critical_zone'] = distance <= CRITICAL_ZONE_PCT

            self._alerted_positions[key] = alert_state

    def _run_discovery(self):
        """
        Run discovery scan to find new addresses and scan due wallets.

        Uses wallet registry to determine which wallets to scan:
        1. Never-scanned wallets (position_value = NULL)
        2. Normal frequency wallets (always)
        3. Infrequent wallets (if last_scanned > INFREQUENT_SCAN_INTERVAL_HOURS ago)

        Also discovers new addresses from cohorts and liquidation history.
        """
        logger.info("Running discovery scan...")
        scan_start = datetime.now(timezone.utc)

        try:
            # Step 1: Get wallets due for scanning from registry
            wallets_to_scan = self.db.get_wallets_to_scan(INFREQUENT_SCAN_INTERVAL_HOURS)
            registry_addresses = {w['address'] for w in wallets_to_scan}

            # Step 2: Fetch current cohorts to discover NEW addresses
            traders = fetch_cohorts(ALL_COHORTS, delay=1.0)

            def passes_filters(t):
                if t.position_value < MIN_WALLET_POSITION_VALUE:
                    return False
                if t.leverage <= 1.0 and t.perp_bias.startswith("Long"):
                    return False
                return True

            traders = [t for t in traders if passes_filters(t)]

            # Find truly new addresses (not in registry at all)
            new_from_cohorts = []
            for t in traders:
                if t.address not in registry_addresses:
                    new_from_cohorts.append({'address': t.address, 'source': 'hyperdash', 'cohort': t.cohort})

            # Step 3: Check liquidation history for new addresses
            new_from_liq_history = []
            if self.liq_history_db:
                liq_addresses = self.liq_history_db.get_addresses_for_discovery(
                    min_notional=100_000,  # $100K minimum liquidation
                    max_scan_age_hours=24
                )
                existing_registry = self.db.get_all_wallet_addresses()
                for addr, max_notional in liq_addresses:
                    if addr not in existing_registry:
                        new_from_liq_history.append({
                            'address': addr,
                            'source': 'liq_history',
                            'cohort': 'liq_history'
                        })

            # Step 4: Register new wallets in registry
            all_new_wallets = new_from_cohorts + new_from_liq_history
            if all_new_wallets:
                self.db.register_wallets_batch(all_new_wallets)
                logger.info(f"Registered {len(new_from_cohorts)} from cohorts, "
                           f"{len(new_from_liq_history)} from liq history")

            # Step 5: Build final scan list (registry due + new wallets)
            addresses_to_scan = []
            for w in wallets_to_scan:
                addresses_to_scan.append((w['address'], w.get('cohort', 'unknown')))
            for w in all_new_wallets:
                addresses_to_scan.append((w['address'], w.get('cohort', 'unknown')))

            if not addresses_to_scan:
                logger.info("Discovery complete: no wallets to scan")
                self.discovery_scheduler.mark_discovery_complete([])
                return

            logger.info(f"Discovery: scanning {len(addresses_to_scan)} wallets "
                       f"({len(wallets_to_scan)} from registry, {len(all_new_wallets)} new)")

            # Step 6: Fetch positions
            mark_prices = fetch_all_mark_prices_async(ALL_DEXES)
            positions = fetch_all_positions_async(addresses_to_scan, mark_prices, dexes=ALL_DEXES)

            # Step 7: Process positions and update cache
            known_exchanges = {"xyz", "flx", "vntl", "hyna", "km"}
            added_count = 0
            filtered_count = 0
            wallet_position_values: Dict[str, float] = {}
            wallet_position_counts: Dict[str, int] = {}

            for pos in positions:
                token = pos.token
                exchange = pos.exchange
                price = get_current_price(token, exchange, mark_prices)

                # Track per-wallet stats regardless of filter outcome
                addr = pos.address
                if addr not in wallet_position_values:
                    wallet_position_values[addr] = 0.0
                    wallet_position_counts[addr] = 0
                wallet_position_values[addr] += pos.position_value
                wallet_position_counts[addr] += 1

                if price and price > 0:
                    # Sanitize token
                    clean_token = token
                    if ":" in token:
                        prefix, rest = token.split(":", 1)
                        if prefix in known_exchanges:
                            clean_token = rest

                    # Filter: must have liquidation price
                    if pos.liquidation_price is None:
                        filtered_count += 1
                        continue

                    # Filter: notional threshold
                    if not passes_watchlist_threshold(
                        token=clean_token,
                        exchange=exchange,
                        is_isolated=pos.is_isolated,
                        position_value=pos.position_value
                    ):
                        filtered_count += 1
                        continue

                    # Filter: distance threshold
                    if pos.liquidation_price and pos.liquidation_price > 0:
                        if pos.side.lower() == "long":
                            distance_pct = ((price - pos.liquidation_price) / price) * 100
                        else:
                            distance_pct = ((pos.liquidation_price - price) / price) * 100
                        if distance_pct > MAX_WATCH_DISTANCE_PCT:
                            filtered_count += 1
                            continue

                    cached = CachedPosition.from_position_dict(
                        vars(pos) if hasattr(pos, '__dict__') else pos,
                        pos.cohort,
                        price
                    )
                    self.position_cache.update_position(cached)
                    added_count += 1

            # Step 8: Update wallet registry with scan results
            wallet_results = [
                {
                    'address': addr,
                    'position_value': wallet_position_values.get(addr, 0.0),
                    'total_collateral': 0.0,
                    'position_count': wallet_position_counts.get(addr, 0),
                }
                for addr, _ in addresses_to_scan
            ]
            self.db.update_wallet_scan_results_batch(wallet_results, WALLET_ACTIVE_THRESHOLD)

            # Step 9: Log scan snapshot
            scan_duration = (datetime.now(timezone.utc) - scan_start).total_seconds()
            total_position_value = sum(wallet_position_values.values())

            # Count wallets by source
            wallets_from_hyperdash = sum(1 for w in wallets_to_scan if w.get('source') == 'hyperdash')
            wallets_from_liq = sum(1 for w in wallets_to_scan if w.get('source') == 'liq_history')
            wallets_normal = sum(1 for w in wallets_to_scan if w.get('scan_frequency') == 'normal')
            wallets_infreq = sum(1 for w in wallets_to_scan if w.get('scan_frequency') == 'infrequent')

            self.db.log_scan_snapshot(
                scan_type='discovery',
                total_wallets_scanned=len(addresses_to_scan),
                wallets_from_hyperdash=wallets_from_hyperdash + len(new_from_cohorts),
                wallets_from_liq_history=wallets_from_liq + len(new_from_liq_history),
                wallets_normal_frequency=wallets_normal,
                wallets_infrequent=wallets_infreq,
                positions_found=added_count,
                positions_with_liq_price=added_count,
                total_position_value=total_position_value,
                scan_duration_seconds=scan_duration,
                notes=f"Discovery: {len(all_new_wallets)} new wallets, {filtered_count} positions filtered"
            )

            # Mark discovery complete
            self.discovery_scheduler.mark_discovery_complete(
                [(addr, cohort) for addr, cohort in addresses_to_scan if addr in [w['address'] for w in all_new_wallets]]
            )

            logger.info(
                f"Discovery complete ({scan_duration:.1f}s): added {added_count} positions from "
                f"{len(addresses_to_scan)} wallets ({filtered_count} filtered)"
            )

        except Exception as e:
            logger.error(f"Discovery scan failed: {e}")
            self.discovery_scheduler.mark_discovery_complete([])

    def _maybe_send_daily_summary(self):
        """Send daily summary at configured times (7am and 4pm EST)."""
        now = datetime.now(EST)
        today = now.date()

        # Reset tracking on new day
        if self._last_summary_date != today:
            self._summaries_sent_today = set()
            self._last_summary_date = today

        for hour, minute in DAILY_SUMMARY_TIMES:
            summary_key = (hour, minute)
            if summary_key in self._summaries_sent_today:
                continue

            # Check if we're past the summary time
            summary_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= summary_time:
                self._send_daily_summary(hour, minute)
                self._summaries_sent_today.add(summary_key)
                logger.info(f"Sent daily summary for {hour:02d}:{minute:02d} EST")

    def _send_daily_summary(self, scheduled_hour: int, scheduled_minute: int):
        """Send the daily watchlist summary to Telegram."""
        from .alerts import send_daily_summary
        send_daily_summary(
            self.position_cache,
            self.alerts,
            self.discovery_scheduler,
            scheduled_hour,
            scheduled_minute,
            self._last_scan_stats
        )

    def _record_position_snapshots(self):
        """Record position snapshots for history tracking (throttled)."""
        now = time.time()

        # Only snapshot every 5 minutes
        if now - self._last_snapshot_time < 300:
            return

        try:
            snapshots = [
                {
                    'position_key': pos.position_key,
                    'liq_price': pos.liq_price or 0,
                    'position_value': pos.position_value,
                    'distance_pct': pos.distance_pct or 0,
                    'mark_price': pos.mark_price,
                }
                for pos in self.position_cache.positions.values()
                if pos.distance_pct is not None and pos.liq_price is not None
            ]

            if snapshots:
                self.db.record_position_snapshots_batch(snapshots)

            self._last_snapshot_time = now

        except Exception as e:
            logger.warning(f"Failed to record position snapshots: {e}")

    def _maybe_prune_data(self):
        """Periodically prune old data (once per day)."""
        now = time.time()

        # Prune once per day
        if now - self._last_prune_time < 86400:
            return

        try:
            # Prune database tables
            self.db.prune_old_data()

            # Prune stale cached positions
            self.db.delete_stale_positions(CACHE_PRUNE_AGE_HOURS)

            # Clean up refresh scheduler
            self.refresh_scheduler.clear_stale_entries()

            self._last_prune_time = now

        except Exception as e:
            logger.warning(f"Failed to prune old data: {e}")

    def run(self):
        """
        Main entry point - run the continuous monitor loop.

        1. Try to restore from cache
        2. If cache invalid, run initial comprehensive scan
        3. Enter main monitoring loop with tiered refresh
        """
        self.running = True
        logger.info("=" * 60)
        logger.info("LIQUIDATION MONITOR SERVICE STARTING")
        logger.info("=" * 60)
        logger.info(f"Mode: Cache-based with tiered refresh")
        logger.info(f"Tiers: Critical ≤{CACHE_TIER_CRITICAL_PCT}%, High ≤{CACHE_TIER_HIGH_PCT}%")
        logger.info(f"Daily summaries at: {', '.join(f'{h:02d}:{m:02d} EST' for h, m in DAILY_SUMMARY_TIMES)}")
        logger.info(f"Dry run: {self.dry_run}")

        try:
            # Try to restore from cache
            if not self._restore_from_cache():
                # Run initial scan
                self._run_initial_scan()
            else:
                # Refresh prices immediately after restore
                logger.info("Refreshing prices after cache restore...")
                try:
                    mark_prices = fetch_all_mark_prices_async(ALL_DEXES)
                    self.position_cache.update_prices(mark_prices)
                    logger.info("Prices refreshed, cache up to date")
                except Exception as e:
                    logger.warning(f"Price refresh failed: {e}")

                # Send startup notification
                tier_counts = self.position_cache.get_tier_counts()
                self.alerts.send_service_status(
                    "started",
                    f"Restored from cache\n"
                    f"Positions: {len(self.position_cache.positions)}\n"
                    f"Critical: {tier_counts['critical']} | High: {tier_counts['high']} | Normal: {tier_counts['normal']}"
                )

            # Send startup watchlist summary (same format as daily summary)
            # Default: skip to avoid duplicate messages on redeployment
            if self.send_startup_summary:
                logger.info("Sending startup watchlist summary (--startup-summary)...")
                now_est = datetime.now(EST)
                self._send_daily_summary(now_est.hour, now_est.minute)
            else:
                logger.info("Skipping startup watchlist summary (use --startup-summary to enable)")

            # Enter main loop
            self._run_main_loop()

        except KeyboardInterrupt:
            logger.info("Service interrupted by user")
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error: {type(e).__name__}"
            logger.error(f"Service error: {error_msg}")
            self.alerts.send_service_status("error", error_msg)
            raise
        except Exception as e:
            error_msg = f"Unexpected error: {type(e).__name__}"
            logger.error(f"Service error: {error_msg}")
            self.alerts.send_service_status("error", error_msg)
            raise
        finally:
            logger.info("LIQUIDATION MONITOR SERVICE STOPPED")
            self.alerts.send_service_status("stopped")

    def stop(self):
        """Stop the monitor service gracefully."""
        self.running = False
