"""
Position Models
===============

Dataclasses for position data from Hyperliquid API.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set


# Cohort category constants
SIZE_COHORTS = {"kraken", "large_whale", "whale", "shark"}
PNL_COHORTS = {"rekt", "very_unprofitable", "extremely_profitable", "very_profitable"}


def format_cohorts(cohorts: Set[str]) -> str:
    """
    Format cohort set for display.

    Shows size cohort / PNL cohort if both present.
    Example: "kraken/rekt", "whale", "very_unprofitable"
    """
    if not cohorts:
        return ""

    size = [c for c in cohorts if c in SIZE_COHORTS]
    pnl = [c for c in cohorts if c in PNL_COHORTS]

    parts = []
    if size:
        parts.append(size[0])  # Only show first size cohort (highest priority)
    if pnl:
        parts.append(pnl[0])  # Only show first PNL cohort

    return "/".join(parts)


@dataclass
class Position:
    """Position data from Hyperliquid API clearinghouseState."""
    address: str
    token: str
    side: str
    size: float
    leverage: float
    leverage_type: str
    entry_price: float
    mark_price: float
    position_value: float
    unrealized_pnl: float
    roe: float
    liquidation_price: Optional[float]  # None if no liquidation price
    margin_used: float
    funding_since_open: float
    cohort: str
    exchange: str      # "main", "xyz", "flx", "vntl", "hyna", "km"
    is_isolated: bool  # True for isolated margin positions (all sub-exchange positions)


@dataclass
class WatchedPosition:
    """
    A position being monitored for liquidation proximity.

    Tracks the position details and monitoring state.
    """
    # Position identification
    address: str
    token: str
    exchange: str
    side: str

    # Position details
    liq_price: float
    position_value: float
    is_isolated: bool
    hunting_score: float

    # Cohort information - wallet can belong to size cohort AND/OR PNL cohort
    cohorts: Set[str] = field(default_factory=set)

    # Tracking state
    last_distance_pct: float = None
    last_mark_price: float = None
    threshold_pct: float = None  # Computed from multi-factorial rules
    alerted_proximity: bool = False  # Already sent proximity alert (0.5%)
    alerted_critical: bool = False  # Already sent critical alert (0.1%)
    in_critical_zone: bool = False  # Currently in critical zone (<0.2%)
    liq_price_at_critical_entry: float = None  # Liq price when entering critical zone (for recovery detection)
    first_seen_scan: str = None  # Timestamp of first detection
    alert_message_id: int = None  # Telegram message_id for reply threading
    last_proximity_message_id: int = None  # Last proximity/critical alert message_id

    # Previous state for change detection (liquidation status monitoring)
    previous_liq_price: Optional[float] = None
    previous_position_value: Optional[float] = None

    # Alert flags for liquidation status events
    alerted_collateral_added: bool = False
    alerted_liquidation: bool = False

    @property
    def cohort_display(self) -> str:
        """Format cohorts for display in alerts."""
        return format_cohorts(self.cohorts)

    @property
    def position_key(self) -> str:
        """Unique key for this position (address + token + exchange + side)."""
        return f"{self.address}:{self.token}:{self.exchange}:{self.side}"

    def __hash__(self):
        return hash(self.position_key)

    def __eq__(self, other):
        if isinstance(other, WatchedPosition):
            return self.position_key == other.position_key
        return False
