"""
Order Book & Cascade Detection
==============================

L2 order book analysis and liquidation cascade detection for Hyperliquid.

Components:
- OrderBookLevel: Single price level in the order book
- L2Book: L2 order book with bids and asks
- CascadePosition: Position info for cascade detection
- CascadeResult: Result of cascade detection

Functions:
- estimate_price_impact(): Estimate slippage from a liquidation
- detect_cascades(): Detect potential liquidation cascades
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .hyperliquid import HyperliquidAPIClient

logger = logging.getLogger(__name__)


@dataclass
class OrderBookLevel:
    """Single price level in the order book"""
    price: float
    size: float
    num_orders: int


@dataclass
class L2Book:
    """L2 order book with bids and asks"""
    coin: str
    bids: List[OrderBookLevel]  # sorted high to low
    asks: List[OrderBookLevel]  # sorted low to high

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None


@dataclass
class CascadePosition:
    """Position info needed for cascade detection"""
    address: str
    notional_usd: float
    liquidation_price: float
    side: str  # "long" or "short"
    liq_distance_pct: float  # distance from current price to liq price


@dataclass
class CascadeResult:
    """Result of cascade detection"""
    coin: str
    direction: str  # "down" (hunting longs) or "up" (hunting shorts)
    trigger_price: float  # price needed to start cascade
    current_price: float
    distance_to_trigger_pct: float
    total_notional: float  # combined notional of all cascading positions
    num_positions: int
    positions: List[CascadePosition]  # positions in cascade order
    estimated_price_impact_pct: Optional[float] = None

    def __str__(self) -> str:
        impact_str = f"Est. impact: {self.estimated_price_impact_pct:.2%}" if self.estimated_price_impact_pct else ""
        return (
            f"{self.coin} CASCADE ({self.direction}): "
            f"${self.total_notional/1e6:.1f}M across {self.num_positions} positions | "
            f"Trigger: {self.distance_to_trigger_pct:.2%} away | "
            f"{impact_str}"
        )


def estimate_price_impact(
    book: L2Book,
    notional_usd: float,
    side: str,
) -> Optional[Dict[str, float]]:
    """
    Estimate price impact of a liquidation.

    Args:
        book: L2 order book
        notional_usd: Size of liquidation in USD
        side: "sell" for long liquidation, "buy" for short liquidation

    Returns:
        Dict with impact details or None if book exhausted/error:
        - start_price: price before liquidation
        - end_price: estimated fill price
        - impact_pct: percentage price impact
        - levels_consumed: how many book levels were used
    """
    levels = book.bids if side == "sell" else book.asks
    if not levels:
        return None

    start_price = levels[0].price
    remaining_usd = notional_usd
    levels_consumed = 0

    for level in levels:
        level_notional = level.price * level.size

        if remaining_usd <= level_notional:
            # Fully filled at this level
            levels_consumed += 1
            end_price = level.price
            impact_pct = abs(end_price - start_price) / start_price

            return {
                "start_price": start_price,
                "end_price": end_price,
                "impact_pct": impact_pct,
                "levels_consumed": levels_consumed
            }

        remaining_usd -= level_notional
        levels_consumed += 1

    # Book exhausted before fill
    logger.warning(f"Order book exhausted for {book.coin} - ${remaining_usd:,.0f} unfilled")
    return None


def detect_cascades(
    coin: str,
    positions: List[Dict],
    current_price: float,
    get_l2_book_fn,
) -> List[CascadeResult]:
    """
    Detect potential liquidation cascades for an asset.

    Args:
        coin: Asset name
        positions: List of position dicts with keys:
            - address: trader address
            - notional_usd: position size in USD
            - liquidation_price: liq price (required, skip if None)
            - side: "long" or "short"
        current_price: Current mark price
        get_l2_book_fn: Function to fetch L2 book (coin) -> L2Book

    Returns:
        List of CascadeResult for each direction (down for longs, up for shorts)
    """
    # Filter positions with valid liq prices and calculate distance
    valid_positions = []
    for pos in positions:
        liq_price = pos.get("liquidation_price")
        if liq_price is None or liq_price <= 0:
            continue

        side = pos.get("side", "").lower()
        if side not in ("long", "short"):
            continue

        # Calculate distance to liquidation
        if side == "long":
            # Longs liquidate when price drops below liq_price
            liq_distance_pct = (current_price - liq_price) / current_price
        else:
            # Shorts liquidate when price rises above liq_price
            liq_distance_pct = (liq_price - current_price) / current_price

        # Skip if already past liquidation price
        if liq_distance_pct < 0:
            continue

        valid_positions.append(CascadePosition(
            address=pos.get("address", "unknown"),
            notional_usd=pos.get("notional_usd", 0),
            liquidation_price=liq_price,
            side=side,
            liq_distance_pct=liq_distance_pct
        ))

    # Separate by side
    longs = [p for p in valid_positions if p.side == "long"]
    shorts = [p for p in valid_positions if p.side == "short"]

    # Sort by proximity to liquidation (closest first)
    # For longs: highest liq price = closest to current price
    longs.sort(key=lambda p: p.liquidation_price, reverse=True)
    # For shorts: lowest liq price = closest to current price
    shorts.sort(key=lambda p: p.liquidation_price)

    results = []

    # Detect cascade for longs (price moving down)
    if longs:
        cascade = build_cascade(coin, longs, current_price, "down", get_l2_book_fn)
        if cascade:
            results.append(cascade)

    # Detect cascade for shorts (price moving up)
    if shorts:
        cascade = build_cascade(coin, shorts, current_price, "up", get_l2_book_fn)
        if cascade:
            results.append(cascade)

    return results


def build_cascade(
    coin: str,
    sorted_positions: List[CascadePosition],
    current_price: float,
    direction: str,
    get_l2_book_fn,
) -> Optional[CascadeResult]:
    """
    Build a cascade chain from sorted positions.

    Args:
        coin: Asset name
        sorted_positions: Positions sorted by liq proximity (closest first)
        current_price: Current mark price
        direction: "down" for longs, "up" for shorts
        get_l2_book_fn: Function to fetch L2 book (coin) -> L2Book
    """
    if not sorted_positions:
        return None

    book = get_l2_book_fn(coin)
    if not book:
        return None

    cascade_positions = []
    cumulative_notional = 0
    simulated_price = current_price

    for pos in sorted_positions:
        # Check if current simulated price has reached this position's liq
        if direction == "down":
            # Price needs to drop to liq_price
            reached = simulated_price <= pos.liquidation_price
        else:
            # Price needs to rise to liq_price
            reached = simulated_price >= pos.liquidation_price

        # First position always triggers if we push to it
        # Subsequent positions only if cascade reaches them
        if not cascade_positions or reached:
            cascade_positions.append(pos)
            cumulative_notional += pos.notional_usd

            # Estimate impact of this liquidation
            side = "sell" if direction == "down" else "buy"
            impact = estimate_price_impact(book, pos.notional_usd, side)

            if impact:
                # Update simulated price after this liquidation
                simulated_price = impact["end_price"]

    if not cascade_positions:
        return None

    # Calculate trigger distance (distance to first liquidation)
    trigger_price = cascade_positions[0].liquidation_price
    distance_to_trigger = abs(current_price - trigger_price) / current_price

    # Estimate total price impact of full cascade
    total_side = "sell" if direction == "down" else "buy"
    total_impact = estimate_price_impact(book, cumulative_notional, total_side)

    return CascadeResult(
        coin=coin,
        direction=direction,
        trigger_price=trigger_price,
        current_price=current_price,
        distance_to_trigger_pct=distance_to_trigger,
        total_notional=cumulative_notional,
        num_positions=len(cascade_positions),
        positions=cascade_positions,
        estimated_price_impact_pct=total_impact["impact_pct"] if total_impact else None
    )
