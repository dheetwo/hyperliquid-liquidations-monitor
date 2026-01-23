"""
Position Filter
================

Logic for identifying large positions based on configurable thresholds.
"""

import logging
from typing import List, Dict, Optional
from dataclasses import dataclass

from models import Position, PositionSide, FlaggedPosition, AlertReason
from config import (
    MAJOR_ASSET_THRESHOLD,
    DEFAULT_NOTIONAL_THRESHOLD,
    OI_PERCENTAGE_THRESHOLD,
    MAJOR_ASSETS
)

logger = logging.getLogger(__name__)


@dataclass
class FilterThresholds:
    """Configurable thresholds for position filtering"""
    
    # n_1: Threshold for major assets (BTC, ETH, SOL) in USD
    major_asset_threshold: float = MAJOR_ASSET_THRESHOLD
    
    # n_2: Threshold for all other assets in USD  
    default_threshold: float = DEFAULT_NOTIONAL_THRESHOLD
    
    # x: Percentage of Open Interest threshold (as decimal)
    oi_percentage: float = OI_PERCENTAGE_THRESHOLD
    
    # List of assets considered "major"
    major_assets: List[str] = None
    
    # If True, major assets ONLY use n_1 threshold (ignore n_2)
    # If False, major assets use both n_1 AND n_2 (more sensitive)
    major_assets_exclusive: bool = True
    
    def __post_init__(self):
        if self.major_assets is None:
            self.major_assets = MAJOR_ASSETS.copy()
    
    def get_threshold_for_asset(self, asset: str) -> float:
        """Get the notional threshold for a specific asset"""
        if asset.upper() in [a.upper() for a in self.major_assets]:
            return self.major_asset_threshold
        return self.default_threshold


class PositionFilter:
    """
    Filters positions based on configurable thresholds.
    
    A position is flagged if it meets ANY of:
    1. Major asset (BTC/ETH/SOL) and notional >= major_asset_threshold (n_1)
    2. Any asset and notional >= default_threshold (n_2)  
    3. Any asset and notional >= oi_percentage * asset's Open Interest (x%)
    """
    
    def __init__(self, thresholds: Optional[FilterThresholds] = None):
        self.thresholds = thresholds or FilterThresholds()
        self._oi_cache: Dict[str, float] = {}
    
    def set_open_interest(self, asset: str, oi: float):
        """Cache Open Interest value for an asset"""
        self._oi_cache[asset.upper()] = oi
    
    def bulk_set_open_interest(self, oi_data: Dict[str, float]):
        """Cache OI for multiple assets"""
        for asset, oi in oi_data.items():
            self._oi_cache[asset.upper()] = oi
    
    def get_open_interest(self, asset: str) -> Optional[float]:
        """Get cached OI for an asset"""
        return self._oi_cache.get(asset.upper())
    
    def evaluate_position(self, position: Position) -> Optional[FlaggedPosition]:
        """
        Evaluate a single position against all thresholds.
        
        Returns FlaggedPosition if any threshold is met, None otherwise.
        """
        alerts = []
        asset = position.asset.upper()
        notional = position.notional_usd
        is_major = asset in [a.upper() for a in self.thresholds.major_assets]
        
        # Check condition 1: Major asset threshold (n_1)
        if is_major:
            if notional >= self.thresholds.major_asset_threshold:
                alerts.append(AlertReason.MAJOR_ASSET_THRESHOLD)
                logger.debug(
                    f"Position flagged (major asset): {asset} ${notional:,.0f} >= "
                    f"threshold ${self.thresholds.major_asset_threshold:,.0f}"
                )
        
        # Check condition 2: Default notional threshold (n_2)
        # Skip for major assets if major_assets_exclusive is True
        if not (is_major and self.thresholds.major_assets_exclusive):
            if notional >= self.thresholds.default_threshold:
                alerts.append(AlertReason.NOTIONAL_THRESHOLD)
                logger.debug(
                    f"Position flagged (notional): {asset} ${notional:,.0f} >= "
                    f"threshold ${self.thresholds.default_threshold:,.0f}"
                )
        
        # Check condition 3: OI percentage threshold (x%)
        oi = self.get_open_interest(asset) or position.asset_open_interest
        if oi and oi > 0:
            oi_pct = notional / oi
            if oi_pct >= self.thresholds.oi_percentage:
                alerts.append(AlertReason.OI_PERCENTAGE_THRESHOLD)
                logger.debug(
                    f"Position flagged (OI %): {asset} {oi_pct:.2%} >= "
                    f"threshold {self.thresholds.oi_percentage:.2%}"
                )
        
        if alerts:
            # Remove duplicates while preserving order
            unique_alerts = list(dict.fromkeys(alerts))
            return FlaggedPosition(position=position, alert_reasons=unique_alerts)
        
        return None
    
    def filter_positions(self, positions: List[Position]) -> List[FlaggedPosition]:
        """
        Filter a list of positions, returning only those that meet thresholds.
        
        Args:
            positions: List of positions to evaluate
            
        Returns:
            List of FlaggedPosition objects for positions meeting criteria
        """
        flagged = []
        
        for position in positions:
            result = self.evaluate_position(position)
            if result:
                flagged.append(result)
        
        logger.info(f"Filtered {len(positions)} positions -> {len(flagged)} flagged")
        return flagged
    
    def summary(self) -> str:
        """Get a summary of current filter settings"""
        return (
            f"Position Filter Settings:\n"
            f"  Major Assets: {', '.join(self.thresholds.major_assets)}\n"
            f"  Major Asset Threshold (n_1): ${self.thresholds.major_asset_threshold:,.0f}\n"
            f"  Default Threshold (n_2): ${self.thresholds.default_threshold:,.0f}\n"
            f"  OI Percentage Threshold (x): {self.thresholds.oi_percentage:.2%}\n"
            f"  OI Data Cached: {len(self._oi_cache)} assets"
        )


def create_filter_from_params(
    major_threshold: float = None,
    default_threshold: float = None,
    oi_percentage: float = None,
    major_assets: List[str] = None
) -> PositionFilter:
    """
    Convenience function to create a filter with custom parameters.

    Args:
        major_threshold: n_1 - threshold for BTC/ETH/SOL in USD
        default_threshold: n_2 - threshold for other assets in USD
        oi_percentage: x - OI percentage threshold (0.05 = 5%)
        major_assets: List of assets to use n_1 for

    Returns:
        Configured PositionFilter instance
    """
    thresholds = FilterThresholds(
        major_asset_threshold=major_threshold or MAJOR_ASSET_THRESHOLD,
        default_threshold=default_threshold or DEFAULT_NOTIONAL_THRESHOLD,
        oi_percentage=oi_percentage or OI_PERCENTAGE_THRESHOLD,
        major_assets=major_assets
    )

    return PositionFilter(thresholds)


# =============================================================================
# LIQUIDATION PROXIMITY FILTERING
# =============================================================================

def calculate_liquidation_proximity(position: Position) -> Optional[float]:
    """
    Calculate how close a position is to liquidation as a percentage.

    Returns:
        Float between 0 and 1 representing distance to liquidation.
        0.05 = 5% away from liquidation
        None if prices not available
    """
    if not position.current_price or not position.liquidation_price:
        return None

    if position.current_price <= 0:
        return None

    proximity = abs(position.current_price - position.liquidation_price) / position.current_price
    return proximity


def calculate_liquidation_risk_score(position: Position) -> Optional[float]:
    """
    Calculate a risk score that balances notional size with liquidation proximity.

    Formula: risk_score = notional_usd / liquidation_proximity

    Higher score = more interesting (larger position closer to liquidation)

    Examples:
        - $10M position 5% from liq: 10M / 0.05 = 200M score
        - $1M position 1% from liq: 1M / 0.01 = 100M score
        - $50M position 50% from liq: 50M / 0.50 = 100M score

    Returns:
        Risk score or None if cannot calculate
    """
    proximity = calculate_liquidation_proximity(position)

    if proximity is None:
        return None

    # Avoid division by zero - treat < 0.1% as 0.1%
    if proximity < 0.001:
        proximity = 0.001

    return position.notional_usd / proximity


def find_liquidation_candidates(
    positions: List[Position],
    min_notional: float = 500_000,
    max_proximity: float = 0.10,
) -> List[Position]:
    """
    Find positions that are close to liquidation and sufficiently large.

    Args:
        positions: All positions to search
        min_notional: Minimum position size in USD (default $500K)
        max_proximity: Maximum distance to liquidation (default 5%)

    Returns:
        Positions meeting criteria, sorted by risk_score descending
    """
    candidates = []

    for pos in positions:
        # Check minimum notional
        if pos.notional_usd < min_notional:
            continue

        # Check liquidation proximity
        proximity = calculate_liquidation_proximity(pos)
        if proximity is None or proximity > max_proximity:
            continue

        candidates.append(pos)

    # Sort by risk score (highest first)
    candidates.sort(
        key=lambda p: calculate_liquidation_risk_score(p) or 0,
        reverse=True
    )

    return candidates


def ensure_asset_coverage(
    all_positions: List[Position],
    flagged_positions: List[FlaggedPosition],
    min_notional: float = 500_000,
    max_proximity: float = 0.10,
) -> List[FlaggedPosition]:
    """
    Ensure at least 1 long and 1 short per asset in results.

    For assets missing coverage in flagged_positions, find candidates
    using the liquidation proximity filter (more lenient criteria).

    Args:
        all_positions: All scraped positions (unfiltered)
        flagged_positions: Already flagged positions from threshold filter
        min_notional: Minimum notional for liquidation candidates
        max_proximity: Maximum distance to liquidation (e.g., 0.05 = 5%)

    Returns:
        Extended list of flagged positions with coverage gaps filled
    """
    from models import AlertReason

    # Build coverage map: asset -> {long: FlaggedPosition, short: FlaggedPosition}
    coverage = {}
    for fp in flagged_positions:
        asset = fp.position.asset.upper()
        side = fp.position.side.value  # "long" or "short"

        if asset not in coverage:
            coverage[asset] = {"long": None, "short": None}

        # Keep the one with highest notional if multiple exist
        existing = coverage[asset][side]
        if existing is None or fp.position.notional_usd > existing.position.notional_usd:
            coverage[asset][side] = fp

    # Find all unique assets in the dataset
    all_assets = set(p.asset.upper() for p in all_positions)

    # Find liquidation candidates for gap filling
    liq_candidates = find_liquidation_candidates(
        all_positions,
        min_notional=min_notional,
        max_proximity=max_proximity
    )

    # Group candidates by asset and side
    candidates_by_asset_side = {}
    for pos in liq_candidates:
        key = (pos.asset.upper(), pos.side.value)
        if key not in candidates_by_asset_side:
            candidates_by_asset_side[key] = pos  # Already sorted, first is best

    # Fill gaps
    additions = []
    for asset in all_assets:
        if asset not in coverage:
            coverage[asset] = {"long": None, "short": None}

        for side in ["long", "short"]:
            if coverage[asset][side] is None:
                # Try to find a liquidation candidate
                key = (asset, side)
                if key in candidates_by_asset_side:
                    candidate = candidates_by_asset_side[key]
                    proximity = calculate_liquidation_proximity(candidate)
                    risk_score = calculate_liquidation_risk_score(candidate)

                    logger.debug(
                        f"Coverage gap filled: {asset} {side.upper()} "
                        f"${candidate.notional_usd:,.0f} @ {proximity:.2%} from liq "
                        f"(risk_score: {risk_score:,.0f})"
                    )

                    fp = FlaggedPosition(
                        position=candidate,
                        alert_reasons=[AlertReason.LIQUIDATION_PROXIMITY]
                    )
                    additions.append(fp)
                    coverage[asset][side] = fp

    if additions:
        logger.info(f"Added {len(additions)} positions to fill coverage gaps")

    # Combine original flagged with additions
    result = list(flagged_positions) + additions
    return result


def calculate_dynamic_min_notional(
    oi_data: Dict[str, float],
    oi_percentage: float = 0.005,
    floor: float = 500_000,
) -> float:
    """
    Calculate the minimum notional threshold for Hyperdash pre-filtering.

    For each asset, the threshold is MAX(oi_percentage * OI, floor).
    Returns the MINIMUM across all assets so we don't miss any relevant positions.

    This value is used as a pre-filter on Hyperdash to reduce scraping load,
    then more specific per-asset filtering is done after scraping.

    Args:
        oi_data: Dict of asset -> open interest in USD
        oi_percentage: Percentage of OI threshold (default 0.005 = 0.5%)
        floor: Minimum threshold floor in USD (default $500,000)

    Returns:
        The minimum notional threshold to use for pre-filtering
    """
    if not oi_data:
        logger.warning("No OI data provided, using floor as min notional")
        return floor

    # Calculate per-asset thresholds
    thresholds = []
    for asset, oi in oi_data.items():
        if oi and oi > 0:
            asset_threshold = max(oi * oi_percentage, floor)
            thresholds.append(asset_threshold)
            logger.debug(f"{asset}: OI=${oi:,.0f}, threshold=${asset_threshold:,.0f}")

    if not thresholds:
        return floor

    # Use the minimum threshold to ensure we capture all relevant positions
    # (The floor is usually the minimum, but this handles edge cases)
    min_threshold = min(thresholds)

    logger.info(
        f"Dynamic min notional: ${min_threshold:,.0f} "
        f"(from {len(thresholds)} assets, floor=${floor:,.0f})"
    )

    return min_threshold


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.DEBUG)
    
    # Create filter with custom thresholds
    filter_obj = create_filter_from_params(
        major_threshold=10_000_000,  # $10M for BTC/ETH/SOL
        default_threshold=1_000_000,  # $1M for others
        oi_percentage=0.05           # 5% of OI
    )
    
    # Set some mock OI data
    filter_obj.bulk_set_open_interest({
        "BTC": 500_000_000,   # $500M OI
        "ETH": 300_000_000,   # $300M OI
        "SOL": 100_000_000,   # $100M OI
        "DOGE": 50_000_000,   # $50M OI
    })
    
    print(filter_obj.summary())
    
    # Test with mock positions
    test_positions = [
        Position(
            trader_address="0x1234...",
            asset="BTC",
            side=PositionSide.LONG,
            notional_usd=15_000_000,  # $15M BTC long - should flag (n_1)
            size=150.0
        ),
        Position(
            trader_address="0x5678...",
            asset="DOGE",
            side=PositionSide.SHORT,
            notional_usd=3_000_000,  # $3M DOGE short - should flag (n_2 and x%)
            size=100_000_000.0
        ),
        Position(
            trader_address="0x9abc...",
            asset="ETH",
            side=PositionSide.LONG,
            notional_usd=5_000_000,  # $5M ETH - should NOT flag
            size=1500.0
        ),
    ]
    
    flagged = filter_obj.filter_positions(test_positions)
    
    print(f"\n=== Flagged Positions ({len(flagged)}) ===")
    for fp in flagged:
        print(fp)
