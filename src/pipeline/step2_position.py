"""
Step 2: Position Scraper
========================

Fetches position data for all wallet addresses from cohort_data.csv
using the Hyperliquid API.

Priority order: Kraken -> Large Whale -> Whale -> Shark

Outputs a CSV with columns:
    Address, Token, Side, Size, Leverage, Leverage Type, Entry Price,
    Mark Price, Position Value, Unrealized PnL, ROE, Liquidation Price,
    Margin Used, Funding (Since Open), Cohort

Supports both sync and async modes for API calls.
"""

import asyncio
import csv
import logging
import requests
import time
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path

import aiohttp

from src.models import Position
from src.utils.prices import HYPERLIQUID_API

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Cohort groupings
HIGH_PRIORITY_COHORTS = ["kraken", "large_whale", "rekt"]
NORMAL_COHORTS = ["kraken", "large_whale", "whale", "rekt", "extremely_profitable", "very_unprofitable", "very_profitable"]
ALL_COHORTS = ["kraken", "large_whale", "whale", "rekt", "extremely_profitable", "shark", "very_unprofitable", "very_profitable", "profitable", "unprofitable"]

# DEX/Exchange identifiers
MAIN_DEX = ""
CORE_DEXES = [MAIN_DEX, "xyz"]  # main + xyz only
ALL_DEXES = [MAIN_DEX, "xyz", "flx", "hyna", "km"]  # vntl excluded: no external price discovery

# Scan mode configurations
# high-priority: kraken + large_whale + rekt, main + xyz only
# normal: kraken + large_whale + whale + rekt + profit/loss cohorts, main + xyz only
# comprehensive: all cohorts, all exchanges
# whale-only: whale cohort only (incremental for progressive scan)
# shark-incremental: shark cohort + all exchanges for all cohorts already scanned
SCAN_MODES = {
    "high-priority": {
        "cohorts": HIGH_PRIORITY_COHORTS,
        "dexes": CORE_DEXES,
    },
    "normal": {
        "cohorts": NORMAL_COHORTS,
        "dexes": CORE_DEXES,
    },
    "comprehensive": {
        "cohorts": ALL_COHORTS,
        "dexes": ALL_DEXES,
    },
    # Incremental modes for progressive startup scan (avoid re-scanning)
    "whale-only": {
        "cohorts": ["whale"],
        "dexes": CORE_DEXES,
    },
    "shark-incremental": {
        "cohorts": ["shark"],  # Secondary cohorts
        "dexes": ALL_DEXES,    # All exchanges (secondary cohorts need full coverage)
        # Note: Also need to scan existing cohorts on new exchanges
        # This is handled by additional_scans below
        "additional_scans": [
            # Scan normal cohorts on the extra exchanges they missed
            {"cohorts": NORMAL_COHORTS, "dexes": ["flx", "hyna", "km"]},
        ],
    },
}

# Legacy aliases for backward compatibility
PRIORITY_COHORTS = NORMAL_COHORTS
SECONDARY_COHORTS = ["shark", "very_unprofitable", "very_profitable"]
SUB_EXCHANGES = ["xyz", "flx", "hyna", "km"]  # vntl excluded: no external price discovery

# Rate limiting settings (sync mode)
REQUEST_DELAY = 0.2  # seconds between API calls
BATCH_DELAY = 2.0    # seconds between batches of 50
DEX_DELAY = 0.1      # seconds between dex queries for same address

# Async concurrency settings
MAX_CONCURRENT_REQUESTS = 20  # Max concurrent API calls
ASYNC_REQUEST_DELAY = 0.05    # Small delay between launching requests


def load_cohort_addresses(
    csv_path: str = "data/raw/cohort_data.csv",
    cohorts: List[str] = None
) -> List[Tuple[str, str]]:
    """
    Load addresses from cohort CSV, filtered by specified cohorts.

    Args:
        csv_path: Path to cohort data CSV
        cohorts: List of cohorts to include (default: all cohorts)

    Returns:
        List of (address, cohort) tuples, sorted by priority
    """
    if cohorts is None:
        cohorts = ALL_COHORTS

    addresses_by_cohort = {cohort: [] for cohort in cohorts}

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            address = row['Address']
            cohort = row['Cohort']
            if cohort in addresses_by_cohort:
                addresses_by_cohort[cohort].append(address)

    # Flatten in priority order
    result = []
    for cohort in ALL_COHORTS:  # Use full priority order for sorting
        if cohort in addresses_by_cohort:
            for address in addresses_by_cohort[cohort]:
                result.append((address, cohort))

    logger.info(f"Loaded {len(result)} addresses from {csv_path}")
    for cohort in cohorts:
        logger.info(f"  {cohort}: {len(addresses_by_cohort.get(cohort, []))}")

    return result


def fetch_all_mark_prices() -> Dict[str, float]:
    """
    Fetch current mark prices for all perp tokens across all exchanges.

    Returns:
        Dict mapping token symbol to mark price (includes prefixed tokens like xyz:TSLA)
    """
    all_prices = {}

    for dex in ALL_DEXES:
        try:
            payload = {"type": "allMids"}
            if dex:  # Add dex parameter for sub-exchanges
                payload["dex"] = dex

            response = requests.post(
                HYPERLIQUID_API,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            # Response is a dict of token -> price string
            prices = {token: float(price) for token, price in data.items()}
            all_prices.update(prices)

            dex_name = dex if dex else "main"
            logger.debug(f"Fetched {len(prices)} mark prices from {dex_name} exchange")

        except Exception as e:
            dex_name = dex if dex else "main"
            logger.error(f"Failed to fetch mark prices from {dex_name}: {e}")

    logger.debug(f"Total mark prices fetched: {len(all_prices)}")
    return all_prices


def fetch_positions_for_dex(address: str, dex: str = "") -> List[Dict[str, Any]]:
    """
    Fetch positions for a single address from a specific exchange.

    Args:
        address: Wallet address
        dex: Exchange identifier ("" for main, "xyz", "flx")

    Returns:
        List of position dicts from API response
    """
    try:
        payload = {
            "type": "clearinghouseState",
            "user": address
        }
        if dex:
            payload["dex"] = dex

        response = requests.post(
            HYPERLIQUID_API,
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        return data.get("assetPositions", [])

    except Exception as e:
        dex_name = dex if dex else "main"
        logger.debug(f"Failed to fetch {dex_name} positions for {address}: {e}")
        return []


def fetch_all_positions_for_address(
    address: str,
    mark_prices: Dict[str, float] = None,
    dexes: List[str] = None
) -> List[Tuple[Dict[str, Any], str]]:
    """
    Fetch positions for a single address across specified exchanges.

    Args:
        address: Wallet address
        mark_prices: Dict of mark prices (unused, for API compatibility)
        dexes: List of dex identifiers to query (default: ALL_DEXES)

    Returns:
        List of (position_dict, exchange_name) tuples
    """
    if dexes is None:
        dexes = ALL_DEXES

    all_positions = []

    for dex in dexes:
        positions = fetch_positions_for_dex(address, dex)
        exchange_name = dex if dex else "main"

        for pos in positions:
            all_positions.append((pos, exchange_name))

        if dex != dexes[-1]:  # Don't delay after last dex
            time.sleep(DEX_DELAY)

    return all_positions


def parse_position(
    address: str,
    cohort: str,
    position_data: Dict[str, Any],
    mark_prices: Dict[str, float],
    exchange: str = "main"
) -> Optional[Position]:
    """
    Parse position data from API response into Position object.

    Args:
        address: Wallet address
        cohort: Cohort name
        position_data: Raw position dict from API
        mark_prices: Dict of token -> mark price
        exchange: Exchange identifier ("main", "xyz", "flx")

    Returns:
        Position object or None if parsing fails
    """
    try:
        pos = position_data.get("position", {})

        token = pos.get("coin", "")
        size = float(pos.get("szi", 0))

        # Skip if no position
        if size == 0:
            return None

        # Determine side from size sign
        side = "Long" if size > 0 else "Short"
        size = abs(size)

        # Leverage info
        leverage_info = pos.get("leverage", {})
        leverage = float(leverage_info.get("value", 0))
        leverage_type = leverage_info.get("type", "unknown")

        # Prices
        entry_price = float(pos.get("entryPx", 0))
        mark_price = mark_prices.get(token, 0)

        # Values
        position_value = float(pos.get("positionValue", 0))
        unrealized_pnl = float(pos.get("unrealizedPnl", 0))
        roe = float(pos.get("returnOnEquity", 0))
        margin_used = float(pos.get("marginUsed", 0))

        # Liquidation price - handle null properly
        liq_px = pos.get("liquidationPx")
        liquidation_price = float(liq_px) if liq_px is not None else None

        # Funding
        cum_funding = pos.get("cumFunding", {})
        funding_since_open = float(cum_funding.get("sinceOpen", 0))

        # Determine if isolated - all sub-exchange positions are isolated,
        # plus any position with leverage_type == "isolated"
        is_isolated = (exchange != "main") or (leverage_type == "isolated")

        return Position(
            address=address,
            token=token,
            side=side,
            size=size,
            leverage=leverage,
            leverage_type=leverage_type,
            entry_price=entry_price,
            mark_price=mark_price,
            position_value=position_value,
            unrealized_pnl=unrealized_pnl,
            roe=roe,
            liquidation_price=liquidation_price,
            margin_used=margin_used,
            funding_since_open=funding_since_open,
            cohort=cohort,
            exchange=exchange,
            is_isolated=is_isolated
        )

    except Exception as e:
        logger.debug(f"Error parsing position for {address}: {e}")
        return None


def fetch_all_positions(
    addresses: List[Tuple[str, str]],
    mark_prices: Dict[str, float],
    dexes: List[str] = None,
    progress_callback: callable = None
) -> List[Position]:
    """
    Fetch positions for all addresses across specified exchanges with rate limiting.

    Args:
        addresses: List of (address, cohort) tuples
        mark_prices: Dict of token -> mark price
        dexes: List of dex identifiers to query (default: ALL_DEXES)
        progress_callback: Optional callback(processed, total, positions_found, cohort)
                          called every 50 addresses for progress updates

    Returns:
        List of all Position objects
    """
    all_positions = []
    total = len(addresses)
    current_cohort = None
    sub_exchange_positions = 0

    for i, (address, cohort) in enumerate(addresses):
        # Log cohort transitions and notify via callback
        if cohort != current_cohort:
            current_cohort = cohort
            logger.info(f"Starting cohort: {cohort}")

            # Call callback on cohort transition
            if progress_callback:
                try:
                    progress_callback(i, total, len(all_positions), current_cohort)
                except Exception as e:
                    logger.debug(f"Progress callback error: {e}")

        # Progress logging every 50 addresses
        if (i + 1) % 50 == 0:
            logger.info(f"Progress: {i + 1}/{total} addresses processed "
                       f"({len(all_positions)} positions, {sub_exchange_positions} from sub-exchanges)")
            time.sleep(BATCH_DELAY)

        # Fetch positions from specified exchanges for this address
        positions_with_exchange = fetch_all_positions_for_address(address, mark_prices, dexes)

        for raw_pos, exchange in positions_with_exchange:
            position = parse_position(address, cohort, raw_pos, mark_prices, exchange)
            if position:
                all_positions.append(position)
                if exchange != "main":
                    sub_exchange_positions += 1

        time.sleep(REQUEST_DELAY)

    logger.info(f"Completed: {total} addresses, {len(all_positions)} positions found "
               f"({sub_exchange_positions} from sub-exchanges)")
    return all_positions


# =============================================================================
# ASYNC FUNCTIONS
# =============================================================================

async def async_fetch_positions_for_dex(
    session: aiohttp.ClientSession,
    address: str,
    dex: str = ""
) -> List[Dict[str, Any]]:
    """
    Async version: Fetch positions for a single address from a specific exchange.

    Args:
        session: aiohttp ClientSession
        address: Wallet address
        dex: Exchange identifier ("" for main, "xyz", "flx")

    Returns:
        List of position dicts from API response
    """
    try:
        payload = {
            "type": "clearinghouseState",
            "user": address
        }
        if dex:
            payload["dex"] = dex

        async with session.post(HYPERLIQUID_API, json=payload, timeout=30) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("assetPositions", [])

    except Exception as e:
        dex_name = dex if dex else "main"
        logger.debug(f"Failed to fetch {dex_name} positions for {address}: {e}")
        return []


async def async_fetch_all_positions_for_address(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    address: str,
    dexes: List[str] = None
) -> List[Tuple[Dict[str, Any], str]]:
    """
    Async version: Fetch positions for a single address across specified exchanges.
    All exchanges are fetched concurrently.

    Args:
        session: aiohttp ClientSession
        semaphore: Semaphore for rate limiting
        address: Wallet address
        dexes: List of dex identifiers to query (default: ALL_DEXES)

    Returns:
        List of (position_dict, exchange_name) tuples
    """
    if dexes is None:
        dexes = ALL_DEXES

    async def fetch_dex(dex: str) -> List[Tuple[Dict[str, Any], str]]:
        async with semaphore:
            positions = await async_fetch_positions_for_dex(session, address, dex)
            exchange_name = dex if dex else "main"
            return [(pos, exchange_name) for pos in positions]

    # Fetch all exchanges concurrently
    tasks = [fetch_dex(dex) for dex in dexes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_positions = []
    for result in results:
        if isinstance(result, Exception):
            logger.debug(f"Error fetching positions for {address}: {result}")
        else:
            all_positions.extend(result)

    return all_positions


async def async_fetch_all_mark_prices(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    dexes: List[str] = None
) -> Dict[str, float]:
    """
    Async version: Fetch current mark prices for all perp tokens across all exchanges.

    Args:
        session: aiohttp ClientSession
        semaphore: Semaphore for rate limiting
        dexes: List of exchange identifiers (default: ALL_DEXES)

    Returns:
        Dict mapping token symbol to mark price
    """
    if dexes is None:
        dexes = ALL_DEXES

    async def fetch_dex_prices(dex: str) -> Dict[str, float]:
        async with semaphore:
            try:
                payload = {"type": "allMids"}
                if dex:
                    payload["dex"] = dex

                async with session.post(HYPERLIQUID_API, json=payload, timeout=30) as response:
                    response.raise_for_status()
                    data = await response.json()
                    prices = {token: float(price) for token, price in data.items()}
                    dex_name = dex if dex else "main"
                    logger.debug(f"Fetched {len(prices)} mark prices from {dex_name} exchange")
                    return prices
            except Exception as e:
                dex_name = dex if dex else "main"
                logger.error(f"Failed to fetch mark prices from {dex_name}: {e}")
                return {}

    # Fetch all exchanges concurrently
    tasks = [fetch_dex_prices(dex) for dex in dexes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_prices = {}
    for result in results:
        if isinstance(result, dict):
            all_prices.update(result)

    logger.debug(f"Total mark prices fetched: {len(all_prices)}")
    return all_prices


async def async_fetch_all_positions(
    addresses: List[Tuple[str, str]],
    mark_prices: Dict[str, float],
    dexes: List[str] = None,
    progress_callback: callable = None
) -> List[Position]:
    """
    Async version: Fetch positions for all addresses with concurrent requests.

    Args:
        addresses: List of (address, cohort) tuples
        mark_prices: Dict of token -> mark price
        dexes: List of dex identifiers to query (default: ALL_DEXES)
        progress_callback: Optional callback(processed, total, positions_found, cohort)

    Returns:
        List of all Position objects
    """
    if dexes is None:
        dexes = ALL_DEXES

    all_positions = []
    total = len(addresses)
    sub_exchange_positions = 0

    # Group addresses by cohort for progress tracking
    cohort_indices = {}
    current_cohort = None
    for i, (address, cohort) in enumerate(addresses):
        if cohort != current_cohort:
            cohort_indices[i] = cohort
            current_cohort = cohort

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        # Process in batches to allow progress updates and avoid overwhelming the API
        batch_size = 50
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            batch = addresses[batch_start:batch_end]

            # Check for cohort transitions in this batch and notify
            for i in range(batch_start, batch_end):
                if i in cohort_indices:
                    cohort = cohort_indices[i]
                    logger.info(f"Starting cohort: {cohort}")
                    if progress_callback:
                        try:
                            progress_callback(i, total, len(all_positions), cohort)
                        except Exception as e:
                            logger.debug(f"Progress callback error: {e}")

            # Fetch all addresses in batch concurrently
            async def fetch_address(addr_tuple):
                address, cohort = addr_tuple
                await asyncio.sleep(ASYNC_REQUEST_DELAY)  # Small stagger
                return (
                    await async_fetch_all_positions_for_address(session, semaphore, address, dexes),
                    address,
                    cohort
                )

            tasks = [fetch_address(addr_tuple) for addr_tuple in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for result in results:
                if isinstance(result, Exception):
                    logger.debug(f"Error in batch: {result}")
                    continue

                positions_with_exchange, address, cohort = result
                for raw_pos, exchange in positions_with_exchange:
                    position = parse_position(address, cohort, raw_pos, mark_prices, exchange)
                    if position:
                        all_positions.append(position)
                        if exchange != "main":
                            sub_exchange_positions += 1

            logger.info(f"Progress: {batch_end}/{total} addresses processed "
                       f"({len(all_positions)} positions, {sub_exchange_positions} from sub-exchanges)")

    logger.info(f"Completed: {total} addresses, {len(all_positions)} positions found "
               f"({sub_exchange_positions} from sub-exchanges)")
    return all_positions


def fetch_all_positions_async(
    addresses: List[Tuple[str, str]],
    mark_prices: Dict[str, float],
    dexes: List[str] = None,
    progress_callback: callable = None
) -> List[Position]:
    """
    Sync wrapper for async_fetch_all_positions.
    Use this to call async version from sync code.

    Args:
        addresses: List of (address, cohort) tuples
        mark_prices: Dict of token -> mark price
        dexes: List of dex identifiers to query (default: ALL_DEXES)
        progress_callback: Optional callback(processed, total, positions_found, cohort)

    Returns:
        List of all Position objects
    """
    return asyncio.run(async_fetch_all_positions(
        addresses, mark_prices, dexes, progress_callback
    ))


def fetch_all_mark_prices_async(dexes: List[str] = None) -> Dict[str, float]:
    """
    Sync wrapper for async_fetch_all_mark_prices.
    Fetches mark prices from all exchanges concurrently.

    Args:
        dexes: List of exchange identifiers (default: ALL_DEXES)

    Returns:
        Dict mapping token symbol to mark price
    """
    async def _fetch():
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        async with aiohttp.ClientSession() as session:
            return await async_fetch_all_mark_prices(session, semaphore, dexes)

    return asyncio.run(_fetch())


def save_to_csv(positions: List[Position], filename: str):
    """
    Save positions to CSV file.

    Args:
        positions: List of Position objects
        filename: Output filename
    """
    if not positions:
        logger.warning("No positions to save")
        return

    fieldnames = [
        "Address",
        "Token",
        "Side",
        "Size",
        "Leverage",
        "Leverage Type",
        "Entry Price",
        "Mark Price",
        "Position Value",
        "Unrealized PnL",
        "ROE",
        "Liquidation Price",
        "Margin Used",
        "Funding (Since Open)",
        "Cohort",
        "Exchange",
        "Isolated"
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)

        for pos in positions:
            writer.writerow([
                pos.address,
                pos.token,
                pos.side,
                pos.size,
                pos.leverage,
                pos.leverage_type,
                pos.entry_price,
                pos.mark_price,
                pos.position_value,
                pos.unrealized_pnl,
                pos.roe,
                pos.liquidation_price if pos.liquidation_price is not None else "",  # Empty for null
                pos.margin_used,
                pos.funding_since_open,
                pos.cohort,
                pos.exchange,
                pos.is_isolated
            ])

    logger.info(f"Saved {len(positions)} positions to {filename}")


def run_cohort_scan(
    cohorts: List[str],
    output_file: str,
    mark_prices: Dict[str, float],
    cohort_csv: str,
    dexes: List[str] = None
) -> List[Position]:
    """
    Run position scan for specified cohorts.

    Args:
        cohorts: List of cohort names to scan
        output_file: Output CSV filename
        mark_prices: Dict of token -> mark price
        cohort_csv: Path to cohort data CSV
        dexes: List of dex identifiers to query (default: ALL_DEXES)

    Returns:
        List of Position objects
    """
    addresses = load_cohort_addresses(cohort_csv, cohorts=cohorts)

    if not addresses:
        logger.warning(f"No addresses found for cohorts: {cohorts}")
        return []

    logger.info(f"Fetching positions for {len(addresses)} addresses across {len(dexes or ALL_DEXES)} exchanges...")
    start_time = time.time()
    positions = fetch_all_positions(addresses, mark_prices, dexes)
    elapsed = time.time() - start_time

    save_to_csv(positions, output_file)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Scan Complete: {', '.join(cohorts)}")
    print(f"{'='*60}")
    print(f"Addresses processed: {len(addresses)}")
    print(f"Positions found: {len(positions)}")
    print(f"Time elapsed: {elapsed:.1f} seconds")

    cohort_counts = {}
    exchange_counts = {}
    isolated_count = 0
    for pos in positions:
        cohort_counts[pos.cohort] = cohort_counts.get(pos.cohort, 0) + 1
        exchange_counts[pos.exchange] = exchange_counts.get(pos.exchange, 0) + 1
        if pos.is_isolated:
            isolated_count += 1

    print("\nPositions by cohort:")
    for cohort in cohorts:
        count = cohort_counts.get(cohort, 0)
        print(f"  {cohort}: {count}")

    print("\nPositions by exchange:")
    for exchange in ["main", "xyz", "flx"]:
        count = exchange_counts.get(exchange, 0)
        if count > 0:
            print(f"  {exchange}: {count}")

    with_liq = sum(1 for p in positions if p.liquidation_price is not None)
    print(f"\nPositions with liquidation price: {with_liq}/{len(positions)}")
    print(f"Isolated positions (high priority): {isolated_count}/{len(positions)}")
    print(f"Output saved to: {output_file}")

    return positions


def run_scan_mode(mode: str, cohort_csv: str = "data/raw/cohort_data.csv", output_file: str = None) -> List[Position]:
    """
    Run position scan using a predefined scan mode.

    Args:
        mode: Scan mode name ("high-priority", "normal", "comprehensive")
        cohort_csv: Path to cohort data CSV
        output_file: Output CSV filename (default: data/raw/position_data_{mode}.csv)

    Returns:
        List of Position objects
    """
    if mode not in SCAN_MODES:
        raise ValueError(f"Unknown scan mode: {mode}. Available: {list(SCAN_MODES.keys())}")

    config = SCAN_MODES[mode]
    cohorts = config["cohorts"]
    dexes = config["dexes"]

    if output_file is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        output_file = f"data/raw/position_data_{mode.replace('-', '_')}_{timestamp}.csv"

    # Fetch mark prices
    logger.info("Fetching current mark prices...")
    mark_prices = fetch_all_mark_prices()

    if not mark_prices:
        logger.error("Failed to fetch mark prices. Aborting.")
        return []

    print("\n" + "="*60)
    print(f"SCAN MODE: {mode.upper()}")
    print(f"Cohorts: {', '.join(cohorts)}")
    print(f"Exchanges: {', '.join(d or 'main' for d in dexes)}")
    print("="*60)

    positions = run_cohort_scan(
        cohorts=cohorts,
        output_file=output_file,
        mark_prices=mark_prices,
        cohort_csv=cohort_csv,
        dexes=dexes
    )

    return positions


def main():
    """Main entry point with scan mode argument"""
    import argparse

    parser = argparse.ArgumentParser(description='Scan positions from Hyperliquid')
    parser.add_argument('--mode', '-m', choices=['high-priority', 'normal', 'comprehensive'],
                       default='normal', help='Scan mode (default: normal)')
    parser.add_argument('--cohort-file', default='data/raw/cohort_data.csv',
                       help='Cohort CSV file')
    parser.add_argument('--output', '-o', help='Output CSV file')
    args = parser.parse_args()

    cohort_csv = Path(args.cohort_file)
    if not cohort_csv.exists():
        logger.error(f"{args.cohort_file} not found. Run cohort scraper first.")
        return

    positions = run_scan_mode(args.mode, str(cohort_csv), args.output)

    # Final summary
    print("\n" + "="*60)
    print("SCAN COMPLETE")
    print("="*60)
    print(f"Total positions: {len(positions)}")
    print(f"\nScan modes:")
    print("  high-priority  - kraken + large_whale + rekt, main + xyz only")
    print("  normal         - kraken + large_whale + whale + profit/loss, main + xyz only")
    print("  comprehensive  - all cohorts (including shark), all exchanges")


if __name__ == "__main__":
    main()
