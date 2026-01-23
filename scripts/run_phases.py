#!/usr/bin/env python3
"""
Run each phase of the monitor individually to see outputs.

Usage:
    python3 scripts/run_phases.py phase1  # Build wallet database
    python3 scripts/run_phases.py phase2  # Filter wallets
    python3 scripts/run_phases.py phase3  # Fetch positions
    python3 scripts/run_phases.py phase4  # Bucket positions
    python3 scripts/run_phases.py all     # Run all phases
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config, Bucket
from src.api.hyperliquid import HyperliquidClient
from src.api.hyperdash import HyperdashClient
from src.db.wallet_db import WalletDB
from src.db.position_db import PositionDB
from src.core.wallet_filter import filter_wallets_for_scan
from src.core.position_fetcher import PositionFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def print_header(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


async def phase1_build_wallet_db():
    """Phase 1: Build wallet database from Hyperdash cohorts."""
    print_header("PHASE 1: BUILD WALLET DATABASE")

    wallet_db = WalletDB()

    print("Fetching cohorts from Hyperdash...")
    print(f"Cohorts to fetch: {config.cohorts}\n")

    async with HyperdashClient() as client:
        all_wallets = []

        for cohort in config.cohorts:
            print(f"  Fetching {cohort}...", end=" ", flush=True)
            wallets = await client.get_cohort_addresses(cohort)
            print(f"{len(wallets)} wallets")

            for w in wallets:
                all_wallets.append({
                    "address": w.address,
                    "source": "hyperdash",
                    "cohort": cohort,
                })

        print(f"\nTotal wallets fetched: {len(all_wallets)}")

        # Deduplicate by address (keep first occurrence)
        seen = set()
        unique_wallets = []
        for w in all_wallets:
            if w["address"] not in seen:
                seen.add(w["address"])
                unique_wallets.append(w)

        print(f"Unique wallets: {len(unique_wallets)}")

        # Add to database
        new_count, updated_count = wallet_db.add_wallets_batch(unique_wallets)
        print(f"\nAdded to database: {new_count} new, {updated_count} updated")

    # Show stats
    stats = wallet_db.get_stats()
    print(f"\n--- Wallet Registry Stats ---")
    print(f"  Total wallets: {stats.total_wallets}")
    print(f"  From Hyperdash: {stats.from_hyperdash}")
    print(f"  From liq history: {stats.from_liq_history}")
    print(f"  Normal frequency: {stats.normal_frequency}")
    print(f"  Infrequent: {stats.infrequent}")
    print(f"  Never scanned: {stats.never_scanned}")

    return wallet_db


async def phase2_filter_wallets(wallet_db: WalletDB = None):
    """Phase 2: Filter wallets for scanning."""
    print_header("PHASE 2: FILTER WALLETS")

    if wallet_db is None:
        wallet_db = WalletDB()

    # Get all wallets
    all_wallets = wallet_db.get_wallets_for_scan(include_infrequent=True)
    print(f"Total wallets in registry: {len(all_wallets)}")

    # Filter for scanning
    wallets_to_scan = filter_wallets_for_scan(all_wallets, include_infrequent=False)
    print(f"Wallets to scan (normal frequency): {len(wallets_to_scan)}")

    # Show breakdown by cohort
    cohort_counts = {}
    for w in wallets_to_scan:
        cohort = w.cohort or "unknown"
        cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1

    print(f"\n--- Breakdown by Cohort ---")
    for cohort, count in sorted(cohort_counts.items(), key=lambda x: -x[1]):
        print(f"  {cohort}: {count}")

    # Show breakdown by source
    source_counts = {}
    for w in wallets_to_scan:
        source_counts[w.source] = source_counts.get(w.source, 0) + 1

    print(f"\n--- Breakdown by Source ---")
    for source, count in source_counts.items():
        print(f"  {source}: {count}")

    return wallets_to_scan


async def phase3_fetch_positions(wallets_to_scan: list = None, limit: int = None):
    """Phase 3: Fetch positions from wallets."""
    print_header("PHASE 3: FETCH POSITIONS")

    if wallets_to_scan is None:
        wallet_db = WalletDB()
        wallets_to_scan = wallet_db.get_wallets_for_scan()

    addresses = [w.address for w in wallets_to_scan]

    if limit:
        print(f"Limiting to first {limit} addresses for testing")
        addresses = addresses[:limit]

    print(f"Fetching positions for {len(addresses)} addresses...")
    print(f"Exchanges: {config.exchanges}")
    print(f"Concurrency: {config.max_concurrent_requests}")
    print(f"Request delay: {config.request_delay_sec}s\n")

    async with HyperliquidClient() as client:
        fetcher = PositionFetcher(client)

        # Refresh mark prices first
        print("Fetching mark prices...")
        await fetcher.refresh_mark_prices()

        main_prices = fetcher._mark_prices.get("", {})
        xyz_prices = fetcher._mark_prices.get("xyz", {})
        print(f"  Main exchange: {len(main_prices)} prices")
        print(f"  XYZ exchange: {len(xyz_prices)} prices")

        # Fetch positions
        print(f"\nFetching positions...")

        positions_fetched = 0
        def progress(done, total):
            nonlocal positions_fetched
            if done % 100 == 0 or done == total:
                print(f"  Progress: {done}/{total} addresses")

        all_positions = await fetcher.fetch_positions_batch(
            addresses,
            filter_by_threshold=False,  # Get all first
            progress_callback=progress,
        )

        print(f"\n--- Position Fetch Results ---")
        print(f"  Total positions found: {len(all_positions)}")

        # Breakdown by exchange
        by_exchange = {}
        for p in all_positions:
            ex = p.exchange or "main"
            by_exchange[ex] = by_exchange.get(ex, 0) + 1

        print(f"\n  By exchange:")
        for ex, count in sorted(by_exchange.items()):
            print(f"    {ex}: {count}")

        # Breakdown by side
        longs = sum(1 for p in all_positions if p.side == "long")
        shorts = sum(1 for p in all_positions if p.side == "short")
        print(f"\n  By side:")
        print(f"    Long: {longs}")
        print(f"    Short: {shorts}")

        # With liquidation price
        with_liq = [p for p in all_positions if p.has_liq_price]
        print(f"\n  With liquidation price: {len(with_liq)}")

        # Apply threshold filter
        filtered = fetcher.filter_by_threshold(all_positions)
        print(f"  After notional threshold: {len(filtered)}")

        # Final filtered with liq price
        final = [p for p in filtered if p.has_liq_price]
        print(f"  Final (threshold + liq price): {len(final)}")

        # Total notional
        total_notional = sum(p.position_value for p in final)
        print(f"\n  Total notional: ${total_notional:,.0f}")

        return final, fetcher


async def phase4_bucket_positions(positions: list = None, fetcher: PositionFetcher = None):
    """Phase 4: Bucket positions by liquidation proximity."""
    print_header("PHASE 4: BUCKET POSITIONS")

    if positions is None:
        print("No positions provided. Run phase3 first.")
        return

    position_db = PositionDB()

    print(f"Processing {len(positions)} positions...")

    # Calculate distances and bucket
    buckets = {Bucket.CRITICAL: [], Bucket.HIGH: [], Bucket.NORMAL: []}

    for p in positions:
        # Get current mark price if available
        if fetcher:
            mark_price = fetcher.get_mark_price(p.token, p.exchange)
            if mark_price:
                p.mark_price = mark_price

        distance = p.distance_to_liq()
        bucket = config.classify_bucket(distance)
        buckets[bucket].append((p, distance))

        # Save to database
        position_db.upsert_position(p, distance)

    print(f"\n--- Bucket Distribution ---")
    print(f"  Critical (≤{config.critical_distance_pct}%): {len(buckets[Bucket.CRITICAL])}")
    print(f"  High ({config.critical_distance_pct}-{config.high_distance_pct}%): {len(buckets[Bucket.HIGH])}")
    print(f"  Normal (>{config.high_distance_pct}%): {len(buckets[Bucket.NORMAL])}")

    # Show critical positions
    if buckets[Bucket.CRITICAL]:
        print(f"\n--- Critical Positions ---")
        for p, dist in sorted(buckets[Bucket.CRITICAL], key=lambda x: x[1] or 999):
            print(f"  {p.token} {p.side} ${p.position_value:,.0f} @ {dist:.3f}% "
                  f"(liq: ${p.liquidation_price:,.2f})")

    # Show high positions
    if buckets[Bucket.HIGH]:
        print(f"\n--- High Priority Positions ---")
        for p, dist in sorted(buckets[Bucket.HIGH], key=lambda x: x[1] or 999)[:20]:
            print(f"  {p.token} {p.side} ${p.position_value:,.0f} @ {dist:.3f}%")
        if len(buckets[Bucket.HIGH]) > 20:
            print(f"  ... and {len(buckets[Bucket.HIGH]) - 20} more")

    # Show top normal positions by proximity
    if buckets[Bucket.NORMAL]:
        print(f"\n--- Top Normal Positions (closest to threshold) ---")
        sorted_normal = sorted(buckets[Bucket.NORMAL], key=lambda x: x[1] or 999)
        for p, dist in sorted_normal[:10]:
            print(f"  {p.token} {p.side} ${p.position_value:,.0f} @ {dist:.2f}%")

    # Show database stats
    stats = position_db.get_stats()
    print(f"\n--- Position Cache Stats ---")
    print(f"  Total cached: {stats.total_positions}")
    print(f"  Total notional: ${stats.total_notional:,.0f}")

    return position_db


async def run_all_phases(limit: int = None):
    """Run all phases in sequence."""
    print_header("RUNNING ALL PHASES")

    # Phase 1
    wallet_db = await phase1_build_wallet_db()

    input("\nPress Enter to continue to Phase 2...")

    # Phase 2
    wallets_to_scan = await phase2_filter_wallets(wallet_db)

    input("\nPress Enter to continue to Phase 3...")

    # Phase 3
    positions, fetcher = await phase3_fetch_positions(wallets_to_scan, limit=limit)

    input("\nPress Enter to continue to Phase 4...")

    # Phase 4
    position_db = await phase4_bucket_positions(positions, fetcher)

    print_header("ALL PHASES COMPLETE")


async def main():
    parser = argparse.ArgumentParser(description="Run monitor phases individually")
    parser.add_argument(
        "phase",
        choices=["phase1", "phase2", "phase3", "phase4", "all"],
        help="Which phase to run"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of addresses to scan (for testing)"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear databases before running"
    )

    args = parser.parse_args()

    if args.clear:
        print("Clearing databases...")
        WalletDB().add_wallet("dummy", "test")  # Initialize
        PositionDB().clear()
        # Remove wallet db to start fresh
        import os
        wallet_path = config.wallets_db_path
        if wallet_path.exists():
            os.remove(wallet_path)
        print("Databases cleared.\n")

    if args.phase == "phase1":
        await phase1_build_wallet_db()
    elif args.phase == "phase2":
        await phase2_filter_wallets()
    elif args.phase == "phase3":
        await phase3_fetch_positions(limit=args.limit)
    elif args.phase == "phase4":
        # Need to run phase3 first to get positions
        _, fetcher = await phase3_fetch_positions(limit=args.limit)
        # Get positions from DB
        position_db = PositionDB()
        cached = position_db.get_all_positions()
        positions = [c.position for c in cached]
        await phase4_bucket_positions(positions, fetcher)
    elif args.phase == "all":
        await run_all_phases(limit=args.limit)


if __name__ == "__main__":
    asyncio.run(main())
