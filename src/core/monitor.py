"""
Monitor Service

Main monitoring loop that:
1. Refreshes mark prices
2. Updates position distances and bucket classifications
3. Processes positions by bucket (critical > high > normal)
4. Runs discovery periodically to find new positions
5. Sends alerts on proximity/critical thresholds
"""

import asyncio
import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from ..config import Position, Bucket, config


@dataclass(order=True)
class QueuedPosition:
    """Position in the priority queue, ordered by distance to liquidation."""
    priority: float  # distance_pct (lower = more urgent)
    next_refresh: datetime = field(compare=False)
    position_key: str = field(compare=False)
from ..api.hyperliquid import HyperliquidClient
from ..api.hyperdash import HyperdashClient
from ..db.wallet_db import WalletDB
from ..db.position_db import PositionDB, CachedPosition
from .position_fetcher import PositionFetcher

if TYPE_CHECKING:
    from ..alerts.telegram import TelegramAlerts

logger = logging.getLogger(__name__)


class Monitor:
    """
    Main monitoring service.

    Implements priority-based monitoring where positions are processed
    in strict order of urgency (distance to liquidation). Uses a min-heap
    to always process the most critical position first.

    Refresh intervals by bucket:
    - Critical (<=0.125%): every 0.5s
    - High (0.125-0.25%): every 3s
    - Normal (>0.25%): every 30s

    Alerts are sent asynchronously (fire-and-forget) to avoid blocking
    the processing loop while waiting for Telegram responses.

    Discovery runs periodically to find new positions.
    """

    def __init__(
        self,
        wallet_db: WalletDB = None,
        position_db: PositionDB = None,
        telegram_alerts: "TelegramAlerts" = None,
        dry_run: bool = False,
    ):
        """
        Initialize the monitor.

        Args:
            wallet_db: Wallet registry database
            position_db: Position cache database
            telegram_alerts: TelegramAlerts instance for rich alerts
            dry_run: If True, log alerts instead of sending
        """
        self.wallet_db = wallet_db or WalletDB()
        self.position_db = position_db or PositionDB()
        self.telegram_alerts = telegram_alerts
        self.dry_run = dry_run

        self._client: Optional[HyperliquidClient] = None
        self._fetcher: Optional[PositionFetcher] = None
        self._running = False

        # Priority queue for position processing
        self._queue: List[QueuedPosition] = []

        # Timing state
        self._last_price_refresh = datetime.min.replace(tzinfo=timezone.utc)
        self._last_discovery = datetime.min.replace(tzinfo=timezone.utc)

        # Discovery interval (adaptive)
        self._discovery_interval = timedelta(minutes=30)

    async def start(self):
        """Start the monitor."""
        logger.info("Starting monitor...")
        self._running = True

        self._client = HyperliquidClient()
        await self._client._ensure_session()
        self._fetcher = PositionFetcher(self._client)

        # Initial discovery - don't crash on failure, continue with empty cache
        try:
            await self._run_discovery()
            self._rebuild_queue()
        except Exception as e:
            logger.error(f"Initial discovery failed: {e}, continuing with empty cache")
            # Discovery will be retried in the main loop

        # Main loop
        try:
            await self._main_loop()
        finally:
            await self.stop()

    async def stop(self):
        """Stop the monitor."""
        logger.info("Stopping monitor...")
        self._running = False

        if self._client:
            await self._client.close()
            self._client = None

    async def _main_loop(self):
        """Main monitoring loop using priority queue."""
        while self._running:
            now = datetime.now(timezone.utc)

            try:
                # 1. Refresh mark prices periodically
                if (now - self._last_price_refresh).total_seconds() >= config.price_refresh_sec:
                    await self._refresh_prices()
                    self._last_price_refresh = now

                # 2. Process next eligible position (highest priority first)
                processed = await self._process_next_position(now)

                # 3. Run discovery periodically
                if (now - self._last_discovery) >= self._discovery_interval:
                    await self._run_discovery()
                    self._rebuild_queue()
                    self._last_discovery = now

                # Brief sleep if nothing was processed
                if not processed:
                    await asyncio.sleep(0.05)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                await asyncio.sleep(5)  # Back off on error

    async def _refresh_prices(self):
        """Refresh mark prices from API."""
        if self._fetcher:
            await self._fetcher.refresh_mark_prices()

    async def _process_next_position(self, now: datetime) -> bool:
        """
        Process the highest priority eligible position.

        Pops from the priority queue, processes if eligible, and requeues
        with updated priority based on new distance.

        Returns:
            True if a position was processed, False otherwise
        """
        while self._queue:
            item = heapq.heappop(self._queue)

            # Check if eligible for refresh
            if item.next_refresh > now:
                heapq.heappush(self._queue, item)  # Not ready, push back
                return False

            # Get cached position from DB
            cached = self.position_db.get_position(item.position_key)
            if not cached:
                continue  # Position no longer exists, skip

            # Process and get new distance
            try:
                new_distance = await self._process_position(cached)
            except Exception as e:
                logger.warning(f"Error processing {item.position_key}: {e}")
                new_distance = cached.distance_pct  # Keep old distance on error

            # Requeue with updated priority and refresh time
            new_bucket = config.classify_bucket(new_distance)
            refresh_sec = self._get_refresh_interval(new_bucket)

            heapq.heappush(self._queue, QueuedPosition(
                priority=new_distance if new_distance is not None else 999,
                next_refresh=now + timedelta(seconds=refresh_sec),
                position_key=item.position_key,
            ))
            return True

        return False

    def _get_refresh_interval(self, bucket: Bucket) -> float:
        """Get refresh interval in seconds for a bucket."""
        if bucket == Bucket.CRITICAL:
            return config.critical_refresh_sec
        elif bucket == Bucket.HIGH:
            return config.high_refresh_sec
        return config.normal_refresh_sec

    def _rebuild_queue(self):
        """Rebuild priority queue from all positions in DB."""
        self._queue.clear()
        now = datetime.now(timezone.utc)

        for bucket in [Bucket.CRITICAL, Bucket.HIGH, Bucket.NORMAL]:
            for cached in self.position_db.get_positions_by_bucket(bucket):
                distance = cached.distance_pct if cached.distance_pct is not None else 999
                heapq.heappush(self._queue, QueuedPosition(
                    priority=distance,
                    next_refresh=now,  # Eligible immediately after discovery
                    position_key=cached.key,
                ))

        logger.debug(f"Priority queue rebuilt with {len(self._queue)} positions")

    async def _process_position(self, cached: CachedPosition) -> Optional[float]:
        """
        Process a single position.

        - Update mark price
        - Recalculate distance
        - Check for state transitions and send alerts
        - Update database

        Alert rules (state-based):
        - Transition INTO greater danger (NORMAL→HIGH, HIGH→CRITICAL, NORMAL→CRITICAL): Alert
        - Transition INTO less danger: No alert, UNLESS collateral was added while in HIGH/CRITICAL

        Returns:
            New distance to liquidation (or None if unavailable)
        """
        position = cached.position
        previous_bucket = cached.bucket

        # Get current mark price
        mark_price = self._fetcher.get_mark_price(position.token, position.exchange)
        if mark_price:
            position.mark_price = mark_price

        # Calculate distance and new bucket
        distance = position.distance_to_liq()
        new_bucket = config.classify_bucket(distance)

        # Check for state transitions and send alerts
        if distance is not None:
            self._check_bucket_transitions(
                position=position,
                distance=distance,
                previous_bucket=previous_bucket,
                new_bucket=new_bucket,
                previous_liq_price=cached.previous_liq_price,
            )

        # Update position in database
        self.position_db.upsert_position(position, distance)

        return distance

    def _check_bucket_transitions(
        self,
        position: Position,
        distance: float,
        previous_bucket: Bucket,
        new_bucket: Bucket,
        previous_liq_price: Optional[float],
    ):
        """
        Check for bucket state transitions and send appropriate alerts.

        Alerts on transitions INTO greater danger:
        - NORMAL → HIGH: Proximity alert (approaching liquidation)
        - HIGH → CRITICAL: Critical alert (imminent liquidation)
        - NORMAL → CRITICAL: Critical alert (imminent liquidation)

        Alerts on recovery from CRITICAL (only if collateral was added):
        - CRITICAL → HIGH/NORMAL: Collateral added notification
        """
        # Transition INTO greater danger
        if new_bucket == Bucket.HIGH and previous_bucket == Bucket.NORMAL:
            # NORMAL → HIGH: Approaching liquidation
            self._send_proximity_alert_async(position, distance)

        elif new_bucket == Bucket.CRITICAL and previous_bucket in (Bucket.NORMAL, Bucket.HIGH):
            # NORMAL/HIGH → CRITICAL: Imminent liquidation
            self._send_critical_alert_async(position, distance)

        # Transition OUT of CRITICAL (check for collateral addition)
        elif self._is_critical_recovery(previous_bucket, new_bucket):
            # Was in CRITICAL, now safer - check if collateral was added
            if self._detected_collateral_addition(position, previous_liq_price):
                self._send_collateral_added_alert_async(
                    position=position,
                    distance=distance,
                    previous_bucket=previous_bucket,
                )

    def _is_critical_recovery(self, previous_bucket: Bucket, new_bucket: Bucket) -> bool:
        """Check if this is a recovery from CRITICAL to a safer state."""
        return (
            previous_bucket == Bucket.CRITICAL
            and new_bucket in (Bucket.HIGH, Bucket.NORMAL)
        )

    def _detected_collateral_addition(
        self,
        position: Position,
        previous_liq_price: Optional[float],
    ) -> bool:
        """
        Detect if collateral was added based on liquidation price change.

        When collateral is added:
        - For LONG: liq price moves DOWN (further from mark price)
        - For SHORT: liq price moves UP (further from mark price)

        Returns:
            True if collateral addition detected
        """
        if previous_liq_price is None or position.liquidation_price is None:
            return False
        if previous_liq_price <= 0:
            return False

        # Calculate percentage change in liquidation price
        liq_price_change_pct = abs(
            (position.liquidation_price - previous_liq_price) / previous_liq_price * 100
        )

        # Must exceed minimum threshold to be considered significant
        if liq_price_change_pct < config.collateral_change_min_pct:
            return False

        # Check direction of change
        if position.side.lower() == "long":
            # For longs, liq price should move DOWN (lower = safer)
            return position.liquidation_price < previous_liq_price
        else:
            # For shorts, liq price should move UP (higher = safer)
            return position.liquidation_price > previous_liq_price

    async def _run_discovery(self):
        """
        Run discovery to find new wallets and positions.

        1. Fetch cohort addresses from Hyperdash
        2. Add new addresses to wallet registry
        3. Scan wallets for positions
        4. Update position cache
        """
        logger.info("Running discovery...")

        # Get wallets from registry that need scanning
        wallets = self.wallet_db.get_wallets_for_scan()
        addresses = [w.address for w in wallets]

        # Also fetch fresh cohort data from Hyperdash
        try:
            async with HyperdashClient() as dash_client:
                unique_wallets = await dash_client.get_unique_addresses(config.cohorts)

                # Add new wallets to registry
                new_wallets = []
                for addr, wallet_info in unique_wallets.items():
                    if addr not in addresses:
                        # Classify based on totalNotional from Hyperdash
                        freq = "normal" if wallet_info.total_notional >= config.min_wallet_value else "infrequent"
                        new_wallets.append({
                            "address": addr,
                            "source": "hyperdash",
                            "cohort": wallet_info.cohort,
                            "position_value": wallet_info.total_notional,
                            "scan_frequency": freq,
                        })
                        addresses.append(addr)

                if new_wallets:
                    new_count, _ = self.wallet_db.add_wallets_batch(new_wallets)
                    normal_count = sum(1 for w in new_wallets if w["scan_frequency"] == "normal")
                    logger.info(f"Added {new_count} new wallets from Hyperdash ({normal_count} normal, {new_count - normal_count} infrequent)")
        except Exception as e:
            logger.warning(f"Failed to fetch Hyperdash cohorts: {e}, continuing with existing wallets")

        if not addresses:
            logger.warning("No addresses to scan")
            return

        logger.info(f"Scanning {len(addresses)} addresses...")

        # Fetch positions
        def progress(done, total):
            if done % 50 == 0 or done == total:
                logger.info(f"Discovery progress: {done}/{total}")

        positions = await self._fetcher.fetch_positions_batch(
            addresses,
            filter_by_threshold=True,
            progress_callback=progress,
        )

        # Filter to positions with liquidation price
        positions = self._fetcher.filter_with_liq_price(positions)

        logger.info(f"Found {len(positions)} positions with liquidation price")

        # Update position cache
        for position in positions:
            self.position_db.upsert_position(position)

        # Update wallet scan results
        wallet_positions: Dict[str, List[Position]] = {}
        for p in positions:
            if p.address not in wallet_positions:
                wallet_positions[p.address] = []
            wallet_positions[p.address].append(p)

        for addr in addresses:
            wallet_pos = wallet_positions.get(addr, [])
            total_value = sum(p.position_value for p in wallet_pos)
            self.wallet_db.update_scan_result(
                addr,
                position_value=total_value,
                position_count=len(wallet_pos),
            )

        # Adjust discovery interval based on critical positions
        critical_count = len(self.position_db.get_positions_by_bucket(Bucket.CRITICAL))
        high_count = len(self.position_db.get_positions_by_bucket(Bucket.HIGH))

        # More critical positions = longer discovery interval (focus on monitoring)
        pressure_minutes = critical_count * 15 + high_count * 5
        interval_minutes = min(max(30 + pressure_minutes, 30), 240)
        self._discovery_interval = timedelta(minutes=interval_minutes)

        logger.info(f"Discovery complete. Next discovery in {interval_minutes} minutes")

        # Log detailed summaries
        self._log_wallet_summary()
        self._log_position_summary()

    def _log_wallet_summary(self):
        """Log detailed wallet registry summary."""
        logger.info("=" * 60)
        logger.info("WALLET REGISTRY SUMMARY")
        logger.info("=" * 60)

        # Basic stats
        stats = self.wallet_db.get_stats()
        logger.info(f"Total wallets: {stats.total_wallets:,}")
        logger.info(f"  From Hyperdash: {stats.from_hyperdash:,}")
        logger.info(f"  From Liq History: {stats.from_liq_history:,}")
        logger.info(f"  Normal frequency: {stats.normal_frequency:,} ({stats.normal_frequency/max(stats.total_wallets,1)*100:.0f}%)")
        logger.info(f"  Infrequent: {stats.infrequent:,} ({stats.infrequent/max(stats.total_wallets,1)*100:.0f}%)")

        # Cohort breakdown
        cohorts = self.wallet_db.get_cohort_breakdown()
        if cohorts:
            logger.info("Hyperdash cohorts:")
            for cohort, total, normal, infreq in cohorts:
                logger.info(f"  {cohort}: {total:,} ({normal:,} normal, {infreq:,} infreq)")

        # Tier breakdown
        tiers = self.wallet_db.get_tier_breakdown()
        logger.info("Position value tiers:")
        for tier_name, count, total_value in tiers:
            if count > 0:
                logger.info(f"  {tier_name}: {count:,} wallets (${total_value:,.0f})")

    def _log_position_summary(self):
        """Log position cache summary."""
        stats = self.position_db.get_stats()
        logger.info("-" * 60)
        logger.info("POSITION CACHE SUMMARY")
        logger.info("-" * 60)
        logger.info(
            f"Total positions: {stats.total_positions:,} "
            f"(${stats.total_notional:,.0f} notional)"
        )
        logger.info(f"  Critical (<=0.125%): {stats.critical_count:,}")
        logger.info(f"  High (0.125-0.25%): {stats.high_count:,}")
        logger.info(f"  Normal (>0.25%): {stats.total_positions - stats.critical_count - stats.high_count:,}")
        logger.info("=" * 60)

    async def _send_proximity_alert(self, position: Position, distance: float):
        """Send proximity (APPROACHING LIQUIDATION) alert."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] APPROACHING LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.2f}%"
            )
        elif self.telegram_alerts:
            try:
                self.telegram_alerts.send_proximity_alert(
                    token=position.token,
                    side=position.side,
                    address=position.address,
                    distance_pct=distance,
                    liq_price=position.liquidation_price,
                    mark_price=position.mark_price,
                    position_value=position.position_value,
                    is_isolated=position.leverage_type.lower() == "isolated",
                    exchange=position.exchange,
                )
            except Exception as e:
                logger.error(f"Failed to send proximity alert: {e}")
        else:
            logger.info(
                f"APPROACHING LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.2f}%"
            )

    async def _send_critical_alert(self, position: Position, distance: float):
        """Send critical (IMMINENT LIQUIDATION) alert."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] IMMINENT LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.3f}%"
            )
        elif self.telegram_alerts:
            try:
                self.telegram_alerts.send_critical_alert(
                    token=position.token,
                    side=position.side,
                    address=position.address,
                    distance_pct=distance,
                    liq_price=position.liquidation_price,
                    mark_price=position.mark_price,
                    position_value=position.position_value,
                    is_isolated=position.leverage_type.lower() == "isolated",
                    exchange=position.exchange,
                )
            except Exception as e:
                logger.error(f"Failed to send critical alert: {e}")
        else:
            logger.info(
                f"IMMINENT LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.3f}%"
            )

    def _send_proximity_alert_async(self, position: Position, distance: float):
        """Send proximity alert without blocking (fire and forget)."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] APPROACHING LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.2f}%"
            )
        elif self.telegram_alerts:
            try:
                self.telegram_alerts.send_proximity_alert_async(
                    token=position.token,
                    side=position.side,
                    address=position.address,
                    distance_pct=distance,
                    liq_price=position.liquidation_price,
                    mark_price=position.mark_price,
                    position_value=position.position_value,
                    is_isolated=position.leverage_type.lower() == "isolated",
                    exchange=position.exchange,
                )
            except Exception as e:
                logger.error(f"Failed to queue proximity alert: {e}")
        else:
            logger.info(
                f"APPROACHING LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.2f}%"
            )

    def _send_critical_alert_async(self, position: Position, distance: float):
        """Send critical alert without blocking (fire and forget)."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] IMMINENT LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.3f}%"
            )
        elif self.telegram_alerts:
            try:
                self.telegram_alerts.send_critical_alert_async(
                    token=position.token,
                    side=position.side,
                    address=position.address,
                    distance_pct=distance,
                    liq_price=position.liquidation_price,
                    mark_price=position.mark_price,
                    position_value=position.position_value,
                    is_isolated=position.leverage_type.lower() == "isolated",
                    exchange=position.exchange,
                )
            except Exception as e:
                logger.error(f"Failed to queue critical alert: {e}")
        else:
            logger.info(
                f"IMMINENT LIQUIDATION: {position.token} {position.side} "
                f"${position.position_value:,.0f} at {distance:.3f}%"
            )

    def _send_collateral_added_alert_async(
        self,
        position: Position,
        distance: float,
        previous_bucket: Bucket,
    ):
        """Send collateral added alert without blocking (fire and forget)."""
        bucket_name = previous_bucket.value.upper()
        if self.dry_run:
            logger.info(
                f"[DRY RUN] COLLATERAL ADDED: {position.token} {position.side} "
                f"${position.position_value:,.0f} recovered from {bucket_name} to {distance:.2f}%"
            )
        elif self.telegram_alerts:
            try:
                self.telegram_alerts.send_collateral_added_alert_async(
                    token=position.token,
                    side=position.side,
                    address=position.address,
                    distance_pct=distance,
                    liq_price=position.liquidation_price,
                    position_value=position.position_value,
                    previous_bucket=bucket_name,
                    is_isolated=position.leverage_type.lower() == "isolated",
                    exchange=position.exchange,
                )
            except Exception as e:
                logger.error(f"Failed to queue collateral added alert: {e}")
        else:
            logger.info(
                f"COLLATERAL ADDED: {position.token} {position.side} "
                f"${position.position_value:,.0f} recovered from {bucket_name} to {distance:.2f}%"
            )


# =============================================================================
# Entry point for running monitor
# =============================================================================

async def run_monitor(dry_run: bool = False):
    """Run the monitor service."""
    monitor = Monitor(dry_run=dry_run)

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await monitor.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_monitor(dry_run=True))
