#!/usr/bin/env python3
"""
CLI entry point for position scanning.

Usage:
    python scripts/scan_positions.py                    # Normal mode (default)
    python scripts/scan_positions.py --mode high-priority
    python scripts/scan_positions.py --mode comprehensive
    python scripts/scan_positions.py -m normal -o custom_output.csv

Scan Modes:
    high-priority  - kraken + large_whale, main + xyz only
    normal         - kraken + large_whale + whale, main + xyz only
    comprehensive  - all cohorts, all exchanges
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scrapers.position import run_scan_mode, SCAN_MODES


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Scan positions from Hyperliquid",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scan Modes:
  high-priority  - kraken + large_whale, main + xyz only
  normal         - kraken + large_whale + whale, main + xyz only
  comprehensive  - all cohorts, all exchanges
"""
    )
    parser.add_argument(
        "--mode", "-m",
        choices=list(SCAN_MODES.keys()),
        default="normal",
        help="Scan mode (default: normal)"
    )
    parser.add_argument(
        "--cohort-file",
        default="data/raw/cohort_data.csv",
        help="Cohort CSV file"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output CSV file (default: data/raw/position_data_{mode}.csv)"
    )
    args = parser.parse_args()

    cohort_path = Path(args.cohort_file)
    if not cohort_path.exists():
        print(f"Error: {args.cohort_file} not found. Run cohort scraper first.")
        sys.exit(1)

    run_scan_mode(args.mode, str(cohort_path), args.output)


if __name__ == "__main__":
    main()
