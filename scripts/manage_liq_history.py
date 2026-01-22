#!/usr/bin/env python3
"""
Liquidation History Management CLI
==================================

Manage the liquidation history database for recidivist tracking.

Commands:
    stats       Show database statistics
    import      Import from Telegram channel export (JSON)
    add         Add a single liquidation manually
    search      Search for an address
    recidivists List addresses liquidated multiple times
    clear       Clear the database
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.monitor.liquidation_feed import (
    LiquidationHistoryDB,
    LiquidationParser,
    TelegramLiquidationListener,
    ParsedLiquidation,
)


DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "liquidation_history.db"


def cmd_stats(args):
    """Show database statistics."""
    db = LiquidationHistoryDB(args.db_path)
    stats = db.get_stats()

    print("\n=== Liquidation History Statistics ===\n")
    print(f"Total liquidation events: {stats['total_events']:,}")
    print(f"Unique addresses: {stats['unique_addresses']:,}")
    print(f"Events in last 24h: {stats['last_24h']:,}")

    print("\nAddresses by max liquidation size:")
    for tier, count in stats['by_tier'].items():
        print(f"  {tier}: {count:,}")


def cmd_import(args):
    """Import from Telegram channel export."""
    db = LiquidationHistoryDB(args.db_path)
    listener = TelegramLiquidationListener(db)

    export_path = Path(args.file)
    if not export_path.exists():
        print(f"Error: File not found: {export_path}")
        return 1

    print(f"Importing from {export_path}...")
    count = listener.import_from_export(export_path)
    print(f"Successfully imported {count} liquidation events")

    # Show updated stats
    stats = db.get_stats()
    print(f"\nDatabase now has {stats['total_events']:,} events from {stats['unique_addresses']:,} addresses")


def cmd_add(args):
    """Add a single liquidation manually."""
    db = LiquidationHistoryDB(args.db_path)

    # Parse the message or construct from args
    if args.message:
        parsed = LiquidationParser.parse_message(args.message)
        if not parsed:
            print("Error: Could not parse message")
            return 1
    else:
        # Construct from individual args
        if not all([args.address, args.token, args.notional]):
            print("Error: Must provide either --message or --address, --token, and --notional")
            return 1

        parsed = ParsedLiquidation(
            address=args.address.lower(),
            token=args.token.upper(),
            exchange=args.exchange or "main",
            side=args.side or "Long",
            notional=float(args.notional),
            price=float(args.price) if args.price else 0,
            timestamp=datetime.now(timezone.utc),
            raw_message=f"Manual entry: {args.address} {args.token} ${args.notional}",
        )

    if db.record_liquidation(parsed):
        print(f"Added: {parsed.address[:16]}... {parsed.token} ${parsed.notional:,.0f}")
    else:
        print("Duplicate event (not added)")


def cmd_search(args):
    """Search for an address."""
    db = LiquidationHistoryDB(args.db_path)
    address = args.address.lower()

    history = db.get_address_history(address)
    if not history:
        print(f"No liquidation history found for {address}")
        return

    print(f"\n=== Liquidation History for {address[:16]}... ===\n")
    print(f"Total liquidations: {len(history)}")

    for event in history:
        print(f"\n  {event['timestamp']}")
        print(f"  Token: {event['token']} ({event['exchange']})")
        print(f"  Side: {event['side']}")
        print(f"  Notional: ${event['notional']:,.0f}")
        print(f"  Price: ${event['price']:,.2f}")


def cmd_recidivists(args):
    """List addresses liquidated multiple times."""
    db = LiquidationHistoryDB(args.db_path)
    recidivists = db.get_recidivists(min_liquidations=args.min_liqs)

    if not recidivists:
        print(f"No addresses found with {args.min_liqs}+ liquidations")
        return

    print(f"\n=== Recidivists ({args.min_liqs}+ liquidations) ===\n")
    print(f"{'Address':<44} {'Count':>6} {'Max Notional':>14} {'Tokens'}")
    print("-" * 90)

    for r in recidivists[:args.limit]:
        tokens = json.loads(r['tokens_liquidated'] or '[]')
        tokens_str = ', '.join(tokens[:3]) + ('...' if len(tokens) > 3 else '')
        print(f"{r['address']:<44} {r['total_liquidations']:>6} ${r['max_notional']:>12,.0f} {tokens_str}")


def cmd_clear(args):
    """Clear the database."""
    if not args.confirm:
        print("This will delete all liquidation history data.")
        print("Run with --confirm to proceed.")
        return 1

    db_path = args.db_path
    if db_path.exists():
        db_path.unlink()
        print(f"Deleted {db_path}")
    else:
        print(f"Database not found: {db_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Manage liquidation history database for recidivist tracking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to database file (default: {DEFAULT_DB_PATH})"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # stats command
    subparsers.add_parser("stats", help="Show database statistics")

    # import command
    import_parser = subparsers.add_parser("import", help="Import from Telegram export")
    import_parser.add_argument("file", help="Path to Telegram channel export JSON")

    # add command
    add_parser = subparsers.add_parser("add", help="Add a single liquidation")
    add_parser.add_argument("--message", "-m", help="Raw Telegram message to parse")
    add_parser.add_argument("--address", "-a", help="Wallet address")
    add_parser.add_argument("--token", "-t", help="Token symbol (e.g., BTC, xyz:SILVER)")
    add_parser.add_argument("--notional", "-n", help="Notional value in USD")
    add_parser.add_argument("--price", "-p", help="Liquidation price")
    add_parser.add_argument("--side", "-s", choices=["Long", "Short"], default="Long")
    add_parser.add_argument("--exchange", "-e", default="main")

    # search command
    search_parser = subparsers.add_parser("search", help="Search for an address")
    search_parser.add_argument("address", help="Wallet address to search")

    # recidivists command
    recid_parser = subparsers.add_parser("recidivists", help="List multi-liquidated addresses")
    recid_parser.add_argument("--min-liqs", type=int, default=2, help="Minimum liquidations")
    recid_parser.add_argument("--limit", type=int, default=50, help="Max results to show")

    # clear command
    clear_parser = subparsers.add_parser("clear", help="Clear the database")
    clear_parser.add_argument("--confirm", action="store_true", help="Confirm deletion")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Route to command handler
    commands = {
        "stats": cmd_stats,
        "import": cmd_import,
        "add": cmd_add,
        "search": cmd_search,
        "recidivists": cmd_recidivists,
        "clear": cmd_clear,
    }

    return commands[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
