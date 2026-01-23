#!/usr/bin/env python3
"""
View raw data from each source BEFORE combining into wallet database.

Source 1: Hyperdash cohorts
Source 2: Telegram liquidation history

Usage:
    python3 scripts/view_sources.py hyperdash        # Show Hyperdash cohort data
    python3 scripts/view_sources.py hyperdash --cohort kraken  # Specific cohort
    python3 scripts/view_sources.py liq_history      # Show liquidation history
    python3 scripts/view_sources.py all              # Show both sources
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config
from src.api.hyperdash import HyperdashClient


def print_header(title: str):
    print("\n" + "=" * 100)
    print(f"  {title}")
    print("=" * 100)


def print_subheader(title: str):
    print(f"\n--- {title} ---\n")


async def view_hyperdash_source(cohort_filter: str = None, limit: int = None):
    """
    View raw wallet data from Hyperdash API.

    This is SOURCE 1: Hyperdash cohort data with all attributes.
    """
    print_header("SOURCE 1: HYPERDASH COHORTS")

    print("API Endpoint: https://api.hyperdash.com/graphql")
    print("Data includes: address, accountValue, perpPnl, totalNotional, longNotional, shortNotional")

    cohorts_to_fetch = [cohort_filter] if cohort_filter else config.cohorts

    async with HyperdashClient() as client:
        for cohort in cohorts_to_fetch:
            print_subheader(f"Cohort: {cohort.upper()}")

            wallets = await client.get_cohort_addresses(cohort)

            print(f"Total wallets in {cohort}: {len(wallets)}")

            if not wallets:
                continue

            # Sort by account value descending
            wallets_sorted = sorted(wallets, key=lambda w: w.account_value, reverse=True)

            # Apply limit
            if limit:
                wallets_sorted = wallets_sorted[:limit]

            # Print header
            print(f"\n{'#':<4} {'Address':<44} {'Account Value':>15} {'Total Notional':>15} {'Long':>12} {'Short':>12} {'Leverage':>8} {'Bias':<15} {'PnL':>12}")
            print("-" * 150)

            for i, w in enumerate(wallets_sorted, 1):
                addr = w.address
                acct_val = f"${w.account_value:,.0f}"
                total_not = f"${w.total_notional:,.0f}"
                long_not = f"${w.long_notional:,.0f}"
                short_not = f"${w.short_notional:,.0f}"
                leverage = f"{w.leverage:.1f}x"
                bias = w.bias
                pnl = f"${w.perp_pnl:,.0f}" if w.perp_pnl >= 0 else f"-${abs(w.perp_pnl):,.0f}"

                print(f"{i:<4} {addr:<44} {acct_val:>15} {total_not:>15} {long_not:>12} {short_not:>12} {leverage:>8} {bias:<15} {pnl:>12}")

            if limit and len(wallets) > limit:
                print(f"\n... and {len(wallets) - limit} more wallets in this cohort")

            # Summary stats for this cohort
            total_account_value = sum(w.account_value for w in wallets)
            total_notional = sum(w.total_notional for w in wallets)
            avg_leverage = total_notional / total_account_value if total_account_value > 0 else 0

            print(f"\nCohort Summary:")
            print(f"  Total Account Value: ${total_account_value:,.0f}")
            print(f"  Total Notional: ${total_notional:,.0f}")
            print(f"  Average Leverage: {avg_leverage:.1f}x")


def view_liq_history_source(limit: int = None):
    """
    View raw wallet data from Telegram liquidation history.

    This is SOURCE 2: Addresses extracted from liquidation events.
    """
    print_header("SOURCE 2: TELEGRAM LIQUIDATION HISTORY")

    # Check if we have the archived liquidation history database
    liq_db_path = Path(__file__).parent.parent / "data" / "liquidation_history.db"
    archived_liq_db = Path(__file__).parent.parent / "archive" / "v2" / "src" / "monitor" / "liquidation_feed.py"

    print("Source: @liquidations_hyperliquid Telegram channel")
    print("Format: Parsed from messages like '🔴 #BTC Long Liquidation: $1.15M @ $88,827.1'")
    print("Data includes: address, token, side, notional, price, timestamp")

    if not liq_db_path.exists():
        print(f"\nNo liquidation history database found at: {liq_db_path}")
        print("\nTo import liquidation history:")
        print("  1. Export the Telegram channel as JSON using Telegram Desktop")
        print("  2. Run: python3 scripts/import_liq_history.py <export.json>")
        print("  3. Or add individual addresses: python3 scripts/import_liq_history.py --add <address> --notional <value>")

        # Check wallet DB for any liq_history entries
        wallet_db_path = config.wallets_db_path
        if wallet_db_path.exists():
            import sqlite3
            conn = sqlite3.connect(wallet_db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM wallets
                WHERE source = 'liq_history'
                ORDER BY position_value DESC NULLS LAST
            """)
            rows = cursor.fetchall()
            conn.close()

            if rows:
                print(f"\n--- Addresses Already Imported from Liq History ({len(rows)}) ---\n")
                print(f"{'#':<4} {'Address':<44} {'Position Value':>15} {'Cohort':<15} {'Last Scanned':<12}")
                print("-" * 95)

                for i, row in enumerate(rows[:limit] if limit else rows, 1):
                    addr = row['address']
                    pos_val = f"${row['position_value']:,.0f}" if row['position_value'] else "NULL"
                    cohort = row['cohort'] or "N/A"
                    scanned = row['last_scanned'][:10] if row['last_scanned'] else "Never"
                    print(f"{i:<4} {addr:<44} {pos_val:>15} {cohort:<15} {scanned:<12}")
        return

    # If we have the liquidation history database, show it
    import sqlite3
    conn = sqlite3.connect(liq_db_path)
    conn.row_factory = sqlite3.Row

    # Show liquidation events
    print_subheader("Liquidation Events")

    cursor = conn.execute("SELECT COUNT(*) FROM liquidation_history")
    total_events = cursor.fetchone()[0]
    print(f"Total liquidation events: {total_events}")

    cursor = conn.execute("""
        SELECT * FROM liquidation_history
        ORDER BY notional DESC
        LIMIT ?
    """, (limit or 50,))
    events = cursor.fetchall()

    if events:
        print(f"\n{'#':<4} {'Address':<44} {'Token':<12} {'Side':<6} {'Notional':>14} {'Price':>14} {'Exchange':<8} {'Timestamp'}")
        print("-" * 140)

        for i, e in enumerate(events, 1):
            addr = e['address']
            token = e['token'][:12]
            side = e['side']
            notional = f"${e['notional']:,.0f}"
            price = f"${e['price']:,.2f}"
            exchange = e['exchange']
            ts = e['timestamp'][:19] if e['timestamp'] else "N/A"
            print(f"{i:<4} {addr:<44} {token:<12} {side:<6} {notional:>14} {price:>14} {exchange:<8} {ts}")

    # Show aggregated addresses
    print_subheader("Aggregated Addresses (Unique Wallets)")

    cursor = conn.execute("SELECT COUNT(*) FROM liquidated_addresses")
    total_addresses = cursor.fetchone()[0]
    print(f"Total unique liquidated addresses: {total_addresses}")

    cursor = conn.execute("""
        SELECT * FROM liquidated_addresses
        ORDER BY max_notional DESC
        LIMIT ?
    """, (limit or 50,))
    addresses = cursor.fetchall()

    if addresses:
        print(f"\n{'#':<4} {'Address':<44} {'Max Notional':>14} {'Total Liqs':>10} {'First Liq':<12} {'Last Liq':<12} {'Tokens'}")
        print("-" * 130)

        for i, a in enumerate(addresses, 1):
            addr = a['address']
            max_not = f"${a['max_notional']:,.0f}"
            total_liqs = a['total_liquidations']
            first = a['first_liquidation'][:10] if a['first_liquidation'] else "N/A"
            last = a['last_liquidation'][:10] if a['last_liquidation'] else "N/A"
            tokens = a['tokens_liquidated'] or "[]"
            print(f"{i:<4} {addr:<44} {max_not:>14} {total_liqs:>10} {first:<12} {last:<12} {tokens}")

    conn.close()


async def view_all_sources(limit: int = None):
    """View both sources."""
    await view_hyperdash_source(limit=limit)
    print("\n\n")
    view_liq_history_source(limit=limit)


async def main():
    parser = argparse.ArgumentParser(description="View raw source data")
    parser.add_argument(
        "source",
        choices=["hyperdash", "liq_history", "all"],
        help="Which source to view"
    )
    parser.add_argument(
        "--cohort",
        help="Specific cohort to view (for hyperdash)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Number of records per cohort/source (default: 30)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show all records (no limit)"
    )

    args = parser.parse_args()

    limit = None if args.full else args.limit

    if args.source == "hyperdash":
        await view_hyperdash_source(cohort_filter=args.cohort, limit=limit)
    elif args.source == "liq_history":
        view_liq_history_source(limit=limit)
    elif args.source == "all":
        await view_all_sources(limit=limit)


if __name__ == "__main__":
    asyncio.run(main())
