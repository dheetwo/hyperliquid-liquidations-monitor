"""
Data Models for Hyperdash Scanner
==================================
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"


class AlertReason(Enum):
    MAJOR_ASSET_THRESHOLD = "major_asset_threshold"  # BTC/ETH/SOL exceeds n_1
    NOTIONAL_THRESHOLD = "notional_threshold"        # Exceeds n_2
    OI_PERCENTAGE_THRESHOLD = "oi_percentage"        # Exceeds x% of OI
    LIQUIDATION_PROXIMITY = "liquidation_proximity"  # Close to liquidation (coverage fill)


@dataclass
class Position:
    """Represents a single trader's position on an asset"""

    # Core position data
    trader_address: str
    asset: str
    side: PositionSide
    notional_usd: float
    size: float  # Position size in asset terms
    entry_price: Optional[float] = None
    current_price: Optional[float] = None  # Mark price at time of data pull
    liquidation_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    leverage: Optional[float] = None

    # Context
    asset_open_interest: Optional[float] = None  # Total OI for the asset
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def oi_percentage(self) -> Optional[float]:
        """Calculate position as percentage of Open Interest"""
        if self.asset_open_interest and self.asset_open_interest > 0:
            return self.notional_usd / self.asset_open_interest
        return None
    
    def __str__(self) -> str:
        side_emoji = "🟢" if self.side == PositionSide.LONG else "🔴"
        oi_pct = f" ({self.oi_percentage:.2%} of OI)" if self.oi_percentage else ""
        return (
            f"{side_emoji} {self.asset} | ${self.notional_usd:,.0f}{oi_pct}\n"
            f"   Trader: {self.trader_address[:10]}..."
        )


@dataclass
class FlaggedPosition:
    """A position that meets alert criteria"""
    
    position: Position
    alert_reasons: List[AlertReason]
    
    def __str__(self) -> str:
        reasons = ", ".join([r.value for r in self.alert_reasons])
        return f"{self.position}\n   Alert: {reasons}"


@dataclass
class AssetContext:
    """Market context for an asset"""

    asset: str
    mark_price: float
    open_interest: float  # in USD
    funding_rate: Optional[float] = None
    volume_24h: Optional[float] = None

    # Thresholds (for reference)
    notional_threshold: Optional[float] = None
    oi_pct_threshold: Optional[float] = None


@dataclass
class LiquidationProximitySummary:
    """Summary of liquidation proximity across all positions"""

    positions_within_5pct: int = 0
    positions_within_10pct: int = 0
    positions_within_50pct: int = 0
    closest_position: Optional[dict] = None  # {asset, side, notional, proximity_pct}

    def to_dict(self) -> dict:
        result = {
            "positions_within_5pct": self.positions_within_5pct,
            "positions_within_10pct": self.positions_within_10pct,
            "positions_within_50pct": self.positions_within_50pct,
        }
        if self.closest_position:
            result["closest_to_liquidation"] = self.closest_position
        else:
            result["closest_to_liquidation"] = None
        return result


@dataclass
class ScanResult:
    """Results of a complete scan"""

    timestamp: datetime
    assets_scanned: int
    total_positions_checked: int
    flagged_positions: List[FlaggedPosition]
    errors: List[str] = field(default_factory=list)
    liquidation_summary: Optional[LiquidationProximitySummary] = None
    
    @property
    def flagged_count(self) -> int:
        return len(self.flagged_positions)
    
    def summary(self) -> str:
        return (
            f"Scan completed at {self.timestamp.isoformat()}\n"
            f"Assets scanned: {self.assets_scanned}\n"
            f"Positions checked: {self.total_positions_checked}\n"
            f"Flagged positions: {self.flagged_count}\n"
            f"Errors: {len(self.errors)}"
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        result = {
            "timestamp": self.timestamp.isoformat(),
            "assets_scanned": self.assets_scanned,
            "total_positions_checked": self.total_positions_checked,
            "flagged_count": self.flagged_count,
            "liquidation_proximity": self.liquidation_summary.to_dict() if self.liquidation_summary else {
                "positions_within_5pct": 0,
                "positions_within_10pct": 0,
                "positions_within_50pct": 0,
                "closest_to_liquidation": None
            },
            "flagged_positions": [
                {
                    "asset": fp.position.asset,
                    "trader": fp.position.trader_address,
                    "side": fp.position.side.value,
                    "notional_usd": fp.position.notional_usd,
                    "entry_price": fp.position.entry_price,
                    "current_price": fp.position.current_price,
                    "liquidation_price": fp.position.liquidation_price,
                    "oi_percentage": fp.position.oi_percentage,
                    "alert_reasons": [r.value for r in fp.alert_reasons]
                }
                for fp in self.flagged_positions
            ],
            "errors": self.errors
        }
        return result
