"""
Hyperliquid API Client

Single responsibility: communicate with Hyperliquid REST API.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
import aiohttp

from ..config import Position, config

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Async client for Hyperliquid API.

    Handles:
    - Fetching positions for a single address
    - Fetching mark prices
    - Batch fetching with concurrency control
    - Rate limiting and retries
    """

    def __init__(
        self,
        url: str = None,
        max_concurrent: int = None,
        request_delay: float = None,
    ):
        self.url = url or config.hyperliquid_url
        self.max_concurrent = max_concurrent or config.max_concurrent_requests
        self.request_delay = request_delay or config.request_delay_sec
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_session(self):
        """Create session and semaphore if needed."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def close(self):
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(self, payload: dict, retries: int = None) -> Optional[Any]:
        """
        Make a request to the Hyperliquid API with retry logic.

        Args:
            payload: JSON payload to send
            retries: Number of retries (default from config)

        Returns:
            JSON response or None on failure
        """
        await self._ensure_session()
        retries = retries if retries is not None else config.max_retries

        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    async with self._session.post(
                        self.url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    ) as response:
                        if response.status == 429:
                            # Rate limited - back off
                            backoff = config.rate_limit_backoff_sec * (2 ** attempt)
                            logger.warning(f"Rate limited, backing off {backoff}s")
                            await asyncio.sleep(backoff)
                            continue

                        if response.status != 200:
                            logger.error(f"API error {response.status}: {await response.text()}")
                            return None

                        result = await response.json()

                    # Delay AFTER request completes but still inside semaphore
                    # This ensures we don't exceed ~10 req/sec (10 concurrent * 0.1s delay)
                    await asyncio.sleep(self.request_delay)

                    return result

            except aiohttp.ClientError as e:
                logger.error(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < retries:
                    await asyncio.sleep(config.rate_limit_backoff_sec)
                continue

        return None

    # -------------------------------------------------------------------------
    # Public Methods
    # -------------------------------------------------------------------------

    async def get_mark_prices(self, exchange: str = "") -> Dict[str, float]:
        """
        Get current mark prices for all tokens on an exchange.

        Args:
            exchange: Exchange identifier ("" for main, "xyz" for TradeXYZ)

        Returns:
            Dict mapping token symbol to mark price
        """
        payload = {"type": "allMids"}
        if exchange:
            payload["dex"] = exchange

        response = await self._request(payload)
        if not response:
            return {}

        # Response is a dict: {"BTC": "95000.5", "ETH": "3500.2", ...}
        prices = {}
        for token, price_str in response.items():
            try:
                prices[token] = float(price_str)
            except (ValueError, TypeError):
                continue

        return prices

    async def get_positions(self, address: str, exchange: str = "") -> List[Position]:
        """
        Get all positions for a single address on an exchange.

        Args:
            address: Wallet address (0x...)
            exchange: Exchange identifier ("" for main, "xyz" for TradeXYZ)

        Returns:
            List of Position objects
        """
        payload = {"type": "clearinghouseState", "user": address}
        if exchange:
            payload["dex"] = exchange

        response = await self._request(payload)
        if not response:
            return []

        return self._parse_positions(response, address, exchange)

    async def get_positions_batch(
        self,
        addresses: List[str],
        exchange: str = "",
        progress_callback: callable = None,
    ) -> Dict[str, List[Position]]:
        """
        Fetch positions for multiple addresses concurrently.

        Args:
            addresses: List of wallet addresses
            exchange: Exchange identifier
            progress_callback: Optional callback(completed, total) for progress

        Returns:
            Dict mapping address to list of positions
        """
        await self._ensure_session()

        results: Dict[str, List[Position]] = {}
        total = len(addresses)
        completed = 0

        async def fetch_one(addr: str):
            nonlocal completed
            positions = await self.get_positions(addr, exchange)
            results[addr] = positions
            completed += 1
            if progress_callback:
                progress_callback(completed, total)
            # Small delay to avoid hammering
            await asyncio.sleep(self.request_delay)

        # Run all fetches concurrently (semaphore limits actual concurrency)
        tasks = [fetch_one(addr) for addr in addresses]
        await asyncio.gather(*tasks, return_exceptions=True)

        return results

    async def get_positions_all_exchanges(
        self,
        address: str,
        exchanges: List[str] = None,
    ) -> List[Position]:
        """
        Get positions for an address across multiple exchanges.

        Args:
            address: Wallet address
            exchanges: List of exchanges (default from config)

        Returns:
            Combined list of positions from all exchanges
        """
        exchanges = exchanges or config.exchanges
        all_positions = []

        for exchange in exchanges:
            positions = await self.get_positions(address, exchange)
            all_positions.extend(positions)
            if len(exchanges) > 1:
                await asyncio.sleep(self.request_delay)

        return all_positions

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    def _parse_positions(
        self,
        response: dict,
        address: str,
        exchange: str,
    ) -> List[Position]:
        """Parse clearinghouseState response into Position objects."""
        positions = []

        asset_positions = response.get("assetPositions", [])
        for item in asset_positions:
            pos_data = item.get("position", {})
            if not pos_data:
                continue

            # Extract core position data
            try:
                coin = pos_data.get("coin", "")
                size = float(pos_data.get("szi", 0))
                if size == 0:
                    continue

                side = "long" if size > 0 else "short"
                size = abs(size)

                entry_price = float(pos_data.get("entryPx", 0))
                position_value = float(pos_data.get("positionValue", 0))
                unrealized_pnl = float(pos_data.get("unrealizedPnl", 0))
                margin_used = float(pos_data.get("marginUsed", 0))

                # Liquidation price may be missing or None
                liq_px = pos_data.get("liquidationPx")
                liquidation_price = float(liq_px) if liq_px else None

                # Leverage info
                leverage_info = pos_data.get("leverage", {})
                if isinstance(leverage_info, dict):
                    leverage = float(leverage_info.get("value", 1.0))
                    leverage_type = leverage_info.get("type", "cross")
                else:
                    leverage = float(leverage_info) if leverage_info else 1.0
                    leverage_type = "cross"

                # Mark price - estimate from position value and size
                if size > 0:
                    mark_price = position_value / size
                else:
                    mark_price = entry_price

                position = Position(
                    address=address,
                    token=coin,
                    exchange=exchange,
                    side=side,
                    size=size,
                    entry_price=entry_price,
                    mark_price=mark_price,
                    liquidation_price=liquidation_price,
                    position_value=position_value,
                    unrealized_pnl=unrealized_pnl,
                    leverage=leverage,
                    leverage_type=leverage_type,
                    margin_used=margin_used,
                )
                positions.append(position)

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Error parsing position for {address}: {e}")
                continue

        return positions


# =============================================================================
# Convenience function for quick testing
# =============================================================================

async def test_client():
    """Quick test of the API client."""
    async with HyperliquidClient() as client:
        # Test mark prices
        print("Fetching main exchange mark prices...")
        prices = await client.get_mark_prices("")
        print(f"Got {len(prices)} prices")
        if prices:
            print(f"BTC: ${prices.get('BTC', 'N/A')}")
            print(f"ETH: ${prices.get('ETH', 'N/A')}")

        # Test xyz exchange
        print("\nFetching xyz exchange mark prices...")
        xyz_prices = await client.get_mark_prices("xyz")
        print(f"Got {len(xyz_prices)} xyz prices")

        # Test position fetch for a known address (from kraken cohort)
        test_address = "0x123"  # Replace with actual address for real test
        print(f"\nFetching positions for {test_address}...")
        positions = await client.get_positions_all_exchanges(test_address)
        print(f"Got {len(positions)} positions")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_client())
