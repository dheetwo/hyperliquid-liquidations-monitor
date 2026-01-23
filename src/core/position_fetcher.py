"""
Position Fetcher

Handles fetching positions from Hyperliquid with:
- Parallel fetching for multiple addresses
- Filtering by notional thresholds
- Mark price integration
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from ..config import Position, config
from ..api.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


class PositionFetcher:
    """
    Fetches positions from Hyperliquid with parallel processing.

    Handles:
    - Fetching positions for multiple addresses concurrently
    - Filtering by notional thresholds
    - Calculating distance to liquidation
    """

    def __init__(
        self,
        client: HyperliquidClient = None,
        exchanges: List[str] = None,
    ):
        self.client = client
        self.exchanges = exchanges or config.exchanges
        self._mark_prices: Dict[str, Dict[str, float]] = {}  # exchange -> token -> price

    async def refresh_mark_prices(self):
        """Fetch current mark prices for all exchanges."""
        if self.client is None:
            self.client = HyperliquidClient()

        for exchange in self.exchanges:
            prices = await self.client.get_mark_prices(exchange)
            self._mark_prices[exchange] = prices
            logger.debug(f"Refreshed {len(prices)} prices for exchange '{exchange or 'main'}'")

    def get_mark_price(self, token: str, exchange: str = "") -> Optional[float]:
        """Get the current mark price for a token."""
        prices = self._mark_prices.get(exchange, {})
        return prices.get(token)

    async def fetch_positions_for_address(
        self,
        address: str,
        exchanges: List[str] = None,
    ) -> List[Position]:
        """
        Fetch all positions for a single address across exchanges.

        Args:
            address: Wallet address
            exchanges: Exchanges to check (default from config)

        Returns:
            List of positions
        """
        if self.client is None:
            self.client = HyperliquidClient()

        exchanges = exchanges or self.exchanges
        all_positions = []

        for exchange in exchanges:
            positions = await self.client.get_positions(address, exchange)

            # Update mark prices from position data
            for p in positions:
                price = self.get_mark_price(p.token, exchange)
                if price:
                    p.mark_price = price

            all_positions.extend(positions)

        return all_positions

    async def fetch_positions_batch(
        self,
        addresses: List[str],
        exchanges: List[str] = None,
        filter_by_threshold: bool = True,
        progress_callback: Callable[[int, int], None] = None,
    ) -> List[Position]:
        """
        Fetch positions for multiple addresses in parallel.

        Args:
            addresses: List of wallet addresses
            exchanges: Exchanges to check
            filter_by_threshold: Whether to filter by notional thresholds
            progress_callback: Optional callback(completed, total)

        Returns:
            List of all positions (optionally filtered)
        """
        if self.client is None:
            self.client = HyperliquidClient()

        exchanges = exchanges or self.exchanges
        all_positions = []
        total = len(addresses)
        completed = 0

        async def fetch_one(addr: str):
            nonlocal completed
            try:
                positions = await self.fetch_positions_for_address(addr, exchanges)
                return positions
            except Exception as e:
                logger.warning(f"Error fetching positions for {addr[:10]}...: {e}")
                return []
            finally:
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

        # Fetch all addresses concurrently (client handles rate limiting)
        results = await asyncio.gather(*[fetch_one(addr) for addr in addresses])

        for positions in results:
            all_positions.extend(positions)

        logger.info(f"Fetched {len(all_positions)} total positions from {len(addresses)} addresses")

        # Filter by threshold if requested
        if filter_by_threshold:
            all_positions = self.filter_by_threshold(all_positions)
            logger.info(f"After threshold filter: {len(all_positions)} positions")

        return all_positions

    def filter_by_threshold(self, positions: List[Position]) -> List[Position]:
        """
        Filter positions by notional thresholds.

        Args:
            positions: List of positions

        Returns:
            Filtered list with only positions above threshold
        """
        result = []

        for p in positions:
            threshold = config.get_notional_threshold(
                p.token,
                p.exchange,
                p.leverage_type == "isolated",
            )

            if p.position_value >= threshold:
                result.append(p)

        return result

    def filter_with_liq_price(self, positions: List[Position]) -> List[Position]:
        """
        Filter to only positions that have a liquidation price.

        Args:
            positions: List of positions

        Returns:
            Filtered list
        """
        return [p for p in positions if p.has_liq_price]


async def fetch_positions_for_wallets(
    addresses: List[str],
    client: HyperliquidClient = None,
    exchanges: List[str] = None,
    filter_by_threshold: bool = True,
    progress_callback: Callable[[int, int], None] = None,
) -> List[Position]:
    """
    Convenience function to fetch positions for a list of wallets.

    Args:
        addresses: List of wallet addresses
        client: Optional HyperliquidClient (creates one if not provided)
        exchanges: Exchanges to check (default from config)
        filter_by_threshold: Whether to filter by notional thresholds
        progress_callback: Optional progress callback

    Returns:
        List of positions
    """
    should_close = client is None
    if client is None:
        client = HyperliquidClient()

    try:
        async with client:
            fetcher = PositionFetcher(client, exchanges)

            # Refresh mark prices first
            await fetcher.refresh_mark_prices()

            # Fetch positions
            positions = await fetcher.fetch_positions_batch(
                addresses,
                exchanges,
                filter_by_threshold,
                progress_callback,
            )

            return positions
    finally:
        if should_close and client._session:
            await client.close()


# =============================================================================
# Testing
# =============================================================================

async def test_fetcher():
    """Quick test of the position fetcher."""
    from ..api.hyperdash import HyperdashClient

    print("Fetching kraken cohort addresses...")
    async with HyperdashClient() as dash_client:
        wallets = await dash_client.get_cohort_addresses("kraken")
        addresses = [w.address for w in wallets[:5]]  # Test with first 5

    print(f"\nFetching positions for {len(addresses)} addresses...")

    def progress(done, total):
        print(f"  Progress: {done}/{total}")

    positions = await fetch_positions_for_wallets(
        addresses,
        filter_by_threshold=False,  # Show all for testing
        progress_callback=progress,
    )

    print(f"\nTotal positions: {len(positions)}")
    for p in positions[:10]:
        dist = p.distance_to_liq()
        dist_str = f"{dist:.2f}%" if dist else "N/A"
        print(f"  {p.token} {p.side} ${p.position_value:,.0f} dist={dist_str}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__file__).rsplit('/src/', 1)[0])
    asyncio.run(test_fetcher())
