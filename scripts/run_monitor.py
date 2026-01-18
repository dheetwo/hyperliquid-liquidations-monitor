#!/usr/bin/env python3
"""
Liquidation Monitor Service - CLI Entry Point
==============================================

Runs the continuous monitoring service for liquidation hunting opportunities.

Default Mode (Scheduled):
    Scans at specific times (all EST):
    - 6:30 AM: Comprehensive scan (baseline reset, full watchlist)
    - Every hour (:00): Normal scan (alerts only for NEW positions since baseline)
    - Every 30 min (:30): Priority scan (alerts only for NEW positions since baseline)

Manual Mode (--manual):
    Fixed interval between scans (original behavior).

Usage:
    # Start monitor with scheduled mode (default)
    python scripts/run_monitor.py

    # Start with manual mode (fixed 90 min interval)
    python scripts/run_monitor.py --manual

    # Manual mode with custom interval
    python scripts/run_monitor.py --manual --interval 60

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

from src.monitor.service import MonitorService
from src.monitor.alerts import send_test_alert
from config.monitor_settings import (
    SCAN_INTERVAL_MINUTES,
    POLL_INTERVAL_SECONDS,
    DEFAULT_SCAN_MODE,
    LOG_LEVEL,
    LOG_FILE,
    COMPREHENSIVE_SCAN_HOUR,
    COMPREHENSIVE_SCAN_MINUTE,
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

    # Reduce noise from requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # Log which file we're writing to
    root_logger.info(f"Logging to: {dated_log_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Liquidation Monitor Service',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Scheduled Mode (default):
  Scans at specific times (EST):
    - {COMPREHENSIVE_SCAN_HOUR:02d}:{COMPREHENSIVE_SCAN_MINUTE:02d} AM: Comprehensive (baseline)
    - Every hour (:00): Normal scan
    - Every 30 min (:30): Priority scan

Manual Mode (--manual):
  Fixed interval between scans.

Examples:
  python scripts/run_monitor.py                       # Scheduled mode (default)
  python scripts/run_monitor.py --manual              # Manual mode, 90min interval
  python scripts/run_monitor.py --manual -i 60       # Manual mode, 60min interval
  python scripts/run_monitor.py --dry-run             # Console alerts only
  python scripts/run_monitor.py --test-telegram       # Test Telegram setup
        """
    )

    parser.add_argument(
        '--manual',
        action='store_true',
        help='Use manual mode with fixed interval (default: scheduled time-based mode)'
    )

    parser.add_argument(
        '--interval', '-i',
        type=int,
        default=SCAN_INTERVAL_MINUTES,
        help=f'Scan interval in minutes for manual mode (default: {SCAN_INTERVAL_MINUTES})'
    )

    parser.add_argument(
        '--poll',
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f'Price poll interval in seconds (default: {POLL_INTERVAL_SECONDS})'
    )

    parser.add_argument(
        '--mode', '-m',
        choices=['high-priority', 'normal', 'comprehensive'],
        default=DEFAULT_SCAN_MODE,
        help=f'Default scan mode for manual mode (default: {DEFAULT_SCAN_MODE})'
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
    if args.manual:
        print(f"Mode:           MANUAL (fixed interval)")
        print(f"Scan interval:  {args.interval} minutes")
        print(f"Scan mode:      {args.mode}")
    else:
        print(f"Mode:           SCHEDULED (time-based)")
        print(f"Schedule:       {COMPREHENSIVE_SCAN_HOUR:02d}:{COMPREHENSIVE_SCAN_MINUTE:02d} comprehensive, :00 normal, :30 priority")
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

    # Create and run monitor service
    try:
        service = MonitorService(
            scan_interval_minutes=args.interval,
            poll_interval_seconds=args.poll,
            scan_mode=args.mode,
            dry_run=args.dry_run,
            manual_mode=args.manual,
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
