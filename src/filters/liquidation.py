"""
Liquidation Filter
==================

Filters position data for liquidation hunting opportunities.

Steps:
1. Filter out positions without a liquidation price
2. Fetch current prices and calculate distance to liquidation
3. Calculate estimated_liquidatable_value based on margin type
4. Fetch order books and calculate:
   - Notional required to trigger liquidation (hunting cost)
   - Estimated price impact upon liquidation (profit potential)
5. Calculate hunting score based on all factors

Usage:
    python liq_filter.py                           # Filter position_data_priority.csv
    python liq_filter.py position_data.csv         # Filter specific file
    python liq_filter.py --input position_data.csv --output filtered.csv
"""

import csv
import argparse
import logging
import requests
import time
from typing import Dict, List, Any, Tuple, Optional
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

# Configurable ratio for cross-margin positions
CROSS_POSITION_LIQUIDATABLE_RATIO = 0.20  # 20%

# Rate limiting for order book fetches
ORDERBOOK_DELAY = 0.1  # seconds between order book requests


def fetch_all_mark_prices() -> Dict[str, float]:
    """
    Fetch current mark prices for all perp tokens across all exchanges.

    Returns:
        Dict mapping token symbol to mark price
    """
    all_prices = {}
    dexes = ["", "xyz", "flx", "hyna", "km"]  # "" = main exchange; vntl excluded (no external price discovery)

    for dex in dexes:
        try:
            payload = {"type": "allMids"}
            if dex:
                payload["dex"] = dex

            response = requests.post(HYPERLIQUID_API, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Store prices with exchange prefix for sub-exchanges
            for token, price in data.items():
                if dex:
                    # Sub-exchange tokens: store both prefixed and unprefixed
                    all_prices[f"{dex}:{token}"] = float(price)
                all_prices[token] = float(price)

            dex_name = dex if dex else "main"
            logger.info(f"Fetched {len(data)} prices from {dex_name} exchange")

        except Exception as e:
            dex_name = dex if dex else "main"
            logger.error(f"Failed to fetch prices from {dex_name}: {e}")

    return all_prices


def get_current_price(token: str, exchange: str, mark_prices: Dict[str, float]) -> float:
    """
    Get current price for a token, handling exchange-specific lookups.

    Args:
        token: Token symbol (e.g., "BTC", "TSLA")
        exchange: Exchange name ("main", "xyz", "flx", "vntl", "hyna", "km")
        mark_prices: Dict of all mark prices

    Returns:
        Current mark price, or 0 if not found
    """
    # Try exchange-prefixed lookup first for sub-exchanges
    if exchange != "main":
        prefixed = f"{exchange}:{token}"
        if prefixed in mark_prices:
            return mark_prices[prefixed]

    # Fall back to direct lookup
    return mark_prices.get(token, 0)


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


def fetch_order_book(token: str, exchange: str = "main") -> Optional[Dict[str, List]]:
    """
    Fetch L2 order book for a token.

    Args:
        token: Token symbol (e.g., "BTC", "TSLA")
        exchange: Exchange name ("main", "xyz", "flx", "vntl", "hyna", "km")

    Returns:
        Dict with 'bids' and 'asks' lists, each containing [price, size] pairs
        None if fetch fails
    """
    try:
        payload = {"type": "l2Book", "coin": token}
        if exchange != "main":
            payload["dex"] = exchange

        response = requests.post(HYPERLIQUID_API, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        levels = data.get("levels", [[], []])

        # Parse bids and asks into [price, size] pairs
        bids = []
        for level in levels[0]:
            px = float(level.get("px", 0))
            sz = float(level.get("sz", 0))
            if px > 0 and sz > 0:
                bids.append([px, sz])

        asks = []
        for level in levels[1]:
            px = float(level.get("px", 0))
            sz = float(level.get("sz", 0))
            if px > 0 and sz > 0:
                asks.append([px, sz])

        return {"bids": bids, "asks": asks}

    except Exception as e:
        logger.debug(f"Failed to fetch order book for {token} on {exchange}: {e}")
        return None


def fetch_order_books_for_tokens(tokens: List[Tuple[str, str]]) -> Dict[str, Dict]:
    """
    Fetch order books for a list of (token, exchange) pairs.

    Args:
        tokens: List of (token, exchange) tuples

    Returns:
        Dict mapping "token:exchange" to order book data
    """
    order_books = {}
    total = len(tokens)

    logger.info(f"Fetching order books for {total} unique token/exchange pairs...")

    for i, (token, exchange) in enumerate(tokens):
        key = f"{token}:{exchange}"

        book = fetch_order_book(token, exchange)
        if book:
            order_books[key] = book

        # Progress logging
        if (i + 1) % 50 == 0:
            logger.info(f"  Order book progress: {i + 1}/{total}")

        time.sleep(ORDERBOOK_DELAY)

    logger.info(f"Fetched {len(order_books)} order books successfully")
    return order_books


def calculate_notional_to_trigger(
    order_book: Dict[str, List],
    current_price: float,
    liq_price: float,
    side: str
) -> Optional[float]:
    """
    Calculate notional USD required to move price from current to liquidation.

    For a Long position: need to SELL (consume bids) to push price DOWN to liq_price
    For a Short position: need to BUY (consume asks) to push price UP to liq_price

    Args:
        order_book: Dict with 'bids' and 'asks' lists
        current_price: Current mark price
        liq_price: Liquidation price
        side: "Long" or "Short"

    Returns:
        Notional USD required to trigger, or None if book exhausted
    """
    if side == "Long":
        # Need to sell to push price down - walk bids from current to liq
        levels = order_book.get("bids", [])
        # Filter levels between liq_price and current_price
        relevant_levels = [l for l in levels if liq_price <= l[0] <= current_price]
    else:
        # Need to buy to push price up - walk asks from current to liq
        levels = order_book.get("asks", [])
        # Filter levels between current_price and liq_price
        relevant_levels = [l for l in levels if current_price <= l[0] <= liq_price]

    if not relevant_levels:
        return None

    # Sum up the notional value of all levels between current and liq
    total_notional = 0
    for px, sz in relevant_levels:
        total_notional += px * sz

    return total_notional


def calculate_price_impact(
    order_book: Dict[str, List],
    current_price: float,
    liquidation_size_usd: float,
    side: str
) -> Optional[float]:
    """
    Calculate estimated price impact when a position is liquidated.

    Uses current order book depth as a proxy for liquidity at liquidation.
    This assumes similar market depth structure when liquidation occurs.

    When liquidated:
    - Long liquidation = forced SELL (consumes bids)
    - Short liquidation = forced BUY (consumes asks)

    Args:
        order_book: Dict with 'bids' and 'asks' lists
        current_price: Current mark price (used as reference)
        liquidation_size_usd: Notional value being liquidated
        side: "Long" or "Short" (the position being liquidated)

    Returns:
        Price impact as percentage, or None if insufficient book data
    """
    if side == "Long":
        # Long liquidation = forced sell = walk bids from top
        levels = order_book.get("bids", [])
        # Sort bids high to low (should already be, but ensure)
        levels = sorted(levels, key=lambda x: x[0], reverse=True)
    else:
        # Short liquidation = forced buy = walk asks from bottom
        levels = order_book.get("asks", [])
        # Sort asks low to high (should already be, but ensure)
        levels = sorted(levels, key=lambda x: x[0])

    if not levels:
        return None

    start_price = levels[0][0]  # Best bid or ask
    remaining_usd = liquidation_size_usd
    last_fill_price = start_price

    for px, sz in levels:
        level_value = px * sz

        if remaining_usd <= level_value:
            # Partial fill at this level
            last_fill_price = px
            remaining_usd = 0
            break
        else:
            # Consume entire level
            last_fill_price = px
            remaining_usd -= level_value

    if remaining_usd > 0:
        # Book exhausted - extrapolate using average price per level
        if len(levels) >= 2:
            avg_level_value = sum(l[0] * l[1] for l in levels) / len(levels)
            levels_needed = remaining_usd / avg_level_value if avg_level_value > 0 else 0
            avg_price_step = abs(levels[-1][0] - levels[0][0]) / len(levels) if len(levels) > 1 else 0
            # Extrapolate last fill price
            if side == "Long":
                last_fill_price = last_fill_price - (levels_needed * avg_price_step)
            else:
                last_fill_price = last_fill_price + (levels_needed * avg_price_step)

    # Calculate impact as percentage from start to final fill
    if start_price == 0:
        return None

    impact_pct = abs(last_fill_price - start_price) / start_price * 100
    return impact_pct


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

    # First pass: filter positions with liq price and collect unique tokens
    preliminary_rows = []
    skipped_no_liq = 0
    skipped_no_price = 0
    unique_tokens = set()

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

        preliminary_rows.append(row)
        unique_tokens.add((token, exchange))

    logger.info(f"Filtered to {len(preliminary_rows)} positions with liq prices")

    # Fetch order books for all unique tokens
    order_books = fetch_order_books_for_tokens(list(unique_tokens))

    # Second pass: calculate all metrics
    filtered_rows = []

    for row in preliminary_rows:
        liq_price = float(row['Liquidation Price'])
        token = row['Token']
        exchange = row['Exchange']
        side = row['Side']
        position_value = float(row['Position Value'])
        is_isolated = row['Isolated'].lower() == 'true'

        current_price = get_current_price(token, exchange, mark_prices)

        # Calculate base columns
        distance_pct = calculate_distance_to_liquidation(current_price, liq_price, side)
        est_liq_value = calculate_estimated_liquidatable_value(position_value, is_isolated)

        # Get order book for this token
        book_key = f"{token}:{exchange}"
        order_book = order_books.get(book_key)

        # Calculate order book based metrics
        notional_to_trigger = None
        price_impact_pct = None

        if order_book:
            notional_to_trigger = calculate_notional_to_trigger(
                order_book, current_price, liq_price, side
            )
            price_impact_pct = calculate_price_impact(
                order_book, current_price, est_liq_value, side
            )

        # Add columns to row
        row['Current Price'] = current_price
        row['Distance to Liq (%)'] = round(distance_pct, 4)
        row['Estimated Liquidatable Value'] = round(est_liq_value, 2)
        row['Notional to Trigger'] = round(notional_to_trigger, 2) if notional_to_trigger else ''
        row['Est Price Impact (%)'] = round(price_impact_pct, 4) if price_impact_pct else ''

        # Calculate hunting score
        # Formula: (Est Liq Value * Price Impact %) / (Notional to Trigger * Distance %²)
        # Higher score = better hunting opportunity
        if distance_pct > 0 and notional_to_trigger and notional_to_trigger > 0 and price_impact_pct:
            hunting_score = (est_liq_value * price_impact_pct) / (notional_to_trigger * (distance_pct ** 2))
        elif distance_pct > 0:
            # Fallback if order book data missing: Est Liq Value / Distance %²
            hunting_score = est_liq_value / (distance_pct ** 2)
        else:
            hunting_score = float('inf')

        row['Hunting Score'] = round(hunting_score, 2)

        filtered_rows.append(row)

    # Sort by hunting score (highest first)
    filtered_rows.sort(key=lambda x: x['Hunting Score'], reverse=True)

    # Write output
    output_fieldnames = list(input_fieldnames) + [
        'Current Price',
        'Distance to Liq (%)',
        'Estimated Liquidatable Value',
        'Notional to Trigger',
        'Est Price Impact (%)',
        'Hunting Score'
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
        'order_books_fetched': len(order_books),
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

    # Order book metrics coverage
    with_trigger = sum(1 for r in filtered_rows if r['Notional to Trigger'])
    with_impact = sum(1 for r in filtered_rows if r['Est Price Impact (%)'])
    stats['with_notional_to_trigger'] = with_trigger
    stats['with_price_impact'] = with_impact

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
    print(f"\nOrder book analysis:")
    print(f"  Books fetched:          {stats['order_books_fetched']:,}")
    print(f"  With notional-to-trigger: {stats['with_notional_to_trigger']:,}")
    print(f"  With price impact:        {stats['with_price_impact']:,}")
    print(f"\nCross position ratio: {CROSS_POSITION_LIQUIDATABLE_RATIO*100:.0f}%")
    print(f"\nHunting Score formula:")
    print(f"  (Est Liq Value × Price Impact %) / (Notional to Trigger × Distance %²)")


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
