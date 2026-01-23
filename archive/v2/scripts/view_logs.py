#!/usr/bin/env python3
"""
View Logs from SQLite Database
==============================

Query persisted logs from the monitor service database.

Usage:
    # View last 24 hours of logs
    python scripts/view_logs.py

    # View last 6 hours
    python scripts/view_logs.py --hours 6

    # Filter by level
    python scripts/view_logs.py --level ERROR

    # Show more logs
    python scripts/view_logs.py --limit 500

    # Show database stats
    python scripts/view_logs.py --stats
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.monitor.database import MonitorDatabase


def format_log(log: dict) -> str:
    """Format a log entry for display."""
    timestamp = log['timestamp'][:19].replace('T', ' ')  # Truncate microseconds
    level = log['level'].ljust(8)
    name = log['logger_name']
    message = log['message']

    # Truncate long logger names
    if len(name) > 30:
        name = '...' + name[-27:]

    output = f"{timestamp} {level} {name}: {message}"

    # Add exception info if present
    if log.get('exc_info'):
        output += f"\n{log['exc_info']}"

    return output


def main():
    parser = argparse.ArgumentParser(
        description='View logs from the monitor service database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '--hours', '-t',
        type=int,
        default=24,
        help='Hours of logs to show (default: 24)'
    )

    parser.add_argument(
        '--level', '-l',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Filter by log level'
    )

    parser.add_argument(
        '--limit', '-n',
        type=int,
        default=100,
        help='Maximum logs to show (default: 100)'
    )

    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show database statistics'
    )

    parser.add_argument(
        '--db',
        type=str,
        default=str(project_root / "data" / "monitor.db"),
        help='Path to database file'
    )

    args = parser.parse_args()

    # Check if database exists
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("The monitor service needs to run first to create the database.")
        sys.exit(1)

    db = MonitorDatabase(db_path)

    if args.stats:
        stats = db.get_stats()
        print("\nDatabase Statistics:")
        print("-" * 40)
        print(f"  Watchlist entries:     {stats['watchlist']:,}")
        print(f"  Baseline positions:    {stats['baseline_positions']:,}")
        print(f"  Position history:      {stats['position_history']:,}")
        print(f"  Alert log entries:     {stats['alert_log']:,}")
        print(f"  Service log entries:   {stats['service_logs']:,}")
        print(f"  Database size:         {stats['file_size_mb']:.2f} MB")
        print()
        return

    # Get logs
    logs = db.get_logs(hours=args.hours, level=args.level, limit=args.limit)

    if not logs:
        print(f"No logs found in the last {args.hours} hours")
        if args.level:
            print(f"(filtered by level: {args.level})")
        return

    print(f"\n=== Last {len(logs)} logs (most recent first) ===\n")

    # Reverse to show oldest first (more natural reading order)
    for log in reversed(logs):
        print(format_log(log))

    print(f"\n=== Showing {len(logs)} logs from last {args.hours} hours ===")
    if args.level:
        print(f"(filtered by level: {args.level})")


if __name__ == "__main__":
    main()
