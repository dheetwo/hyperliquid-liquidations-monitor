#!/usr/bin/env python3
"""
Health check script for Hyperdash Liquidation Monitor.

Returns exit code 0 if healthy, non-zero otherwise.
Used by Docker health checks to determine container health.

Checks:
1. Database connectivity
2. Positions updated recently (< 5 minutes)
3. Wallet scan progress (> 25% scanned)
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.wallet_db import WalletDB
from src.db.position_db import PositionDB
from src.config import config


def check_health() -> bool:
    """
    Perform comprehensive health checks.

    Returns:
        True if healthy, False otherwise
    """
    now = datetime.now(timezone.utc)

    # Check 1: Database connectivity
    try:
        wallet_db = WalletDB()
        position_db = PositionDB()
    except Exception as e:
        print(f"FAIL: Database connection error: {e}")
        return False

    # Check 2: Get basic stats
    try:
        wallet_stats = wallet_db.get_stats()
        position_stats = position_db.get_stats()
    except Exception as e:
        print(f"FAIL: Could not read database stats: {e}")
        return False

    # Check 3: Verify we have wallets
    if wallet_stats.total_wallets == 0:
        # This is OK during initial startup
        print("WARN: No wallets in database yet (may be initializing)")
        return True

    # Check 4: Check if positions were updated recently
    try:
        import sqlite3
        with sqlite3.connect(config.positions_db_path, timeout=10.0) as conn:
            result = conn.execute(
                "SELECT MAX(last_updated) FROM positions"
            ).fetchone()

            if result and result[0]:
                last_update = datetime.fromisoformat(result[0])
                time_since = (now - last_update).total_seconds()

                # Fail if no updates for more than 5 minutes
                if time_since > 300:
                    print(f"FAIL: Positions not updated for {time_since:.0f}s (> 300s)")
                    return False
            else:
                # No positions yet - check if we have wallets to scan
                if wallet_stats.total_wallets > 0:
                    # We have wallets but no positions - might be during discovery
                    print("WARN: No positions yet (discovery may be running)")
                    return True
    except Exception as e:
        print(f"FAIL: Could not check position updates: {e}")
        return False

    # Check 5: Verify wallet scan progress
    try:
        if wallet_stats.never_scanned > wallet_stats.total_wallets * 0.75:
            # More than 75% never scanned - might be stuck
            # But give grace period during startup
            print(
                f"WARN: {wallet_stats.never_scanned}/{wallet_stats.total_wallets} "
                "wallets never scanned (may be initial scan)"
            )
            # Don't fail on this - could be initial startup
            return True
    except Exception as e:
        print(f"FAIL: Could not check wallet stats: {e}")
        return False

    # All checks passed
    print(
        f"OK: {position_stats.total_positions} positions, "
        f"{wallet_stats.total_wallets} wallets "
        f"({position_stats.critical_count} critical, {position_stats.high_count} high)"
    )
    return True


def main():
    """Run health check and exit with appropriate code."""
    try:
        healthy = check_health()
        sys.exit(0 if healthy else 1)
    except Exception as e:
        print(f"FAIL: Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
