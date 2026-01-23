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
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from ..config import Position, Bucket, config
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

    Implements tiered monitoring where:
    - Critical positions (<=0.125% to liq) are refreshed fastest
    - High positions (0.125-0.25%) are refreshed medium
    - Normal positions (>0.25%) are refreshed slowly

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

        # Timing state
        self._last_price_refresh = datetime.min.replace(tzinfo=timezone.utc)
        self._last_discovery = datetime.min.replace(tzinfo=timezone.utc)
        self._last_critical_refresh = datetime.min.replace(tzinfo=timezone.utc)
        self._last_high_refresh = datetime.min.replace(tzinfo=timezone.utc)
        self._last_normal_refresh = datetime.min.replace(tzinfo=timezone.utc)

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
        """Main monitoring loop."""
        while self._running:
            now = datetime.now(timezone.utc)

            try:
                # 1. Refresh mark prices periodically
                if (now - self._last_price_refresh).total_seconds() >= config.price_refresh_sec:
                    await self._refresh_prices()
                    self._last_price_refresh = now

                # 2. Process critical bucket (most frequently)
                if (now - self._last_critical_refresh).total_seconds() >= config.critical_refresh_sec:
                    await self._process_bucket(Bucket.CRITICAL)
                    self._last_critical_refresh = now

                # 3. Process high bucket
                if (now - self._last_high_refresh).total_seconds() >= config.high_refresh_sec:
                    await self._process_bucket(Bucket.HIGH)
                    self._last_high_refresh = now

                # 4. Process normal bucket
                if (now - self._last_normal_refresh).total_seconds() >= config.normal_refresh_sec:
                    await self._process_bucket(Bucket.NORMAL)
                    self._last_normal_refresh = now

                # 5. Run discovery periodically
                if (now - self._last_discovery) >= self._discovery_interval:
                    await self._run_discovery()
                    self._last_discovery = now

                # Small sleep to prevent busy-waiting
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                await asyncio.sleep(5)  # Back off on error

    async def _refresh_prices(self):
        """Refresh mark prices from API."""
        if self._fetcher:
            await self._fetcher.refresh_mark_prices()

    async def _process_bucket(self, bucket: Bucket):
        """
        Process all positions in a bucket.

        - Updates distances
        - Checks for alerts
        - Reclassifies into appropriate buckets
        """
        positions = self.position_db.get_positions_by_bucket(bucket)
        if not positions:
            return

        logger.debug(f"Processing {len(positions)} {bucket.value} positions")

        for cached in positions:
            try:
                await self._process_position(cached)
            except Exception as e:
                logger.warning(f"Error processing {cached.key}: {e}")

    async def _process_position(self, cached: CachedPosition):
        """
        Process a single position.

        - Update mark price
        - Recalculate distance
        - Check for alerts
        - Update bucket classification
        """
        position = cached.position

        # Get current mark price
        mark_price = self._fetcher.get_mark_price(position.token, position.exchange)
        if mark_price:
            position.mark_price = mark_price

        # Calculate distance
        distance = position.distance_to_liq()
        new_bucket = config.classify_bucket(distance)

        # Check for proximity alert
        if distance is not None:
            if distance <= config.proximity_alert_pct and not cached.alerted_proximity:
                await self._send_proximity_alert(position, distance)
                self.position_db.set_alerted_proximity(cached.key)

            if distance <= config.critical_alert_pct and not cached.alerted_critical:
                await self._send_critical_alert(position, distance)
                self.position_db.set_alerted_critical(cached.key)

        # Update position in database
        self.position_db.upsert_position(position, distance)

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
