#!/usr/bin/env python3
"""
Liquidation Monitor Service - CLI Entry Point
==============================================

Runs the continuous monitoring service for liquidation hunting opportunities.

Architecture:
    - Initial comprehensive scan to populate position cache
    - Continuous tiered refresh based on liquidation distance:
      - Critical (<=0.125%): Continuous (~5 req/sec)
      - High (0.125-0.25%): Every 2-3 seconds
      - Normal (>0.25%): Every 30 seconds
    - Dynamic discovery scans for new addresses (frequency based on API pressure)
    - Two daily summaries at 7am and 4pm EST
    - No intraday "new position" alerts - quiet backend updates

Usage:
    # Start monitor
    python scripts/run_monitor.py

    # Dry run (console alerts only, no Telegram)
    python scripts/run_monitor.py --dry-run

    # Test Telegram configuration
    python scripts/run_monitor.py --test-telegram
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.monitor import MonitorService
from src.monitor.alerts import send_test_alert
from src.monitor.database import MonitorDatabase, SQLiteLoggingHandler
from config.monitor_settings import (
    POLL_INTERVAL_SECONDS,
    LOG_LEVEL,
    LOG_FILE,
    DAILY_SUMMARY_TIMES,
    CACHE_TIER_CRITICAL_PCT,
    CACHE_TIER_HIGH_PCT,
)


def ensure_directories():
    """Ensure required data directories exist."""
    dirs = [
        project_root / "data" / "raw",
        project_root / "data" / "processed",
        project_root / "logs",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def setup_logging(log_level: str = LOG_LEVEL, log_file: str = LOG_FILE):
    """Configure logging for the monitor service."""
    from datetime import datetime

    # Ensure all directories exist
    ensure_directories()

    # Create logs directory if needed
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Create date-stamped log file (e.g., logs/monitor_2026-01-18.log)
    date_str = datetime.now().strftime("%Y-%m-%d")
    dated_log_file = log_path.parent / f"{log_path.stem}_{date_str}{log_path.suffix}"

    # Get root logger and clear any existing handlers
    # (scrapers may have set up handlers at import time via basicConfig)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Configure root logger with both file and console output
    root_logger.setLevel(getattr(logging, log_level.upper()))

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # File handler (date-stamped)
    file_handler = logging.FileHandler(dated_log_file)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # SQLite handler (persists logs to database, survives container restarts)
    sqlite_handler = SQLiteLoggingHandler(
        db_path=project_root / "data" / "monitor.db",
        level=getattr(logging, log_level.upper())
    )
    sqlite_handler.setFormatter(formatter)
    root_logger.addHandler(sqlite_handler)

    # Reduce noise from requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # Log which file we're writing to
    root_logger.info(f"Logging to: {dated_log_file}")
    root_logger.info(f"Logs also persisted to SQLite database")


def format_summary_times() -> str:
    """Format daily summary times for display."""
    times = []
    for hour, minute in DAILY_SUMMARY_TIMES:
        period = "AM" if hour < 12 else "PM"
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        times.append(f"{display_hour}:{minute:02d} {period}")
    return ", ".join(times)


def main():
    parser = argparse.ArgumentParser(
        description='Liquidation Monitor Service',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Architecture:
  - Initial comprehensive scan to populate cache (all cohorts, all exchanges)
  - Tiered refresh based on liquidation distance:
    - Critical (<={CACHE_TIER_CRITICAL_PCT}%): Continuous (~5 req/sec)
    - High ({CACHE_TIER_CRITICAL_PCT}-{CACHE_TIER_HIGH_PCT}%): Every 2-3 seconds
    - Normal (>{CACHE_TIER_HIGH_PCT}%): Every 30 seconds
  - Dynamic discovery scans (frequency based on API pressure)
  - Daily summaries at {format_summary_times()} EST

Examples:
  python scripts/run_monitor.py                # Start monitor
  python scripts/run_monitor.py --dry-run      # Console alerts only
  python scripts/run_monitor.py --test-telegram # Test Telegram setup
  python scripts/run_monitor.py --clear-db     # Clear database and start fresh
        """
    )

    parser.add_argument(
        '--poll',
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f'Price poll interval in seconds (default: {POLL_INTERVAL_SECONDS})'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print alerts to console instead of sending to Telegram'
    )

    parser.add_argument(
        '--test-telegram',
        action='store_true',
        help='Send a test alert to verify Telegram configuration'
    )

    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default=LOG_LEVEL,
        help=f'Log level (default: {LOG_LEVEL})'
    )

    parser.add_argument(
        '--clear-db',
        action='store_true',
        help='Clear ALL database tables INCLUDING wallet addresses (use --clear-cache instead to preserve addresses)'
    )

    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='Clear ephemeral cache (position_cache, watchlist, baseline) while preserving wallet addresses'
    )

    parser.add_argument(
        '--skip-startup-summary',
        action='store_true',
        help='Skip the startup watchlist summary (useful for re-deployments)'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Test Telegram mode
    if args.test_telegram:
        print("Testing Telegram configuration...")
        success = send_test_alert(dry_run=args.dry_run)
        if success:
            print("Test alert sent successfully!")
            sys.exit(0)
        else:
            print("Failed to send test alert. Check your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
            sys.exit(1)

    # Print configuration
    print("\n" + "=" * 60)
    print("LIQUIDATION MONITOR SERVICE")
    print("=" * 60)
    print(f"Architecture:   Cache-based with tiered refresh")
    print(f"Refresh tiers:")
    print(f"  - Critical (<={CACHE_TIER_CRITICAL_PCT}%): Continuous")
    print(f"  - High ({CACHE_TIER_CRITICAL_PCT}-{CACHE_TIER_HIGH_PCT}%): Every 2-3 sec")
    print(f"  - Normal (>{CACHE_TIER_HIGH_PCT}%): Every 30 sec")
    print(f"Daily summaries: {format_summary_times()} EST")
    print(f"Poll interval:  {args.poll} seconds")
    print(f"Dry run:        {args.dry_run}")
    print(f"Log level:      {args.log_level}")
    print("=" * 60)

    if not args.dry_run:
        import os
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("\nWARNING: TELEGRAM_BOT_TOKEN not set!")
            print("Set environment variable or use --dry-run for console output.")
            print("To set: export TELEGRAM_BOT_TOKEN=your_token")
            sys.exit(1)
        if not os.environ.get("TELEGRAM_CHAT_ID"):
            print("\nWARNING: TELEGRAM_CHAT_ID not set!")
            print("Set environment variable or use --dry-run for console output.")
            print("To set: export TELEGRAM_CHAT_ID=your_chat_id")
            sys.exit(1)

    # Clear cache if requested (preserves wallet addresses)
    if args.clear_cache:
        print("\nClearing cache (preserving wallet addresses)...")
        db = MonitorDatabase()
        db.clear_watchlist()
        db.clear_baseline()
        db.clear_position_cache()
        # Preserve: cohort_cache, known_addresses, liquidation_history.db
        db.vacuum()
        print("Cache cleared successfully. Wallet addresses preserved.")
        logger.info("Cache cleared via --clear-cache flag (wallet addresses preserved)")

    # Clear ALL database if requested (including wallet addresses)
    elif args.clear_db:
        print("\n⚠️  WARNING: Clearing ALL database including wallet addresses...")
        print("   (Use --clear-cache to preserve wallet addresses)")
        db = MonitorDatabase()
        db.clear_watchlist()
        db.clear_baseline()
        db.clear_cohort_cache()
        db.clear_position_cache()
        db.clear_known_addresses()
        # Also clear history/logs if desired for full reset
        db.prune_old_data(history_days=0, alert_days=0, log_days=0)
        db.vacuum()
        print("Database cleared successfully (including wallet addresses).")
        logger.info("Database cleared via --clear-db flag (including wallet addresses)")

    # Create and run monitor service
    try:
        service = MonitorService(
            poll_interval_seconds=args.poll,
            dry_run=args.dry_run,
            skip_startup_summary=args.skip_startup_summary,
        )

        print("\nStarting monitor service...")
        print("Press Ctrl+C to stop\n")

        service.run()

    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Monitor service error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
