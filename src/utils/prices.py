"""
Price Utilities
===============

Consolidated mark price fetching from Hyperliquid API.
Single source of truth for price data across the project.
"""

import asyncio
import logging
from typing import Dict, List

import aiohttp
import requests

logger = logging.getLogger(__name__)

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

# All exchanges to fetch prices from
ALL_DEXES = ["", "xyz", "flx", "vntl", "hyna", "km"]

# Async concurrency settings
MAX_CONCURRENT_REQUESTS = 20


def fetch_all_mark_prices(dexes: List[str] = None) -> Dict[str, float]:
    """
    Fetch current mark prices for all perp tokens across specified exchanges.

    Args:
        dexes: List of exchange identifiers (default: all exchanges)

    Returns:
        Dict mapping token symbol to mark price.
        For sub-exchanges, includes both prefixed (xyz:TSLA) and unprefixed keys.
    """
    if dexes is None:
        dexes = ALL_DEXES

    all_prices = {}

    for dex in dexes:
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

            # Store prices with exchange prefix for sub-exchanges
            for token, price in data.items():
                price_float = float(price)
                if dex:
                    # Sub-exchange tokens: store both prefixed and unprefixed
                    all_prices[f"{dex}:{token}"] = price_float
                all_prices[token] = price_float

            dex_name = dex if dex else "main"
            logger.info(f"Fetched {len(data)} mark prices from {dex_name} exchange")

        except Exception as e:
            dex_name = dex if dex else "main"
            logger.error(f"Failed to fetch mark prices from {dex_name}: {e}")

    logger.info(f"Total mark prices fetched: {len(all_prices)}")
    return all_prices


async def _async_fetch_dex_prices(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    dex: str
) -> Dict[str, float]:
    """
    Async helper: Fetch prices from a single exchange.

    Args:
        session: aiohttp ClientSession
        semaphore: Semaphore for rate limiting
        dex: Exchange identifier ("" for main)

    Returns:
        Dict mapping token to price for this exchange
    """
    async with semaphore:
        try:
            payload = {"type": "allMids"}
            if dex:
                payload["dex"] = dex

            async with session.post(HYPERLIQUID_API, json=payload, timeout=30) as response:
                response.raise_for_status()
                data = await response.json()

                prices = {}
                for token, price in data.items():
                    price_float = float(price)
                    if dex:
                        prices[f"{dex}:{token}"] = price_float
                    prices[token] = price_float

                dex_name = dex if dex else "main"
                logger.info(f"Fetched {len(data)} mark prices from {dex_name} exchange")
                return prices

        except Exception as e:
            dex_name = dex if dex else "main"
            logger.error(f"Failed to fetch mark prices from {dex_name}: {e}")
            return {}


async def _async_fetch_all_mark_prices(dexes: List[str] = None) -> Dict[str, float]:
    """
    Async implementation: Fetch all mark prices concurrently.

    Args:
        dexes: List of exchange identifiers (default: all exchanges)

    Returns:
        Dict mapping token symbol to mark price
    """
    if dexes is None:
        dexes = ALL_DEXES

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        tasks = [_async_fetch_dex_prices(session, semaphore, dex) for dex in dexes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_prices = {}
    for result in results:
        if isinstance(result, dict):
            all_prices.update(result)

    logger.info(f"Total mark prices fetched: {len(all_prices)}")
    return all_prices


def fetch_all_mark_prices_async(dexes: List[str] = None) -> Dict[str, float]:
    """
    Sync wrapper for async mark price fetching.
    Fetches mark prices from all exchanges concurrently for speed.

    Args:
        dexes: List of exchange identifiers (default: all exchanges)

    Returns:
        Dict mapping token symbol to mark price
    """
    return asyncio.run(_async_fetch_all_mark_prices(dexes))


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
