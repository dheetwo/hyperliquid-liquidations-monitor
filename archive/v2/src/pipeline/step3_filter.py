"""
Step 3: Liquidation Filter
==========================

Filters position data for liquidation monitoring.

Steps:
1. Filter out positions without a liquidation price
2. Fetch current prices and calculate distance to liquidation
3. Calculate estimated_liquidatable_value based on margin type

Usage:
    python -m src.pipeline.step3_filter                           # Filter position_data_priority.csv
    python -m src.pipeline.step3_filter position_data.csv         # Filter specific file
    python -m src.pipeline.step3_filter --input position_data.csv --output filtered.csv
"""

import csv
import argparse
import logging
from typing import Dict, Any
from pathlib import Path

from src.utils.prices import (
    fetch_all_mark_prices,
    get_current_price,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configurable ratio for cross-margin positions
CROSS_POSITION_LIQUIDATABLE_RATIO = 0.20  # 20%


def calculate_distance_to_liquidation(current_price: float, liq_price: float, side: str) -> float:
    """
    Calculate percentage distance from current price to liquidation price.

    Positive = price needs to move against position to liquidate
    Negative = already past liquidation (shouldn't happen normally)

    Args:
        current_price: Current mark price
        liq_price: Liquidation price
        side: "Long" or "Short"

    Returns:
        Percentage distance (e.g., 5.0 means 5% away from liquidation)
    """
    if current_price == 0:
        return float('inf')

    if side == "Long":
        # Long liquidates when price drops to liq_price
        # Distance = how much price needs to drop (positive = safe)
        distance_pct = ((current_price - liq_price) / current_price) * 100
    else:
        # Short liquidates when price rises to liq_price
        # Distance = how much price needs to rise (positive = safe)
        distance_pct = ((liq_price - current_price) / current_price) * 100

    return distance_pct


def calculate_estimated_liquidatable_value(position_value: float, is_isolated: bool) -> float:
    """
    Calculate estimated liquidatable value based on margin type.

    - Isolated: 100% of position value (entire position liquidated)
    - Cross: CROSS_POSITION_LIQUIDATABLE_RATIO of position value

    Args:
        position_value: Notional position value in USD
        is_isolated: True if isolated margin, False if cross

    Returns:
        Estimated liquidatable value in USD
    """
    if is_isolated:
        return position_value
    else:
        return position_value * CROSS_POSITION_LIQUIDATABLE_RATIO


def filter_positions(input_file: str, output_file: str) -> Dict[str, Any]:
    """
    Filter position data and add liquidation analysis columns.

    Args:
        input_file: Path to input CSV
        output_file: Path to output CSV

    Returns:
        Summary statistics dict
    """
    # Fetch current prices
    logger.info("Fetching current mark prices...")
    mark_prices = fetch_all_mark_prices()

    if not mark_prices:
        logger.error("Failed to fetch mark prices. Aborting.")
        return {}

    # Read input file
    logger.info(f"Reading {input_file}...")
    rows = []
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        input_fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)

    total_rows = len(rows)
    logger.info(f"Loaded {total_rows} positions")

    # Filter and calculate metrics
    filtered_rows = []
    skipped_no_liq = 0
    skipped_no_price = 0

    for row in rows:
        # Skip if no liquidation price
        liq_price_str = row.get('Liquidation Price', '').strip()
        if not liq_price_str:
            skipped_no_liq += 1
            continue

        token = row['Token']
        exchange = row['Exchange']

        # Get current price
        current_price = get_current_price(token, exchange, mark_prices)
        if current_price == 0:
            skipped_no_price += 1
            logger.warning(f"No price found for {token} on {exchange}")
            continue

        liq_price = float(liq_price_str)
        side = row['Side']
        position_value = float(row['Position Value'])
        is_isolated = row['Isolated'].lower() == 'true'

        # Calculate metrics
        distance_pct = calculate_distance_to_liquidation(current_price, liq_price, side)
        est_liq_value = calculate_estimated_liquidatable_value(position_value, is_isolated)

        # Add columns to row
        row['Current Price'] = current_price
        row['Distance to Liq (%)'] = round(distance_pct, 4)
        row['Estimated Liquidatable Value'] = round(est_liq_value, 2)

        filtered_rows.append(row)

    logger.info(f"Filtered to {len(filtered_rows)} positions with liq prices")

    # Sort by distance to liquidation (closest first)
    filtered_rows.sort(key=lambda x: x['Distance to Liq (%)'])

    # Write output
    output_fieldnames = list(input_fieldnames) + [
        'Current Price',
        'Distance to Liq (%)',
        'Estimated Liquidatable Value',
    ]

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames)
        writer.writeheader()
        writer.writerows(filtered_rows)

    logger.info(f"Saved {len(filtered_rows)} filtered positions to {output_file}")

    # Summary stats
    stats = {
        'total_input': total_rows,
        'skipped_no_liq': skipped_no_liq,
        'skipped_no_price': skipped_no_price,
        'filtered_count': len(filtered_rows),
        'isolated_count': sum(1 for r in filtered_rows if r['Isolated'].lower() == 'true'),
        'cross_count': sum(1 for r in filtered_rows if r['Isolated'].lower() == 'false'),
    }

    # Distance breakdown
    close_3pct = sum(1 for r in filtered_rows if r['Distance to Liq (%)'] <= 3)
    close_10pct = sum(1 for r in filtered_rows if r['Distance to Liq (%)'] <= 10)
    stats['within_3pct'] = close_3pct
    stats['within_10pct'] = close_10pct

    # Value breakdown
    total_est_value = sum(r['Estimated Liquidatable Value'] for r in filtered_rows)
    isolated_est_value = sum(r['Estimated Liquidatable Value'] for r in filtered_rows
                            if r['Isolated'].lower() == 'true')
    stats['total_est_liq_value'] = total_est_value
    stats['isolated_est_liq_value'] = isolated_est_value

    return stats


def print_summary(stats: Dict[str, Any], input_file: str, output_file: str):
    """Print summary of filtering results."""
    print(f"\n{'='*60}")
    print("FILTER COMPLETE")
    print(f"{'='*60}")
    print(f"Input:  {input_file}")
    print(f"Output: {output_file}")
    print(f"\nPositions:")
    print(f"  Total input:        {stats['total_input']:,}")
    print(f"  Skipped (no liq):   {stats['skipped_no_liq']:,}")
    print(f"  Skipped (no price): {stats['skipped_no_price']:,}")
    print(f"  Filtered output:    {stats['filtered_count']:,}")
    print(f"\nMargin type:")
    print(f"  Isolated: {stats['isolated_count']:,}")
    print(f"  Cross:    {stats['cross_count']:,}")
    print(f"\nDistance to liquidation:")
    print(f"  Within 3%:  {stats['within_3pct']:,}")
    print(f"  Within 10%: {stats['within_10pct']:,}")
    print(f"\nEstimated liquidatable value:")
    print(f"  Total:    ${stats['total_est_liq_value']:,.2f}")
    print(f"  Isolated: ${stats['isolated_est_liq_value']:,.2f}")
    print(f"\nCross position ratio: {CROSS_POSITION_LIQUIDATABLE_RATIO*100:.0f}%")


def main():
    parser = argparse.ArgumentParser(description='Filter position data for liquidation targets')
    parser.add_argument('input_file', nargs='?', default='data/raw/position_data_priority.csv',
                       help='Input CSV file (default: data/raw/position_data_priority.csv)')
    parser.add_argument('--output', '-o', default=None,
                       help='Output CSV file (default: data/processed/filtered_<input>)')

    args = parser.parse_args()

    input_file = args.input_file

    # Generate output filename if not specified
    if args.output:
        output_file = args.output
    else:
        input_path = Path(input_file)
        output_file = f"data/processed/filtered_{input_path.name}"

    # Check input exists
    if not Path(input_file).exists():
        logger.error(f"Input file not found: {input_file}")
        return

    stats = filter_positions(input_file, output_file)

    if stats:
        print_summary(stats, input_file, output_file)


if __name__ == "__main__":
    main()
