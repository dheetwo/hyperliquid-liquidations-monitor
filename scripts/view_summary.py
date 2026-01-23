#!/usr/bin/env python3
"""
View summary tables for wallet database and data sources.

Usage:
    python scripts/view_summary.py              # Show all summaries
    python scripts/view_summary.py sources      # Source data summaries
    python scripts/view_summary.py wallets      # Wallet database summary
    python scripts/view_summary.py thresholds   # Token threshold reference
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hyperdash import HyperdashClient
from src.config import config


def print_table(headers: list, rows: list, alignments: list = None):
    """Print a formatted table."""
    if not rows:
        print("(no data)")
        return

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    # Default alignments (right for numbers, left for text)
    if alignments is None:
        alignments = []
        for i, h in enumerate(headers):
            # Check first data row for type
            if rows and i < len(rows[0]):
                val = str(rows[0][i])
                alignments.append('>' if val.replace(',', '').replace('$', '').replace('%', '').replace('.', '').replace('-', '').isdigit() else '<')
            else:
                alignments.append('<')

    # Print header
    header_fmt = " | ".join(f"{{{i}:{alignments[i]}{widths[i]}}}" for i in range(len(headers)))
    print(header_fmt.format(*headers))
    print("-" * (sum(widths) + 3 * (len(headers) - 1)))

    # Print rows
    for row in rows:
        print(header_fmt.format(*[str(c) for c in row]))


async def show_hyperdash_summary():
    """Show Hyperdash cohort summary."""
    print("\n" + "=" * 80)
    print("  SOURCE 1: HYPERDASH COHORTS")
    print("=" * 80)

    client = HyperdashClient()
    threshold = config.min_wallet_value

    rows = []
    totals = {"wallets": 0, "account": 0, "notional": 0, "normal": 0, "infreq": 0}

    for cohort in config.cohorts:
        wallets = await client.get_cohort_addresses(cohort)

        account_value = sum(w.account_value for w in wallets)
        total_notional = sum(w.total_notional for w in wallets)
        normal = sum(1 for w in wallets if w.total_notional >= threshold)
        infreq = len(wallets) - normal
        avg_lev = total_notional / account_value if account_value > 0 else 0

        rows.append([
            cohort,
            f"{len(wallets):,}",
            f"${account_value/1e6:,.0f}M",
            f"${total_notional/1e6:,.0f}M",
            f"{avg_lev:.1f}x",
            f"{normal:,}",
            f"{infreq:,}",
        ])

        totals["wallets"] += len(wallets)
        totals["account"] += account_value
        totals["notional"] += total_notional
        totals["normal"] += normal
        totals["infreq"] += infreq

    await client.close()

    # Add totals row
    avg_lev = totals["notional"] / totals["account"] if totals["account"] > 0 else 0
    rows.append([
        "TOTAL",
        f"{totals['wallets']:,}",
        f"${totals['account']/1e6:,.0f}M",
        f"${totals['notional']/1e6:,.0f}M",
        f"{avg_lev:.1f}x",
        f"{totals['normal']:,}",
        f"{totals['infreq']:,}",
    ])

    print(f"\nThreshold for normal/infrequent: ${threshold:,.0f}\n")
    print_table(
        ["Cohort", "Wallets", "Account Value", "Total Notional", "Avg Lev", "Normal", "Infreq"],
        rows
    )


def show_liq_history_summary(json_path: str = None):
    """Show liquidation history summary by token."""
    print("\n" + "=" * 80)
    print("  SOURCE 2: TELEGRAM LIQUIDATION HISTORY")
    print("=" * 80)

    # Try to find JSON file
    if json_path is None:
        json_path = Path(".context/attachments/result.json")
        if not json_path.exists():
            print("\nNo liquidation history JSON found.")
            print("Provide path: python scripts/view_summary.py sources --liq-json <path>")
            return
    else:
        json_path = Path(json_path)

    with open(json_path) as f:
        data = json.load(f)

    MESSAGE_PATTERN = re.compile(
        r'([🔴🟢])\s*'
        r'#(\[?\w+\]?:?\w+)\s+'
        r'(Long|Short)\s+Liquidation:\s*'
        r'\$([0-9,.]+)([KMB]?)\s*'
        r'@\s*\$?([0-9,.]+)',
        re.IGNORECASE
    )
    MULTIPLIERS = {'': 1, 'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}

    token_stats = {}

    for msg in data['messages']:
        if msg.get('type') != 'message':
            continue
        text_parts = msg.get('text', '')
        if not isinstance(text_parts, list):
            continue
        text = ''.join(p if isinstance(p, str) else p.get('text', '') for p in text_parts)

        match = MESSAGE_PATTERN.search(text)
        if not match:
            continue

        _, token_raw, side, notional_str, multiplier, _ = match.groups()
        token_raw = token_raw.lstrip('#')

        if token_raw.startswith('['):
            m = re.match(r'\[(\w+)\]:(\w+)', token_raw)
            token = f"{m.group(1)}:{m.group(2)}" if m else token_raw
        elif ':' in token_raw:
            token = token_raw
        else:
            token = token_raw.upper()

        notional = float(notional_str.replace(',', ''))
        notional *= MULTIPLIERS.get(multiplier.upper() if multiplier else '', 1)

        if notional < 50_000:
            continue

        if token.startswith('xyz:') or token.startswith('[xyz]'):
            exchange = 'xyz'
            token_name = token.replace('xyz:', '').replace('[xyz]:', '')
        else:
            exchange = ''
            token_name = token

        threshold = config.get_notional_threshold(token_name, exchange, is_isolated=True)
        qualifies = notional >= threshold

        if token not in token_stats:
            token_stats[token] = {'count': 0, 'qualifies': 0, 'threshold': threshold}

        token_stats[token]['count'] += 1
        if qualifies:
            token_stats[token]['qualifies'] += 1

    sorted_tokens = sorted(token_stats.items(), key=lambda x: -x[1]['count'])

    rows = []
    for token, stats in sorted_tokens[:20]:
        pct = stats['qualifies'] / stats['count'] * 100 if stats['count'] > 0 else 0
        rows.append([
            token,
            f"{stats['count']:,}",
            f"{stats['qualifies']:,}",
            f"{pct:.0f}%",
            f"${stats['threshold']:,.0f}",
        ])

    total_liqs = sum(s['count'] for s in token_stats.values())
    total_qual = sum(s['qualifies'] for s in token_stats.values())

    print(f"\nTotal liquidations: {total_liqs:,}")
    print(f"Meeting threshold: {total_qual:,} ({total_qual/total_liqs*100:.0f}%)\n")

    print_table(
        ["Token", "Liqs", "Qualify", "%", "Threshold"],
        rows
    )


def show_wallet_summary():
    """Show wallet database summary."""
    print("\n" + "=" * 80)
    print("  WALLET DATABASE SUMMARY")
    print("=" * 80)

    db_path = Path("data/wallets.db")
    if not db_path.exists():
        print("\nNo wallet database found.")
        return

    conn = sqlite3.connect(db_path)

    # Basic stats
    total = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    hyperdash = conn.execute("SELECT COUNT(*) FROM wallets WHERE source = 'hyperdash'").fetchone()[0]
    liq_hist = conn.execute("SELECT COUNT(*) FROM wallets WHERE source = 'liq_history'").fetchone()[0]
    normal = conn.execute("SELECT COUNT(*) FROM wallets WHERE scan_frequency = 'normal'").fetchone()[0]
    infreq = conn.execute("SELECT COUNT(*) FROM wallets WHERE scan_frequency = 'infrequent'").fetchone()[0]

    print(f"\nTotal wallets: {total:,}")
    print(f"  From Hyperdash: {hyperdash:,}")
    print(f"  From Liq History: {liq_hist:,}")
    print(f"\nBy scan frequency:")
    print(f"  Normal: {normal:,} ({normal/total*100:.0f}%)")
    print(f"  Infrequent: {infreq:,} ({infreq/total*100:.0f}%)")

    # By position value tier
    print("\nBy position value tier:")
    tiers = [
        ("$10M+", 10_000_000, float('inf')),
        ("$1M-$10M", 1_000_000, 10_000_000),
        ("$100K-$1M", 100_000, 1_000_000),
        ("$60K-$100K", 60_000, 100_000),
        ("Below $60K", 0, 60_000),
    ]

    rows = []
    for name, low, high in tiers:
        result = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(position_value), 0) FROM wallets WHERE position_value >= ? AND position_value < ?",
            (low, high)
        ).fetchone()
        rows.append([name, f"{result[0]:,}", f"${result[1]:,.0f}"])

    print_table(["Tier", "Wallets", "Total Value"], rows)

    # By cohort
    print("\nBy cohort (Hyperdash wallets):")
    cohort_rows = conn.execute("""
        SELECT cohort, COUNT(*), SUM(CASE WHEN scan_frequency = 'normal' THEN 1 ELSE 0 END)
        FROM wallets
        WHERE source = 'hyperdash' AND cohort IS NOT NULL
        GROUP BY cohort
        ORDER BY COUNT(*) DESC
    """).fetchall()

    rows = []
    for cohort, count, normal_count in cohort_rows:
        rows.append([cohort, f"{count:,}", f"{normal_count:,}", f"{count - normal_count:,}"])

    print_table(["Cohort", "Wallets", "Normal", "Infreq"], rows)

    conn.close()


def show_thresholds():
    """Show token threshold reference."""
    print("\n" + "=" * 80)
    print("  TOKEN THRESHOLD REFERENCE")
    print("=" * 80)

    print("\nMain Exchange (Cross / Isolated):")
    rows = [
        ["Mega Cap", "BTC, ETH", "$30M", "$6M"],
        ["Large Cap", "SOL", "$20M", "$4M"],
        ["Tier 1 Alts", "DOGE, XRP, HYPE", "$10M", "$2M"],
        ["Tier 2 Alts", "BNB", "$2M", "$400K"],
        ["Mid Alts", "LINK, SUI, UNI...", "$1M", "$200K"],
        ["Low Alts", "PEPE, WIF, ARB...", "$500K", "$100K"],
        ["Small Caps", "Everything else", "$300K", "$60K"],
    ]
    print_table(["Tier", "Tokens", "Cross", "Isolated"], rows)

    print("\nXYZ Exchange (All Isolated):")
    rows = [
        ["Indices", "XYZ100", "$2M"],
        ["High Liq Equities", "NVDA, TSLA, META...", "$1M"],
        ["Low Liq Equities", "BABA, HOOD...", "$500K"],
        ["Gold", "GOLD", "$1M"],
        ["Silver", "SILVER", "$1M"],
        ["Oil", "CL", "$600K"],
        ["Metals", "COPPER", "$400K"],
        ["Energy", "NATGAS", "$300K"],
        ["Uranium", "URANIUM", "$200K"],
        ["Forex", "EUR, JPY", "$1M"],
    ]
    print_table(["Category", "Tokens", "Threshold"], rows)

    print("\nOther Sub-Exchanges (flx, hyna, km): $400K flat")


async def main():
    parser = argparse.ArgumentParser(description="View summary tables")
    parser.add_argument("section", nargs="?", default="all",
                       choices=["all", "sources", "wallets", "thresholds"],
                       help="Which section to show")
    parser.add_argument("--liq-json", help="Path to liquidation history JSON")

    args = parser.parse_args()

    if args.section in ["all", "sources"]:
        await show_hyperdash_summary()
        show_liq_history_summary(args.liq_json)

    if args.section in ["all", "wallets"]:
        show_wallet_summary()

    if args.section in ["all", "thresholds"]:
        show_thresholds()


if __name__ == "__main__":
    asyncio.run(main())
