#!/usr/bin/env python3
"""
View all data in the databases.

Usage:
    python3 scripts/view_data.py wallets          # Show wallet database
    python3 scripts/view_data.py wallets --full   # Show all wallet records
    python3 scripts/view_data.py positions        # Show position cache
    python3 scripts/view_data.py positions --full # Show all position records
    python3 scripts/view_data.py all              # Show everything
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config


def print_header(title: str):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)


def print_subheader(title: str):
    print(f"\n--- {title} ---")


def format_value(val):
    """Format a value for display."""
    if val is None:
        return "NULL"
    if isinstance(val, float):
        if abs(val) >= 1_000_000:
            return f"${val:,.0f}"
        elif abs(val) >= 1000:
            return f"${val:,.2f}"
        else:
            return f"{val:.4f}"
    return str(val)


def view_wallets(full: bool = False, limit: int = 50):
    """View wallet database contents."""
    print_header("WALLET DATABASE")

    db_path = config.wallets_db_path
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    print(f"Database: {db_path}")
    print(f"Size: {db_path.stat().st_size / 1024:.1f} KB")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Table info
    print_subheader("Table Schema")
    cursor = conn.execute("PRAGMA table_info(wallets)")
    columns = cursor.fetchall()
    print(f"{'Column':<20} {'Type':<15} {'Nullable':<10}")
    print("-" * 45)
    for col in columns:
        nullable = "NULL" if not col['notnull'] else "NOT NULL"
        print(f"{col['name']:<20} {col['type']:<15} {nullable:<10}")

    # Stats
    print_subheader("Summary Statistics")

    total = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    print(f"Total wallets: {total}")

    # By source
    cursor = conn.execute("""
        SELECT source, COUNT(*) as count
        FROM wallets
        GROUP BY source
        ORDER BY count DESC
    """)
    print("\nBy source:")
    for row in cursor:
        print(f"  {row['source']}: {row['count']}")

    # By cohort
    cursor = conn.execute("""
        SELECT cohort, COUNT(*) as count
        FROM wallets
        WHERE cohort IS NOT NULL
        GROUP BY cohort
        ORDER BY count DESC
    """)
    print("\nBy cohort:")
    for row in cursor:
        print(f"  {row['cohort']}: {row['count']}")

    # By scan frequency
    cursor = conn.execute("""
        SELECT scan_frequency, COUNT(*) as count
        FROM wallets
        GROUP BY scan_frequency
        ORDER BY count DESC
    """)
    print("\nBy scan frequency:")
    for row in cursor:
        print(f"  {row['scan_frequency']}: {row['count']}")

    # Position value distribution
    cursor = conn.execute("""
        SELECT
            CASE
                WHEN position_value IS NULL THEN 'Never scanned'
                WHEN position_value >= 10000000 THEN '$10M+'
                WHEN position_value >= 1000000 THEN '$1M-$10M'
                WHEN position_value >= 100000 THEN '$100K-$1M'
                WHEN position_value >= 60000 THEN '$60K-$100K'
                ELSE 'Below $60K'
            END as tier,
            COUNT(*) as count,
            SUM(position_value) as total_value
        FROM wallets
        GROUP BY tier
        ORDER BY
            CASE tier
                WHEN '$10M+' THEN 1
                WHEN '$1M-$10M' THEN 2
                WHEN '$100K-$1M' THEN 3
                WHEN '$60K-$100K' THEN 4
                WHEN 'Below $60K' THEN 5
                ELSE 6
            END
    """)
    print("\nBy position value tier:")
    for row in cursor:
        total = row['total_value'] or 0
        print(f"  {row['tier']:<15} {row['count']:>5} wallets  (${total:,.0f})")

    # Total position value
    cursor = conn.execute("SELECT SUM(position_value) FROM wallets WHERE position_value IS NOT NULL")
    total_value = cursor.fetchone()[0] or 0
    print(f"\nTotal tracked position value: ${total_value:,.0f}")

    # Sample or full records
    if full:
        print_subheader(f"All Wallet Records ({total})")
        query = """
            SELECT * FROM wallets
            ORDER BY position_value DESC NULLS LAST
        """
    else:
        print_subheader(f"Top {limit} Wallets by Position Value")
        query = f"""
            SELECT * FROM wallets
            ORDER BY position_value DESC NULLS LAST
            LIMIT {limit}
        """

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if rows:
        # Print header
        print(f"\n{'Address':<44} {'Source':<10} {'Cohort':<18} {'Position Value':>15} {'Freq':<10} {'Scanned'}")
        print("-" * 130)

        for row in rows:
            addr = row['address']
            source = row['source'] or ""
            cohort = row['cohort'] or ""
            pos_val = f"${row['position_value']:,.0f}" if row['position_value'] else "NULL"
            freq = row['scan_frequency'] or ""
            scanned = row['last_scanned'][:10] if row['last_scanned'] else "Never"

            print(f"{addr:<44} {source:<10} {cohort:<18} {pos_val:>15} {freq:<10} {scanned}")

    conn.close()


def view_positions(full: bool = False, limit: int = 50):
    """View position cache contents."""
    print_header("POSITION CACHE")

    db_path = config.positions_db_path
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    print(f"Database: {db_path}")
    print(f"Size: {db_path.stat().st_size / 1024:.1f} KB")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Table info
    print_subheader("Table Schema")
    cursor = conn.execute("PRAGMA table_info(positions)")
    columns = cursor.fetchall()
    print(f"{'Column':<25} {'Type':<15}")
    print("-" * 40)
    for col in columns:
        print(f"{col['name']:<25} {col['type']:<15}")

    # Stats
    print_subheader("Summary Statistics")

    total = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    print(f"Total positions: {total}")

    if total == 0:
        print("\nNo positions in cache. Run phase3 first.")
        conn.close()
        return

    # By bucket
    cursor = conn.execute("""
        SELECT bucket, COUNT(*) as count, SUM(position_value) as total_value
        FROM positions
        GROUP BY bucket
        ORDER BY
            CASE bucket
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'normal' THEN 3
            END
    """)
    print("\nBy bucket:")
    for row in cursor:
        print(f"  {row['bucket']:<10} {row['count']:>5} positions  ${row['total_value']:,.0f}")

    # By exchange
    cursor = conn.execute("""
        SELECT
            CASE WHEN exchange = '' THEN 'main' ELSE exchange END as ex,
            COUNT(*) as count,
            SUM(position_value) as total_value
        FROM positions
        GROUP BY ex
        ORDER BY total_value DESC
    """)
    print("\nBy exchange:")
    for row in cursor:
        print(f"  {row['ex']:<10} {row['count']:>5} positions  ${row['total_value']:,.0f}")

    # By side
    cursor = conn.execute("""
        SELECT side, COUNT(*) as count, SUM(position_value) as total_value
        FROM positions
        GROUP BY side
    """)
    print("\nBy side:")
    for row in cursor:
        print(f"  {row['side']:<10} {row['count']:>5} positions  ${row['total_value']:,.0f}")

    # By token (top 20)
    cursor = conn.execute("""
        SELECT token, COUNT(*) as count, SUM(position_value) as total_value
        FROM positions
        GROUP BY token
        ORDER BY total_value DESC
        LIMIT 20
    """)
    print("\nTop 20 tokens by notional:")
    for row in cursor:
        print(f"  {row['token']:<15} {row['count']:>5} positions  ${row['total_value']:,.0f}")

    # Distance distribution
    cursor = conn.execute("""
        SELECT
            CASE
                WHEN distance_pct IS NULL THEN 'No liq price'
                WHEN distance_pct <= 0.125 THEN '≤0.125% (critical)'
                WHEN distance_pct <= 0.25 THEN '0.125-0.25% (high)'
                WHEN distance_pct <= 1 THEN '0.25-1%'
                WHEN distance_pct <= 5 THEN '1-5%'
                WHEN distance_pct <= 10 THEN '5-10%'
                ELSE '>10%'
            END as tier,
            COUNT(*) as count,
            SUM(position_value) as total_value
        FROM positions
        GROUP BY tier
        ORDER BY
            CASE tier
                WHEN '≤0.125% (critical)' THEN 1
                WHEN '0.125-0.25% (high)' THEN 2
                WHEN '0.25-1%' THEN 3
                WHEN '1-5%' THEN 4
                WHEN '5-10%' THEN 5
                WHEN '>10%' THEN 6
                ELSE 7
            END
    """)
    print("\nBy distance to liquidation:")
    for row in cursor:
        print(f"  {row['tier']:<20} {row['count']:>5} positions  ${row['total_value']:,.0f}")

    # Alert status
    cursor = conn.execute("""
        SELECT
            SUM(alerted_proximity) as proximity_alerts,
            SUM(alerted_critical) as critical_alerts
        FROM positions
    """)
    row = cursor.fetchone()
    print(f"\nAlert status:")
    print(f"  Proximity alerts sent: {row['proximity_alerts'] or 0}")
    print(f"  Critical alerts sent: {row['critical_alerts'] or 0}")

    # Total notional
    cursor = conn.execute("SELECT SUM(position_value) FROM positions")
    total_value = cursor.fetchone()[0] or 0
    print(f"\nTotal notional: ${total_value:,.0f}")

    # Sample or full records
    if full:
        print_subheader(f"All Position Records ({total})")
        query = "SELECT * FROM positions ORDER BY distance_pct ASC NULLS LAST"
    else:
        print_subheader(f"Top {limit} Positions (Closest to Liquidation)")
        query = f"SELECT * FROM positions ORDER BY distance_pct ASC NULLS LAST LIMIT {limit}"

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if rows:
        print(f"\n{'Token':<12} {'Side':<6} {'Value':>14} {'Dist%':>8} {'Liq Price':>14} {'Mark Price':>14} {'Lev':>6} {'Bucket':<10} {'Exchange':<8}")
        print("-" * 110)

        for row in rows:
            token = row['token'][:12]
            side = row['side']
            value = f"${row['position_value']:,.0f}"
            dist = f"{row['distance_pct']:.2f}%" if row['distance_pct'] else "N/A"
            liq = f"${row['liquidation_price']:,.2f}" if row['liquidation_price'] else "N/A"
            mark = f"${row['mark_price']:,.2f}" if row['mark_price'] else "N/A"
            lev = f"{row['leverage']:.1f}x"
            bucket = row['bucket']
            exchange = row['exchange'] if row['exchange'] else "main"

            print(f"{token:<12} {side:<6} {value:>14} {dist:>8} {liq:>14} {mark:>14} {lev:>6} {bucket:<10} {exchange:<8}")

    conn.close()


def view_all(full: bool = False):
    """View all databases."""
    view_wallets(full=full)
    view_positions(full=full)


def main():
    parser = argparse.ArgumentParser(description="View database contents")
    parser.add_argument(
        "view",
        choices=["wallets", "positions", "all"],
        help="What to view"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show all records (not just top N)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of records to show (default: 50)"
    )

    args = parser.parse_args()

    if args.view == "wallets":
        view_wallets(full=args.full, limit=args.limit)
    elif args.view == "positions":
        view_positions(full=args.full, limit=args.limit)
    elif args.view == "all":
        view_all(full=args.full)


if __name__ == "__main__":
    main()
