#!/usr/bin/env python3
"""
Force Alert Script
==================

Manually triggers alerts for positions currently in CRITICAL and HIGH buckets.
Bypasses the normal state-transition logic to send alerts immediately.

Usage (in Coolify Terminal):
    python3 scripts/force_alerts.py              # Send real alerts
    python3 scripts/force_alerts.py --dry-run    # Preview only
    python3 scripts/force_alerts.py --critical   # Critical only
    python3 scripts/force_alerts.py --high       # High (approaching) only
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.position_db import PositionDB
from src.config import Bucket
from src.alerts.telegram import TelegramAlerts, AlertConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Force send liquidation alerts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview alerts without sending"
    )
    parser.add_argument(
        "--critical",
        action="store_true",
        help="Only send critical (imminent) alerts"
    )
    parser.add_argument(
        "--high",
        action="store_true",
        help="Only send high (approaching) alerts"
    )

    args = parser.parse_args()

    # Default to both if neither specified
    send_critical = args.critical or (not args.critical and not args.high)
    send_high = args.high or (not args.critical and not args.high)

    # Initialize database
    position_db = PositionDB()
    stats = position_db.get_stats()

    print(f"\n{'='*60}")
    print("FORCE ALERT - Current Position Status")
    print(f"{'='*60}")
    print(f"Total positions: {stats.total_positions}")
    print(f"  Critical (≤0.125%): {stats.critical_count}")
    print(f"  High (0.125-0.25%): {stats.high_count}")
    print(f"  Normal (>0.25%):    {stats.normal_count}")
    print(f"{'='*60}\n")

    if stats.critical_count == 0 and stats.high_count == 0:
        print("No positions in CRITICAL or HIGH buckets. Nothing to alert.")
        return

    # Set up alerts
    if args.dry_run:
        # Create dry-run alert sender
        config = AlertConfig(bot_token="dry-run", chat_id="dry-run", dry_run=True)
        telegram = TelegramAlerts(config)
        print("[DRY RUN MODE - No alerts will be sent]\n")
    else:
        telegram = TelegramAlerts.from_env()
        if telegram is None:
            print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
            print("Use --dry-run to preview without sending")
            sys.exit(1)

    alert_count = 0

    # Send critical alerts
    if send_critical:
        critical_positions = position_db.get_positions_by_bucket(Bucket.CRITICAL)
        for pos in critical_positions:
            p = pos.position
            is_isolated = p.leverage_type.lower() == "isolated"

            print(f"🚨 CRITICAL: {p.token} {p.side} - {pos.distance_pct:.3f}% to liq")
            print(f"   Value: ${p.position_value:,.0f} | Liq: ${p.liquidation_price:,.2f}")
            print(f"   Address: {p.address[:10]}...")

            telegram.send_critical_alert(
                token=p.token,
                side=p.side,
                address=p.address,
                distance_pct=pos.distance_pct,
                liq_price=p.liquidation_price,
                mark_price=p.mark_price,
                position_value=p.position_value,
                is_isolated=is_isolated,
                exchange=p.exchange or "main",
            )
            alert_count += 1

    # Send proximity alerts
    if send_high:
        high_positions = position_db.get_positions_by_bucket(Bucket.HIGH)
        for pos in high_positions:
            p = pos.position
            is_isolated = p.leverage_type.lower() == "isolated"

            print(f"⚠️  HIGH: {p.token} {p.side} - {pos.distance_pct:.3f}% to liq")
            print(f"   Value: ${p.position_value:,.0f} | Liq: ${p.liquidation_price:,.2f}")
            print(f"   Address: {p.address[:10]}...")

            telegram.send_proximity_alert(
                token=p.token,
                side=p.side,
                address=p.address,
                distance_pct=pos.distance_pct,
                liq_price=p.liquidation_price,
                mark_price=p.mark_price,
                position_value=p.position_value,
                is_isolated=is_isolated,
                exchange=p.exchange or "main",
            )
            alert_count += 1

    print(f"\n{'='*60}")
    print(f"Sent {alert_count} alert(s)")
    if args.dry_run:
        print("[DRY RUN - No alerts actually sent]")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
