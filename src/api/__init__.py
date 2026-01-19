"""
API Package
===========

External API clients for Hyperliquid and market data.

Components:
- hyperliquid.py: HyperliquidAPIClient, RateLimiter, MarketCapClient
- orderbook.py: L2Book, order book analysis, cascade detection
"""

from .hyperliquid import (
    HyperliquidAPIClient,
    HyperliquidAsset,
    RateLimiter,
    MarketCapClient,
    COINGECKO_ID_MAP,
    get_hyperliquid_oi,
)
from .orderbook import (
    OrderBookLevel,
    L2Book,
    CascadePosition,
    CascadeResult,
    estimate_price_impact,
    detect_cascades,
    build_cascade,
)

__all__ = [
    # Hyperliquid API
    "HyperliquidAPIClient",
    "HyperliquidAsset",
    "RateLimiter",
    "MarketCapClient",
    "COINGECKO_ID_MAP",
    "get_hyperliquid_oi",
    # Order Book
    "OrderBookLevel",
    "L2Book",
    "CascadePosition",
    "CascadeResult",
    "estimate_price_impact",
    "detect_cascades",
    "build_cascade",
]
