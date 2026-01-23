#!/usr/bin/env python3
"""
Quick test script for API clients.

Run with: python scripts/test_apis.py
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hyperliquid import HyperliquidClient
from src.api.hyperdash import HyperdashClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def test_hyperliquid():
    """Test Hyperliquid API client."""
    print("\n" + "=" * 60)
    print("TESTING HYPERLIQUID API")
    print("=" * 60)

    async with HyperliquidClient() as client:
        # Test 1: Get mark prices for main exchange
        print("\n1. Fetching main exchange mark prices...")
        prices = await client.get_mark_prices("")
        print(f"   Got {len(prices)} prices")
        if prices:
            btc = prices.get("BTC")
            eth = prices.get("ETH")
            print(f"   BTC: ${btc:,.2f}" if btc else "   BTC: N/A")
            print(f"   ETH: ${eth:,.2f}" if eth else "   ETH: N/A")

        # Test 2: Get mark prices for xyz exchange
        print("\n2. Fetching xyz exchange mark prices...")
        xyz_prices = await client.get_mark_prices("xyz")
        print(f"   Got {len(xyz_prices)} prices")
        if xyz_prices:
            # Show a few examples
            for token in list(xyz_prices.keys())[:5]:
                print(f"   {token}: ${xyz_prices[token]:,.2f}")

        return prices, xyz_prices


async def test_hyperdash():
    """Test Hyperdash API client."""
    print("\n" + "=" * 60)
    print("TESTING HYPERDASH API")
    print("=" * 60)

    async with HyperdashClient() as client:
        # Test: Fetch kraken cohort (highest value accounts)
        print("\n1. Fetching kraken cohort...")
        wallets = await client.get_cohort_addresses("kraken")
        print(f"   Got {len(wallets)} kraken wallets")

        if wallets:
            print("\n   Top 5 by account value:")
            for w in sorted(wallets, key=lambda x: x.account_value, reverse=True)[:5]:
                print(f"   - {w.address[:10]}... ${w.account_value:,.0f} "
                      f"leverage={w.leverage:.1f}x {w.bias}")

        return wallets


async def test_positions(wallets):
    """Test fetching positions for a real wallet."""
    if not wallets:
        print("\nSkipping position test (no wallets)")
        return

    print("\n" + "=" * 60)
    print("TESTING POSITION FETCHING")
    print("=" * 60)

    # Pick the highest value wallet
    wallet = max(wallets, key=lambda x: x.account_value)
    print(f"\n1. Fetching positions for {wallet.address[:10]}...")
    print(f"   (account value: ${wallet.account_value:,.0f})")

    async with HyperliquidClient() as client:
        positions = await client.get_positions_all_exchanges(wallet.address)
        print(f"   Got {len(positions)} positions")

        if positions:
            print("\n   Positions:")
            for p in positions:
                liq_dist = p.distance_to_liq()
                liq_str = f"{liq_dist:.2f}%" if liq_dist else "N/A"
                exch = p.exchange or "main"
                print(f"   - {p.token} {p.side} ${p.position_value:,.0f} "
                      f"lev={p.leverage:.1f}x dist={liq_str} ({exch})")

        return positions


async def main():
    """Run all tests."""
    try:
        # Test Hyperliquid
        prices, xyz_prices = await test_hyperliquid()

        # Test Hyperdash
        wallets = await test_hyperdash()

        # Test position fetching
        positions = await test_positions(wallets)

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)

    except Exception as e:
        logger.exception(f"Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
