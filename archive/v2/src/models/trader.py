"""
Trader Models
=============

Dataclasses for trader data from Hyperdash API.
"""

from dataclasses import dataclass


@dataclass
class CohortTrader:
    """Trader data from Hyperdash cohort API."""
    address: str
    perp_equity: float
    perp_bias: str
    position_value: float
    leverage: float
    sum_upnl: float
    pnl_cohort: str
    cohort: str
