#!/usr/bin/env python3
"""
Run the Hyperdash Liquidation Monitor.

Usage:
    python scripts/run_monitor.py                  # Normal mode
    python scripts/run_monitor.py --dry-run        # No alerts sent
    python scripts/run_monitor.py --clear-cache    # Clear position cache
    python scripts/run_monitor.py --log-level DEBUG
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.wallet_db import WalletDB
from src.db.position_db import PositionDB
from src.core.monitor import Monitor
from src.alerts import TelegramAlerts

logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Hyperdash Liquidation Monitor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log alerts instead of sending them"
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear position cache before starting"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize databases
    wallet_db = WalletDB()
    position_db = PositionDB()

    # Clear cache if requested
    if args.clear_cache:
        logger.info("Clearing position cache...")
        position_db.clear()

    # Set up alerts
    alert_callback = None
    telegram_alerts = None
    if not args.dry_run:
        telegram_alerts = TelegramAlerts.from_env()
        if telegram_alerts:
            alert_callback = telegram_alerts.as_callback()

    # Show initial stats
    wallet_stats = wallet_db.get_stats()
    position_stats = position_db.get_stats()

    logger.info(f"Wallet registry: {wallet_stats.total_wallets} wallets")
    logger.info(f"Position cache: {position_stats.total_positions} positions")

    # Create and run monitor
    monitor = Monitor(
        wallet_db=wallet_db,
        position_db=position_db,
        alert_callback=alert_callback,
        dry_run=args.dry_run,
    )

    logger.info("Starting monitor..." + (" (DRY RUN)" if args.dry_run else ""))

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
