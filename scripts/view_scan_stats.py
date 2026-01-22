#!/usr/bin/env python3
"""
View scan snapshots and wallet registry statistics.

Usage:
    python scripts/view_scan_stats.py           # Show recent snapshots
    python scripts/view_scan_stats.py --all     # Show all snapshots
    python scripts/view_scan_stats.py --wallets # Show wallet registry stats
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.monitor.database import MonitorDatabase


def main():
    parser = argparse.ArgumentParser(description="View scan statistics")
    parser.add_argument('--all', action='store_true', help='Show all snapshots (not just recent)')
    parser.add_argument('--wallets', action='store_true', help='Show wallet registry stats')
    parser.add_argument('--limit', type=int, default=10, help='Number of snapshots to show')
    args = parser.parse_args()

    db = MonitorDatabase()

    if args.wallets:
        print("\n" + "=" * 60)
        print("WALLET REGISTRY STATISTICS")
        print("=" * 60)
        stats = db.get_wallet_registry_stats()
        print(f"Total wallets: {stats.get('total', 0)}")
        print(f"  By source:")
        for source, count in stats.get('by_source', {}).items():
            print(f"    {source}: {count}")
        print(f"  By scan frequency:")
        for freq, count in stats.get('by_frequency', {}).items():
            print(f"    {freq}: {count}")
        print(f"  Never scanned: {stats.get('never_scanned', 0)}")
        print(f"  Scanned at least once: {stats.get('scanned', 0)}")
        print()

    print("\n" + "=" * 60)
    print("RECENT SCAN SNAPSHOTS")
    print("=" * 60)

    limit = 100 if args.all else args.limit
    snapshots = db.get_recent_scan_snapshots(limit=limit)

    if not snapshots:
        print("No scan snapshots found.")
        return

    for snap in snapshots:
        print(f"\n[{snap['scan_time']}] {snap['scan_type'].upper()}")
        print(f"  Wallets scanned: {snap['total_wallets_scanned']}")
        print(f"    From Hyperdash: {snap.get('wallets_from_hyperdash', 0)}")
        print(f"    From Liq History: {snap.get('wallets_from_liq_history', 0)}")
        print(f"    Normal frequency: {snap.get('wallets_normal_frequency', 0)}")
        print(f"    Infrequent: {snap.get('wallets_infrequent', 0)}")
        print(f"  Positions found: {snap['positions_found']}")
        print(f"  Total position value: ${snap.get('total_position_value', 0):,.0f}")
        print(f"  Duration: {snap.get('scan_duration_seconds', 0):.1f}s")
        if snap.get('notes'):
            print(f"  Notes: {snap['notes']}")

    # Summary
    print("\n" + "-" * 60)
    summary = db.get_scan_snapshot_summary(hours=24)
    print(f"Last 24h: {summary['total_scans']} scans, "
          f"{summary['total_wallets_scanned']} total wallets, "
          f"{summary['total_positions_found']} total positions, "
          f"{summary['total_duration_seconds']:.0f}s total duration")


if __name__ == "__main__":
    main()
