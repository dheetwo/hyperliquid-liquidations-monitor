"""
Position Models
===============

Dataclasses for position data from Hyperliquid API.
"""

from dataclasses import dataclass, field
from typing import Optional


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

    # Tracking state
    last_distance_pct: float = None
    last_mark_price: float = None
    threshold_pct: float = None  # Computed from multi-factorial rules
    alerted_proximity: bool = False  # Already sent proximity alert (0.5%)
    alerted_critical: bool = False  # Already sent critical alert (0.1%)
    in_critical_zone: bool = False  # Currently in critical zone (<0.2%)
    first_seen_scan: str = None  # Timestamp of first detection
    alert_message_id: int = None  # Telegram message_id for reply threading
    last_proximity_message_id: int = None  # Last proximity/critical alert message_id

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
