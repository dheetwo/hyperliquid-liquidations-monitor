"""
Configuration for Hyperdash Liquidation Monitor v3

All settings in one place for easy tuning.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from pathlib import Path
import os


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class Position:
    """A position from Hyperliquid API."""
    address: str
    token: str
    exchange: str  # "" = main, "xyz", etc.
    side: str  # "long" or "short"
    size: float
    entry_price: float
    mark_price: float
    liquidation_price: Optional[float]
    position_value: float  # Notional USD
    unrealized_pnl: float
    leverage: float
    leverage_type: str  # "cross" or "isolated"
    margin_used: float

    @property
    def key(self) -> str:
        """Unique identifier for this position."""
        return f"{self.address}:{self.token}:{self.exchange}:{self.side}"

    @property
    def has_liq_price(self) -> bool:
        """Whether this position has a liquidation price."""
        return self.liquidation_price is not None and self.liquidation_price > 0

    def distance_to_liq(self, current_price: Optional[float] = None) -> Optional[float]:
        """
        Calculate distance to liquidation as a percentage.
        Returns positive if price must move against the position.
        Returns None if no liquidation price.
        """
        if not self.has_liq_price:
            return None

        price = current_price if current_price is not None else self.mark_price
        if price <= 0:
            return None

        if self.side == "long":
            # For longs, liq price is below current price
            distance = (price - self.liquidation_price) / price * 100
        else:
            # For shorts, liq price is above current price
            distance = (self.liquidation_price - price) / price * 100

        return distance


@dataclass
class Wallet:
    """A wallet from the wallet registry."""
    address: str
    source: str  # "hyperdash" or "liq_history"
    cohort: Optional[str]
    position_value: Optional[float]
    last_scanned: Optional[str]
    scan_frequency: str  # "normal" or "infrequent"


class Bucket(Enum):
    """Position monitoring bucket based on liquidation proximity."""
    CRITICAL = "critical"  # <= 0.125% to liq
    HIGH = "high"          # 0.125% - 0.25%
    NORMAL = "normal"      # > 0.25%


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """All configuration settings."""

    # -------------------------------------------------------------------------
    # Exchanges
    # -------------------------------------------------------------------------
    # "" = main Hyperliquid, "xyz" = TradeXYZ (stocks/commodities)
    exchanges: List[str] = field(default_factory=lambda: ["", "xyz"])

    # -------------------------------------------------------------------------
    # Bucket Thresholds (distance to liquidation %)
    # -------------------------------------------------------------------------
    critical_distance_pct: float = 0.125
    high_distance_pct: float = 0.25

    # -------------------------------------------------------------------------
    # Refresh Intervals (seconds)
    # -------------------------------------------------------------------------
    critical_refresh_sec: float = 0.5
    high_refresh_sec: float = 3.0
    normal_refresh_sec: float = 30.0

    # How often to fetch mark prices (seconds)
    price_refresh_sec: float = 5.0

    # -------------------------------------------------------------------------
    # Wallet Filtering
    # -------------------------------------------------------------------------
    # Minimum total position value to scan a wallet
    min_wallet_value: float = 60_000

    # Wallets with position value below this are scanned infrequently
    infrequent_scan_threshold: float = 60_000

    # How often to scan "infrequent" wallets (hours)
    infrequent_scan_interval_hours: int = 24

    # -------------------------------------------------------------------------
    # Position Notional Thresholds (Production Values)
    # Based on order book liquidity analysis
    # Isolated = Cross / 5 (ISOLATED_MULTIPLIER)
    # -------------------------------------------------------------------------

    # Isolated multiplier (cross threshold / ISOLATED_MULTIPLIER = isolated threshold)
    isolated_multiplier: float = 5.0

    # --- Main Exchange Token Tiers ---
    main_mega_cap: set = field(default_factory=lambda: {"BTC", "ETH"})
    main_large_cap: set = field(default_factory=lambda: {"SOL"})
    main_tier1_alts: set = field(default_factory=lambda: {"DOGE", "XRP", "HYPE"})
    main_tier2_alts: set = field(default_factory=lambda: {"BNB"})
    main_mid_alts: set = field(default_factory=lambda: {
        "ADA", "AVAX", "LINK", "LTC", "DOT", "UNI", "ATOM", "TRX", "SHIB",
        "SUI", "AAVE", "CRV", "NEAR", "OP", "kPEPE", "kSHIB", "kBONK",
    })
    main_low_alts: set = field(default_factory=lambda: {
        "APT", "ARB", "TON", "SEI", "TIA", "INJ",
        "PEPE", "WIF", "BONK", "FLOKI",
        "MKR", "RENDER", "FET", "FIL", "ORDI", "XLM",
    })

    # Main exchange cross thresholds
    main_thresholds_cross: Dict[str, float] = field(default_factory=lambda: {
        "MEGA_CAP": 30_000_000,     # $30M - BTC, ETH
        "LARGE_CAP": 20_000_000,    # $20M - SOL
        "TIER1_ALTS": 10_000_000,   # $10M - DOGE, XRP, HYPE
        "TIER2_ALTS": 2_000_000,    # $2M - BNB
        "MID_ALTS": 1_000_000,      # $1M - mid liquidity alts
        "LOW_ALTS": 500_000,        # $500K - low liquidity alts
        "SMALL_CAPS": 300_000,      # $300K - everything else
    })

    # --- XYZ Exchange Token Classifications (all isolated) ---
    xyz_indices: set = field(default_factory=lambda: {"XYZ100"})
    xyz_high_liq_equities: set = field(default_factory=lambda: {
        "NFLX", "INTC", "GOOGL", "NVDA", "TSLA", "AMZN", "META",
        "MSTR", "AAPL", "COIN", "MSFT", "AMD", "MU", "PLTR", "ORCL",
    })
    xyz_low_liq_equities: set = field(default_factory=lambda: {
        "BABA", "CRCL", "HOOD", "SNDK", "TSM", "LLY", "COST"
    })
    xyz_gold: set = field(default_factory=lambda: {"GOLD"})
    xyz_oil: set = field(default_factory=lambda: {"CL"})
    xyz_silver: set = field(default_factory=lambda: {"SILVER"})
    xyz_metals: set = field(default_factory=lambda: {"COPPER"})
    xyz_energy: set = field(default_factory=lambda: {"NATGAS"})
    xyz_uranium: set = field(default_factory=lambda: {"URANIUM"})
    xyz_forex: set = field(default_factory=lambda: {"EUR", "JPY"})

    # XYZ exchange thresholds (all isolated)
    xyz_thresholds: Dict[str, float] = field(default_factory=lambda: {
        "INDICES": 2_000_000,           # $2M - XYZ100
        "HIGH_LIQ_EQUITIES": 1_000_000, # $1M - NFLX, NVDA, TSLA, etc.
        "LOW_LIQ_EQUITIES": 500_000,    # $500K - lower liquidity stocks
        "EQUITIES": 500_000,            # $500K - default for other stocks
        "GOLD": 1_000_000,              # $1M
        "OIL": 600_000,                 # $600K - CL
        "SILVER": 1_000_000,            # $1M
        "METALS": 400_000,              # $400K - COPPER
        "ENERGY": 300_000,              # $300K - NATGAS
        "URANIUM": 200_000,             # $200K
        "FOREX": 1_000_000,             # $1M - EUR, JPY
    })

    # Other HIP-3 sub-exchanges (flx, hyna, km) - flat threshold
    other_sub_exchanges: set = field(default_factory=lambda: {"flx", "hyna", "km"})
    other_sub_exchange_threshold: float = 400_000  # $400K

    # -------------------------------------------------------------------------
    # Alert Thresholds
    # -------------------------------------------------------------------------
    # Alert when position crosses below this distance (approaching)
    proximity_alert_pct: float = 0.25

    # Alert again when position crosses below this (imminent)
    critical_alert_pct: float = 0.125

    # Minimum liq price change to detect collateral addition (%)
    collateral_change_min_pct: float = 2.0

    # Position value drop to trigger partial liquidation alert (%)
    partial_liq_threshold_pct: float = 10.0

    # -------------------------------------------------------------------------
    # API Settings
    # -------------------------------------------------------------------------
    hyperliquid_url: str = "https://api.hyperliquid.xyz/info"
    hyperdash_url: str = "https://api.hyperdash.com/graphql"

    # Concurrent requests for batch fetching
    # Hyperliquid has strict rate limits - keep concurrency low
    max_concurrent_requests: int = 5

    # Delay between API requests (seconds)
    request_delay_sec: float = 0.25  # 250ms between requests

    # Rate limiting backoff (seconds)
    rate_limit_backoff_sec: float = 2.0
    max_retries: int = 3

    # -------------------------------------------------------------------------
    # Cohorts to fetch from Hyperdash (priority order)
    # -------------------------------------------------------------------------
    cohorts: List[str] = field(default_factory=lambda: [
        "kraken",           # $5M+
        "large_whale",      # $1M-$5M
        "whale",            # $250K-$1M
        "rekt",             # Large realized losses
        "extremely_profitable",
        "very_unprofitable",
        "very_profitable",
        "shark",            # $100K-$250K (optional, many addresses)
    ])

    # -------------------------------------------------------------------------
    # Database Paths
    # -------------------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "data")

    @property
    def wallets_db_path(self) -> Path:
        return self.data_dir / "wallets.db"

    @property
    def positions_db_path(self) -> Path:
        return self.data_dir / "positions.db"

    # -------------------------------------------------------------------------
    # Telegram Settings (from environment)
    # -------------------------------------------------------------------------
    @property
    def telegram_bot_token(self) -> Optional[str]:
        return os.environ.get("TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self) -> Optional[str]:
        return os.environ.get("TELEGRAM_CHAT_ID")

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def get_notional_threshold(self, token: str, exchange: str, is_isolated: bool) -> float:
        """
        Get the minimum notional threshold for a position to be monitored.

        Uses token classification tiers with consistent 5:1 cross/isolated ratio
        for main exchange, and specific thresholds for xyz assets.

        Args:
            token: Token symbol (e.g., "BTC", "TSLA", "GOLD")
            exchange: Exchange name ("", "xyz", "flx", "hyna", "km")
            is_isolated: Whether the position uses isolated margin

        Returns:
            Minimum notional value in USD
        """
        # Strip prefixes if present
        if token.startswith("xyz:"):
            token = token[4:]

        # XYZ exchange - all isolated, use xyz-specific thresholds
        if exchange == "xyz":
            if token in self.xyz_indices:
                return self.xyz_thresholds["INDICES"]
            elif token in self.xyz_high_liq_equities:
                return self.xyz_thresholds["HIGH_LIQ_EQUITIES"]
            elif token in self.xyz_low_liq_equities:
                return self.xyz_thresholds["LOW_LIQ_EQUITIES"]
            elif token in self.xyz_gold:
                return self.xyz_thresholds["GOLD"]
            elif token in self.xyz_oil:
                return self.xyz_thresholds["OIL"]
            elif token in self.xyz_silver:
                return self.xyz_thresholds["SILVER"]
            elif token in self.xyz_metals:
                return self.xyz_thresholds["METALS"]
            elif token in self.xyz_energy:
                return self.xyz_thresholds["ENERGY"]
            elif token in self.xyz_uranium:
                return self.xyz_thresholds["URANIUM"]
            elif token in self.xyz_forex:
                return self.xyz_thresholds["FOREX"]
            else:
                # Default for unknown xyz tokens (probably equities)
                return self.xyz_thresholds["EQUITIES"]

        # Other HIP-3 sub-exchanges (flx, hyna, km) - flat threshold
        if exchange in self.other_sub_exchanges:
            return self.other_sub_exchange_threshold

        # Main exchange - use token tier classification
        if token in self.main_mega_cap:
            cross_threshold = self.main_thresholds_cross["MEGA_CAP"]
        elif token in self.main_large_cap:
            cross_threshold = self.main_thresholds_cross["LARGE_CAP"]
        elif token in self.main_tier1_alts:
            cross_threshold = self.main_thresholds_cross["TIER1_ALTS"]
        elif token in self.main_tier2_alts:
            cross_threshold = self.main_thresholds_cross["TIER2_ALTS"]
        elif token in self.main_mid_alts:
            cross_threshold = self.main_thresholds_cross["MID_ALTS"]
        elif token in self.main_low_alts:
            cross_threshold = self.main_thresholds_cross["LOW_ALTS"]
        else:
            cross_threshold = self.main_thresholds_cross["SMALL_CAPS"]

        # Apply isolated multiplier (5:1 ratio)
        if is_isolated:
            return cross_threshold / self.isolated_multiplier
        else:
            return cross_threshold

    def classify_bucket(self, distance_pct: Optional[float]) -> Bucket:
        """
        Classify a position into a monitoring bucket based on distance to liquidation.

        Args:
            distance_pct: Distance to liquidation as percentage (positive = must move against)

        Returns:
            Bucket classification
        """
        if distance_pct is None:
            return Bucket.NORMAL

        if distance_pct <= self.critical_distance_pct:
            return Bucket.CRITICAL
        elif distance_pct <= self.high_distance_pct:
            return Bucket.HIGH
        return Bucket.NORMAL


# Global config instance
config = Config()
