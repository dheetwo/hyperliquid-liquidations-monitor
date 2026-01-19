"""
Hyperliquid API Client
=======================

Direct API integration with Hyperliquid for:
- Asset metadata and market context
- Open Interest data
- Trader position data (if addresses are known)
- Market cap data (via CoinGecko)

API Endpoint: https://api.hyperliquid.xyz/info
"""

import requests
import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

# Import order book types and functions from orderbook module
from .orderbook import (
    OrderBookLevel,
    L2Book,
    CascadePosition,
    CascadeResult,
    estimate_price_impact,
    detect_cascades,
    build_cascade,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter with exponential backoff for failed requests."""

    def __init__(self, min_interval: float = 0.5, max_retries: int = 3):
        """
        Args:
            min_interval: Minimum seconds between requests
            max_retries: Maximum retry attempts on rate limit errors
        """
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request_time = 0.0

    def wait(self):
        """Wait if needed to respect rate limit."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def execute_with_retry(self, func, *args, **kwargs):
        """Execute a function with exponential backoff on rate limit errors."""
        last_exception = None
        for attempt in range(self.max_retries):
            self.wait()
            try:
                return func(*args, **kwargs)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    wait_time = (2 ** attempt) * 2  # 2, 4, 8 seconds
                    logger.warning(f"Rate limited, waiting {wait_time}s (attempt {attempt + 1}/{self.max_retries})")
                    time.sleep(wait_time)
                    last_exception = e
                else:
                    raise
            except requests.exceptions.RequestException as e:
                # Network errors - retry with backoff
                wait_time = (2 ** attempt)
                logger.warning(f"Request failed, retrying in {wait_time}s: {e}")
                time.sleep(wait_time)
                last_exception = e

        raise last_exception or Exception("Max retries exceeded")


# Common mappings from Hyperliquid symbols to CoinGecko IDs
COINGECKO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "UNI": "uniswap",
    "ATOM": "cosmos",
    "LTC": "litecoin",
    "ETC": "ethereum-classic",
    "XLM": "stellar",
    "ALGO": "algorand",
    "NEAR": "near",
    "FTM": "fantom",
    "AAVE": "aave",
    "ARB": "arbitrum",
    "OP": "optimism",
    "APT": "aptos",
    "SUI": "sui",
    "INJ": "injective-protocol",
    "SEI": "sei-network",
    "TIA": "celestia",
    "HYPE": "hyperliquid",
    "PEPE": "pepe",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "SHIB": "shiba-inu",
    "FLOKI": "floki",
    "ORDI": "ordi",
    "STX": "stacks",
    "IMX": "immutable-x",
    "RENDER": "render-token",
    "FET": "fetch-ai",
    "GRT": "the-graph",
    "MKR": "maker",
    "SNX": "havven",
    "CRV": "curve-dao-token",
    "LDO": "lido-dao",
    "RUNE": "thorchain",
    "SAND": "the-sandbox",
    "MANA": "decentraland",
    "AXS": "axie-infinity",
    "ENS": "ethereum-name-service",
    "BLUR": "blur",
    "ZEC": "zcash",
    "JTO": "jito-governance-token",
    "JUP": "jupiter-exchange-solana",
    "PYTH": "pyth-network",
    "W": "wormhole",
    "ENA": "ethena",
    "PENDLE": "pendle",
    "WLD": "worldcoin-wld",
    "STRK": "starknet",
    "ZRO": "layerzero",
    "EIGEN": "eigenlayer",
    "PAXG": "pax-gold",
    "FARTCOIN": "fartcoin",
    "PUMP": "pump-fun",
}


@dataclass
class HyperliquidAsset:
    """Asset information from Hyperliquid"""
    name: str
    asset_index: int
    mark_price: float
    open_interest: float  # USD notional
    funding_rate: float
    volume_24h: float
    max_leverage: int


class HyperliquidAPIClient:
    """Client for Hyperliquid's public info API"""

    BASE_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })
        self._asset_cache: Optional[Dict[str, HyperliquidAsset]] = None
        self._rate_limiter = RateLimiter(min_interval=0.2, max_retries=3)
    
    def _post(self, payload: Dict[str, Any]) -> Any:
        """Make a POST request to the info endpoint with rate limiting."""
        def do_request():
            response = self.session.post(
                self.BASE_URL,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()

        try:
            return self._rate_limiter.execute_with_retry(do_request)
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
    
    def get_meta_and_asset_contexts(self) -> Dict[str, HyperliquidAsset]:
        """
        Fetch all perpetual assets with their market context.
        
        Returns dict mapping asset name -> HyperliquidAsset
        """
        logger.info("Fetching asset metadata and contexts from Hyperliquid...")
        
        data = self._post({"type": "metaAndAssetCtxs"})
        
        # Response is [universe_meta, [asset_contexts]]
        # universe_meta contains asset names and config
        # asset_contexts contains mark price, OI, funding, etc.
        
        universe = data[0]["universe"]  # List of asset configs
        contexts = data[1]  # List of asset contexts
        
        assets = {}
        for i, (asset_meta, ctx) in enumerate(zip(universe, contexts)):
            name = asset_meta["name"]
            
            # Parse numeric values from string responses
            mark_price = float(ctx.get("markPx", 0))

            # Open Interest is provided in coins/contracts, convert to USD
            open_interest_coins = float(ctx.get("openInterest", 0))
            open_interest = open_interest_coins * mark_price
            
            # Funding rate
            funding = float(ctx.get("funding", 0))
            
            # 24h volume
            volume = float(ctx.get("dayNtlVlm", 0))
            
            # Max leverage from universe meta
            max_leverage = asset_meta.get("maxLeverage", 50)
            
            assets[name] = HyperliquidAsset(
                name=name,
                asset_index=i,
                mark_price=mark_price,
                open_interest=open_interest,
                funding_rate=funding,
                volume_24h=volume,
                max_leverage=max_leverage
            )
        
        logger.info(f"Loaded {len(assets)} perpetual assets")
        self._asset_cache = assets
        return assets
    
    def get_user_state(self, address: str) -> Optional[Dict]:
        """
        Get a user's account state including all positions.
        
        Args:
            address: Ethereum address (0x...)
            
        Returns:
            User's clearinghouse state or None if not found
        """
        logger.debug(f"Fetching state for {address[:10]}...")
        
        try:
            data = self._post({
                "type": "clearinghouseState",
                "user": address
            })
            return data
        except Exception as e:
            logger.warning(f"Failed to get state for {address}: {e}")
            return None
    
    def get_user_positions(self, address: str) -> List[Dict]:
        """
        Get a user's open perpetual positions.
        
        Returns list of position dicts with:
        - coin: asset name
        - szi: position size (negative = short)
        - positionValue: notional USD
        - entryPx: entry price
        - unrealizedPnl: current PnL
        - leverage: effective leverage
        """
        state = self.get_user_state(address)
        if not state:
            return []
        
        positions = []
        for asset_pos in state.get("assetPositions", []):
            pos = asset_pos.get("position", {})
            if pos:
                positions.append({
                    "coin": pos.get("coin"),
                    "size": float(pos.get("szi", 0)),
                    "notional_usd": abs(float(pos.get("positionValue", 0))),
                    "entry_price": float(pos.get("entryPx", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": pos.get("leverage", {}).get("value", 0),
                    "liquidation_price": float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else None
                })
        
        return positions
    
    def get_asset_open_interest(self, asset: str) -> Optional[float]:
        """Get Open Interest for a specific asset in USD"""
        if self._asset_cache is None:
            self.get_meta_and_asset_contexts()
        
        if asset in self._asset_cache:
            return self._asset_cache[asset].open_interest
        return None
    
    def get_all_assets(self) -> List[str]:
        """Get list of all tradeable perpetual asset names"""
        if self._asset_cache is None:
            self.get_meta_and_asset_contexts()
        return list(self._asset_cache.keys())

    def get_l2_book(self, coin: str) -> Optional[L2Book]:
        """
        Fetch L2 order book for an asset.

        Args:
            coin: Asset name (e.g., "BTC", "ETH")

        Returns:
            L2Book with bids and asks, or None on error
        """
        logger.debug(f"Fetching L2 book for {coin}...")

        try:
            data = self._post({"type": "l2Book", "coin": coin})

            levels = data.get("levels", [[], []])

            bids = [
                OrderBookLevel(
                    price=float(level["px"]),
                    size=float(level["sz"]),
                    num_orders=int(level["n"])
                )
                for level in levels[0]
            ]

            asks = [
                OrderBookLevel(
                    price=float(level["px"]),
                    size=float(level["sz"]),
                    num_orders=int(level["n"])
                )
                for level in levels[1]
            ]

            return L2Book(coin=coin, bids=bids, asks=asks)

        except Exception as e:
            logger.warning(f"Failed to get L2 book for {coin}: {e}")
            return None

    def estimate_price_impact(
        self,
        coin: str,
        notional_usd: float,
        side: str,
        book: Optional[L2Book] = None
    ) -> Optional[Dict[str, float]]:
        """
        Estimate price impact of a liquidation.

        Delegates to standalone function in orderbook module.

        Args:
            coin: Asset name
            notional_usd: Size of liquidation in USD
            side: "sell" for long liquidation, "buy" for short liquidation
            book: Optional pre-fetched L2Book, will fetch if not provided

        Returns:
            Dict with impact details or None if book exhausted/error:
            - start_price: price before liquidation
            - end_price: estimated fill price
            - impact_pct: percentage price impact
            - levels_consumed: how many book levels were used
        """
        if book is None:
            book = self.get_l2_book(coin)

        if book is None:
            return None

        # Delegate to standalone function from orderbook module
        return estimate_price_impact(book, notional_usd, side)

    def detect_cascades(
        self,
        coin: str,
        positions: List[Dict],
        current_price: Optional[float] = None
    ) -> List[CascadeResult]:
        """
        Detect potential liquidation cascades for an asset.

        Delegates to standalone function in orderbook module.

        Args:
            coin: Asset name
            positions: List of position dicts with keys:
                - address: trader address
                - notional_usd: position size in USD
                - liquidation_price: liq price (required, skip if None)
                - side: "long" or "short"
            current_price: Current mark price (fetched if not provided)

        Returns:
            List of CascadeResult for each direction (down for longs, up for shorts)
        """
        if current_price is None:
            if self._asset_cache is None:
                self.get_meta_and_asset_contexts()
            if coin in self._asset_cache:
                current_price = self._asset_cache[coin].mark_price
            else:
                logger.warning(f"Could not get current price for {coin}")
                return []

        # Delegate to standalone function from orderbook module
        return detect_cascades(coin, positions, current_price, self.get_l2_book)


class MarketCapClient:
    """
    Fetches market cap data from CoinGecko API.

    Free tier limits: 10-30 calls/minute, no API key required.
    """

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self._cache: Dict[str, float] = {}
        self._cache_time: float = 0
        self._cache_ttl: float = 300  # 5 minute cache
        # CoinGecko free tier: 10-30 calls/min, use 3s interval to be safe
        self._rate_limiter = RateLimiter(min_interval=3.0, max_retries=3)

    def _get_coingecko_id(self, symbol: str) -> Optional[str]:
        """Map Hyperliquid symbol to CoinGecko ID"""
        return COINGECKO_ID_MAP.get(symbol.upper())

    def get_market_caps(self, symbols: List[str]) -> Dict[str, float]:
        """
        Fetch market caps for multiple symbols in one API call.

        Args:
            symbols: List of Hyperliquid asset symbols (e.g., ["BTC", "ETH"])

        Returns:
            Dict mapping symbol -> market cap in USD
        """
        # Check cache
        if time.time() - self._cache_time < self._cache_ttl:
            # Return cached values for requested symbols
            result = {s: self._cache.get(s, 0) for s in symbols if s in self._cache}
            if len(result) == len(symbols):
                return result

        # Map symbols to CoinGecko IDs
        id_to_symbol = {}
        for symbol in symbols:
            cg_id = self._get_coingecko_id(symbol)
            if cg_id:
                id_to_symbol[cg_id] = symbol

        if not id_to_symbol:
            logger.warning(f"No CoinGecko IDs found for symbols: {symbols}")
            return {}

        # Fetch from CoinGecko (batch up to 250 IDs)
        ids_str = ",".join(id_to_symbol.keys())

        def do_request():
            response = self.session.get(
                f"{self.BASE_URL}/simple/price",
                params={
                    "ids": ids_str,
                    "vs_currencies": "usd",
                    "include_market_cap": "true"
                },
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()

        try:
            data = self._rate_limiter.execute_with_retry(do_request)

            result = {}
            for cg_id, symbol in id_to_symbol.items():
                if cg_id in data and "usd_market_cap" in data[cg_id]:
                    mc = data[cg_id]["usd_market_cap"]
                    result[symbol] = mc
                    self._cache[symbol] = mc

            self._cache_time = time.time()
            logger.info(f"Fetched market caps for {len(result)} assets")
            return result

        except Exception as e:
            logger.error(f"Error fetching market caps: {e}")
            return {}

    def get_market_cap(self, symbol: str) -> Optional[float]:
        """Get market cap for a single symbol"""
        result = self.get_market_caps([symbol])
        return result.get(symbol)


# Convenience function for quick OI lookup
def get_hyperliquid_oi() -> Dict[str, float]:
    """
    Quick helper to get all asset Open Interest values.
    
    Returns: Dict mapping asset name -> OI in USD
    """
    client = HyperliquidAPIClient()
    assets = client.get_meta_and_asset_contexts()
    return {name: asset.open_interest for name, asset in assets.items()}


if __name__ == "__main__":
    # Test the API client
    logging.basicConfig(level=logging.INFO)

    client = HyperliquidAPIClient()
    assets = client.get_meta_and_asset_contexts()

    print("\n=== Top 10 Assets by Open Interest ===")
    sorted_assets = sorted(assets.values(), key=lambda x: x.open_interest, reverse=True)
    for asset in sorted_assets[:10]:
        print(f"{asset.name}: ${asset.open_interest:,.0f} OI | "
              f"${asset.mark_price:,.2f} mark | "
              f"{asset.funding_rate:.4%} funding")

    # Test L2 book
    print("\n=== BTC Order Book (Top 5 levels) ===")
    book = client.get_l2_book("BTC")
    if book:
        print(f"Mid price: ${book.mid_price:,.2f}")
        print("Bids:")
        for level in book.bids[:5]:
            print(f"  ${level.price:,.2f} | {level.size:.4f} BTC | {level.num_orders} orders")
        print("Asks:")
        for level in book.asks[:5]:
            print(f"  ${level.price:,.2f} | {level.size:.4f} BTC | {level.num_orders} orders")

        # Test price impact estimation
        print("\n=== Price Impact Estimates ===")
        for size in [1_000_000, 5_000_000, 10_000_000]:
            impact = client.estimate_price_impact("BTC", size, "sell", book)
            if impact:
                print(f"${size/1e6:.0f}M sell: {impact['impact_pct']:.4%} impact "
                      f"(${impact['start_price']:,.0f} -> ${impact['end_price']:,.0f}, "
                      f"{impact['levels_consumed']} levels)")

        # Test cascade detection with mock positions
        print("\n=== Cascade Detection (Mock Data) ===")
        btc_price = book.mid_price
        mock_positions = [
            # Longs with liq prices below current
            {"address": "0xAAA", "notional_usd": 5_000_000, "liquidation_price": btc_price * 0.97, "side": "long"},
            {"address": "0xBBB", "notional_usd": 8_000_000, "liquidation_price": btc_price * 0.95, "side": "long"},
            {"address": "0xCCC", "notional_usd": 3_000_000, "liquidation_price": btc_price * 0.93, "side": "long"},
            # Shorts with liq prices above current
            {"address": "0xDDD", "notional_usd": 4_000_000, "liquidation_price": btc_price * 1.04, "side": "short"},
            {"address": "0xEEE", "notional_usd": 6_000_000, "liquidation_price": btc_price * 1.06, "side": "short"},
        ]

        cascades = client.detect_cascades("BTC", mock_positions, btc_price)
        for cascade in cascades:
            print(f"\n{cascade.direction.upper()} cascade:")
            print(f"  Trigger: ${cascade.trigger_price:,.0f} ({cascade.distance_to_trigger_pct:.2%} from spot)")
            print(f"  Total notional: ${cascade.total_notional/1e6:.1f}M across {cascade.num_positions} positions")
            if cascade.estimated_price_impact_pct:
                print(f"  Est. price impact: {cascade.estimated_price_impact_pct:.4%}")
            print("  Positions in cascade:")
            for pos in cascade.positions:
                print(f"    - {pos.address}: ${pos.notional_usd/1e6:.1f}M @ liq ${pos.liquidation_price:,.0f}")
