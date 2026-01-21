"""
Monitor Service Configuration
=============================

Configuration for the liquidation monitor service.
"""

import os
from pathlib import Path

# Load .env file from project root
from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# =============================================================================
# TIMING SETTINGS
# =============================================================================

# Time between full scans (cohort -> position -> filter) - used in manual mode only
SCAN_INTERVAL_MINUTES = 90

# Mark price poll frequency during monitor phase
POLL_INTERVAL_SECONDS = 5

# =============================================================================
# POSITION CACHE SETTINGS
# =============================================================================
# Tiered refresh based on liquidation distance.
# Critical positions get continuous refresh, normal positions get background refresh.

# Tiered refresh thresholds (distance to liquidation %)
CACHE_TIER_CRITICAL_PCT = 0.125   # ≤0.125% = critical tier (continuous refresh)
CACHE_TIER_HIGH_PCT = 0.25        # 0.125-0.25% = high tier (frequent refresh)
# >0.25% = normal tier (background refresh)

# Tier refresh intervals (seconds)
CACHE_REFRESH_CRITICAL_SEC = 0.2  # Rate limit bound (~5 req/sec)
CACHE_REFRESH_HIGH_SEC = 2.5      # Every 2-3 seconds
CACHE_REFRESH_NORMAL_SEC = 30.0   # Every 30 seconds

# Discovery settings (find new addresses/positions)
DISCOVERY_MIN_INTERVAL_MINUTES = 30   # Minimum interval between discoveries
DISCOVERY_MAX_INTERVAL_MINUTES = 240  # Maximum interval (4 hours)
DISCOVERY_PRESSURE_CRITICAL_WEIGHT = 15  # Minutes to add per critical position
DISCOVERY_PRESSURE_HIGH_WEIGHT = 5       # Minutes to add per high position

# Cache freshness
CACHE_MAX_AGE_MINUTES = 60   # Force initial scan if cache older than this
CACHE_PRUNE_AGE_HOURS = 24   # Delete positions not refreshed in 24h

# =============================================================================
# DAILY SUMMARY SETTINGS
# =============================================================================
# Daily summary message showing all monitored positions.
# No intraday "new position" alerts - just quiet backend updates.

# Times for daily summary messages (24h format, EST)
DAILY_SUMMARY_TIMES = [
    (6, 0),   # 6:00 AM EST
]

# =============================================================================
# LEGACY SCHEDULED SCAN SETTINGS (deprecated - kept for reference)
# =============================================================================
# These settings are no longer used. The system now uses cache-based
# monitoring with tiered refresh instead of scheduled scans.

COMPREHENSIVE_SCAN_HOUR = 6    # (deprecated)
COMPREHENSIVE_SCAN_MINUTE = 30  # (deprecated)

# =============================================================================
# ASSET CLASSIFICATIONS
# =============================================================================

# Isolated position multiplier (cross threshold / ISOLATED_MULTIPLIER = isolated threshold)
ISOLATED_MULTIPLIER = 5.0

# -----------------------------------------------------------------------------
# MAIN EXCHANGE - Crypto Token Tiers
# -----------------------------------------------------------------------------
# Cross thresholds defined below; Isolated = Cross / ISOLATED_MULTIPLIER

MAIN_MEGA_CAP = {"BTC"}
MAIN_LARGE_CAP = {"ETH"}
MAIN_TIER1_ALTS = {"SOL", "BNB", "XRP"}
MAIN_TIER2_ALTS = {
    "DOGE", "ADA", "AVAX", "LINK", "LTC",
    "DOT", "MATIC", "UNI", "ATOM", "TRX", "SHIB", "HYPE"
}
MAIN_MID_ALTS = {
    "APT", "ARB", "OP", "SUI", "TON", "NEAR", "SEI", "TIA", "INJ",
    "PEPE", "WIF", "BONK", "FLOKI",
    "AAVE", "MKR", "RENDER", "FET", "FIL"
}
# Everything else = SMALL_CAPS (default)

# Cross thresholds for main exchange
MAIN_THRESHOLDS_CROSS = {
    "MEGA_CAP": 100_000_000,      # $100M - BTC
    "LARGE_CAP": 75_000_000,      # $75M - ETH
    "TIER1_ALTS": 25_000_000,     # $25M - SOL, BNB, XRP
    "TIER2_ALTS": 10_000_000,     # $10M - DOGE, ADA, AVAX, etc.
    "MID_ALTS": 5_000_000,        # $5M - APT, ARB, memes, DeFi, etc.
    "SMALL_CAPS": 1_500_000,      # $1.5M - everything else
}

# Legacy compatibility
MAJORS = ["ETH", "SOL", "BNB", "XRP"]

# -----------------------------------------------------------------------------
# XYZ EXCHANGE - Equities, Commodities, Forex (All Isolated)
# -----------------------------------------------------------------------------
# xyz only supports isolated margin - no cross/isolated distinction needed

XYZ_INDICES = {"XYZ100"}
XYZ_MEGA_EQUITIES = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"}
XYZ_LARGE_EQUITIES = {"AMD", "NFLX", "COIN", "MSTR", "ORCL", "TSM", "LLY", "COST"}
# Default for unknown stocks: BABA, CRCL, HOOD, INTC, MU, PLTR, RIVN, SKHX, SNDK, etc.

XYZ_GOLD = {"GOLD"}
XYZ_OIL = {"CL"}
XYZ_SILVER = {"SILVER"}
XYZ_METALS = {"COPPER"}
XYZ_ENERGY = {"NATGAS"}
XYZ_URANIUM = {"URANIUM"}
XYZ_FOREX = {"EUR", "JPY"}

# Thresholds for xyz exchange (all isolated)
XYZ_THRESHOLDS = {
    "INDICES": 5_000_000,         # $5M - XYZ100
    "MEGA_EQUITIES": 3_000_000,   # $3M - AAPL, MSFT, NVDA, etc.
    "LARGE_EQUITIES": 2_000_000,  # $2M - AMD, NFLX, COIN, etc.
    "EQUITIES": 1_000_000,        # $1M - default for other stocks
    "GOLD": 2_500_000,            # $2.5M
    "OIL": 2_000_000,             # $2M - CL (crude)
    "SILVER": 1_500_000,          # $1.5M
    "METALS": 1_000_000,          # $1M - COPPER
    "ENERGY": 800_000,            # $800K - NATGAS
    "URANIUM": 500_000,           # $500K - low liquidity expected
    "FOREX": 1_000_000,           # $1M - EUR, JPY
}

# -----------------------------------------------------------------------------
# OTHER HIP-3 SUB-EXCHANGES (flx, hyna, km) - All Isolated
# Note: vntl excluded - private equity assets have no external price discovery
# -----------------------------------------------------------------------------
# These sub-exchanges have lower liquidity; flat threshold for all tokens

OTHER_SUB_EXCHANGES = {"flx", "hyna", "km"}  # vntl excluded: no external price discovery
OTHER_SUB_EXCHANGE_THRESHOLD = 500_000  # $500K for all tokens

# =============================================================================
# NEW POSITION ALERT THRESHOLDS
# =============================================================================
# For "NEW LIQUIDATION TARGETS DETECTED" alerts.
# Position must meet BOTH minimum notional AND maximum distance requirements.
# Isolated positions get ISOLATED_MULTIPLIER applied to their notional for threshold checks.

NEW_POSITION_THRESHOLDS = {
    # At ≤3% distance to liquidation
    "standard": {
        "max_distance_pct": 3.0,
        "btc_min_value": 100_000_000,      # $100M for BTC
        "majors_min_value": 50_000_000,    # $50M for ETH/SOL/BNB/XRP
        "alts_min_value": 5_000_000,       # $5M for other alts
    },
    # At ≤1% distance to liquidation (lower bar)
    "close": {
        "max_distance_pct": 1.0,
        "btc_min_value": 50_000_000,       # $50M for BTC
        "majors_min_value": 25_000_000,    # $25M for ETH/SOL/BNB/XRP
        "alts_min_value": 2_000_000,       # $2M for other alts
    },
}

# =============================================================================
# PROXIMITY ALERT THRESHOLDS
# =============================================================================
# For "POSITION APPROACHING LIQUIDATION" alerts during monitor phase.
# Only triggers when position crosses below this threshold for the first time.

PROXIMITY_ALERT_THRESHOLD_PCT = 0.25  # Alert when distance drops below 0.25%

# =============================================================================
# HIGH-INTENSITY MONITORING & ALERTS
# =============================================================================
# Positions under PROXIMITY_ALERT_THRESHOLD_PCT get high-intensity monitoring:
# - Position data refreshed frequently to detect liq price changes (margin adds)
# - Liq price tracked for recovery detection (manual intervention vs price movement)
#
# Alert at CRITICAL_ALERT_PCT threshold (imminent liquidation).
# Recovery alert when position goes from <PROXIMITY_ALERT_THRESHOLD_PCT to >RECOVERY_PCT
# AND liquidation price changed (indicating manual intervention).

CRITICAL_ALERT_PCT = 0.125    # Alert when crossing below 0.125% (imminent)
CRITICAL_ZONE_PCT = 0.25      # Threshold for entering critical monitoring zone (same as proximity)
RECOVERY_PCT = 0.5            # Recovery detection threshold

# Dynamic refresh interval for critical positions
# Scales based on number of positions to avoid rate limits
CRITICAL_REFRESH_MIN_INTERVAL = 2   # Minimum seconds (base interval)
CRITICAL_REFRESH_MAX_INTERVAL = 5   # Maximum seconds (many positions)
CRITICAL_REFRESH_SCALE_FACTOR = 0.3 # Seconds to add per position
MAX_CRITICAL_POSITIONS = 30         # Max positions to track (prioritize closest)

# =============================================================================
# LIQUIDATION STATUS MONITORING
# =============================================================================
# Detect and alert on position state changes: collateral additions, liquidations.
# These alerts inform about events that have occurred, not just proximity warnings.

# Minimum liquidation price change (%) to detect collateral addition
# For longs: liq price must decrease by this % (safer)
# For shorts: liq price must increase by this % (safer)
COLLATERAL_CHANGE_MIN_PCT = 2.0

# Minimum position value drop (%) to consider a partial liquidation
# E.g., if position drops from $5M to $4M (20% drop), it's a partial liq
PARTIAL_LIQ_THRESHOLD_PCT = 10.0

# Whether to alert on natural price recovery (price moved favorably)
# If False, only alert when user adds collateral (liq price changes)
ALERT_NATURAL_RECOVERY = False

# =============================================================================
# WATCHLIST SETTINGS
# =============================================================================

# Maximum distance (%) to include in watchlist - positions farther won't be monitored
MAX_WATCH_DISTANCE_PCT = 5.0

# Minimum wallet position value to fetch positions for
# Wallets with total notional below this are skipped entirely (saves API calls)
# Matches lowest position threshold ($300K isolated small caps)
MIN_WALLET_POSITION_VALUE = 300_000  # $300K

# Minimum notional thresholds are now defined via token classification above.
# Use get_watchlist_threshold() function to get the threshold for a given token.

# Maximum age (minutes) for cached position data to be used as fallback
# If rate limited, will use cached data if it's newer than this
POSITION_CACHE_MAX_AGE_MINUTES = 30

# Maximum age (hours) for cohort cache (wallet addresses)
# Cohort membership doesn't change frequently, so we can cache it for longer.
# Only comprehensive scans will refresh the cohort cache; normal/high-priority
# scans will use cached data if it's fresh enough.
COHORT_CACHE_MAX_AGE_HOURS = 24  # 24 hours

# =============================================================================
# TELEGRAM SETTINGS
# =============================================================================

# Set via environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Message formatting
MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096

# =============================================================================
# SCAN MODE SETTINGS
# =============================================================================

# Default scan mode for the monitor service
DEFAULT_SCAN_MODE = "normal"

# Available modes (from src/pipeline/step2_position.py):
# - "high-priority": kraken + large_whale + rekt, main + xyz only
# - "normal": kraken + large_whale + whale + rekt + profit/loss cohorts, main + xyz only
# - "comprehensive": all cohorts, all exchanges

# =============================================================================
# DATA PATHS
# =============================================================================

# Default paths for data files
COHORT_DATA_PATH = "data/raw/cohort_data.csv"
POSITION_DATA_PATH = "data/raw/position_data.csv"
FILTERED_DATA_PATH = "data/processed/filtered_position_data.csv"

# =============================================================================
# LOGGING
# =============================================================================

LOG_LEVEL = "INFO"
LOG_FILE = "logs/monitor.log"

# =============================================================================
# SECONDARY MONITOR SETTINGS (All-Cohorts Monitor)
# =============================================================================
# Settings for running a secondary monitor that covers ALL cohorts with
# lower thresholds. Designed to run alongside the primary monitor without
# impacting performance.
#
# Key differences from primary monitor:
# - Lower notional thresholds (captures more positions)
# - Longer discovery intervals (less API pressure)
# - Separate database file (no lock contention)
# - Optional separate Telegram channel

# Separate Telegram channel for secondary alerts (optional)
# If not set, uses the same channel as primary monitor
SECONDARY_TELEGRAM_BOT_TOKEN = os.environ.get("SECONDARY_TELEGRAM_BOT_TOKEN", "")
SECONDARY_TELEGRAM_CHAT_ID = os.environ.get("SECONDARY_TELEGRAM_CHAT_ID", "")

# Use primary channel if secondary not configured
def get_secondary_telegram_config():
    """Get Telegram config for secondary monitor, falling back to primary."""
    bot_token = SECONDARY_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN
    chat_id = SECONDARY_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID
    return bot_token, chat_id

# Secondary monitor notional thresholds (lower than primary)
# These are divisors applied to primary thresholds
SECONDARY_THRESHOLD_DIVISOR = 10.0  # 10x lower thresholds

# Secondary discovery intervals (longer than primary - less API pressure)
SECONDARY_DISCOVERY_MIN_INTERVAL_MINUTES = 60   # 1 hour minimum
SECONDARY_DISCOVERY_MAX_INTERVAL_MINUTES = 360  # 6 hours maximum

# Secondary refresh intervals (slower than primary)
SECONDARY_CACHE_REFRESH_CRITICAL_SEC = 0.5   # Slower critical refresh
SECONDARY_CACHE_REFRESH_HIGH_SEC = 5.0       # Slower high refresh
SECONDARY_CACHE_REFRESH_NORMAL_SEC = 60.0    # Slower normal refresh

# Secondary database file (separate from primary to avoid contention)
SECONDARY_DB_PATH = "data/monitor_secondary.db"
SECONDARY_LOG_FILE = "logs/monitor_secondary.log"

# Maximum positions to track in secondary monitor
# Prevents runaway memory usage with lower thresholds
SECONDARY_MAX_POSITIONS = 5000


def get_proximity_alert_threshold() -> float:
    """
    Get the proximity alert threshold.

    Returns:
        Distance threshold percentage for triggering proximity alerts.
    """
    return PROXIMITY_ALERT_THRESHOLD_PCT


def get_watchlist_threshold(token: str, exchange: str, is_isolated: bool) -> float:
    """
    Get the minimum notional threshold for watchlist inclusion.

    Uses token classification tiers with consistent 5:1 cross/isolated ratio
    for main exchange, and specific thresholds for xyz assets.

    Args:
        token: Token symbol (e.g., "BTC", "TSLA", "GOLD")
        exchange: Exchange name ("main", "xyz", "flx", "hyna", "km")
        is_isolated: Whether the position uses isolated margin

    Returns:
        Minimum notional value in USD
    """
    # xyz exchange - all isolated, use xyz-specific thresholds
    if exchange == "xyz":
        if token in XYZ_INDICES:
            return XYZ_THRESHOLDS["INDICES"]
        elif token in XYZ_MEGA_EQUITIES:
            return XYZ_THRESHOLDS["MEGA_EQUITIES"]
        elif token in XYZ_LARGE_EQUITIES:
            return XYZ_THRESHOLDS["LARGE_EQUITIES"]
        elif token in XYZ_GOLD:
            return XYZ_THRESHOLDS["GOLD"]
        elif token in XYZ_OIL:
            return XYZ_THRESHOLDS["OIL"]
        elif token in XYZ_SILVER:
            return XYZ_THRESHOLDS["SILVER"]
        elif token in XYZ_METALS:
            return XYZ_THRESHOLDS["METALS"]
        elif token in XYZ_ENERGY:
            return XYZ_THRESHOLDS["ENERGY"]
        elif token in XYZ_URANIUM:
            return XYZ_THRESHOLDS["URANIUM"]
        elif token in XYZ_FOREX:
            return XYZ_THRESHOLDS["FOREX"]
        else:
            # Default for unknown xyz tokens (probably equities)
            return XYZ_THRESHOLDS["EQUITIES"]

    # Other HIP-3 sub-exchanges (flx, hyna, km) - flat threshold
    if exchange in OTHER_SUB_EXCHANGES:
        return OTHER_SUB_EXCHANGE_THRESHOLD

    # Main exchange - use token tier classification
    # Get cross threshold based on token class
    if token in MAIN_MEGA_CAP:
        cross_threshold = MAIN_THRESHOLDS_CROSS["MEGA_CAP"]
    elif token in MAIN_LARGE_CAP:
        cross_threshold = MAIN_THRESHOLDS_CROSS["LARGE_CAP"]
    elif token in MAIN_TIER1_ALTS:
        cross_threshold = MAIN_THRESHOLDS_CROSS["TIER1_ALTS"]
    elif token in MAIN_TIER2_ALTS:
        cross_threshold = MAIN_THRESHOLDS_CROSS["TIER2_ALTS"]
    elif token in MAIN_MID_ALTS:
        cross_threshold = MAIN_THRESHOLDS_CROSS["MID_ALTS"]
    else:
        cross_threshold = MAIN_THRESHOLDS_CROSS["SMALL_CAPS"]

    # Apply isolated multiplier (5:1 ratio)
    if is_isolated:
        return cross_threshold / ISOLATED_MULTIPLIER
    else:
        return cross_threshold


def passes_new_position_threshold(
    token: str,
    position_value: float,
    distance_pct: float,
    is_isolated: bool
) -> bool:
    """
    Check if a position passes the threshold for NEW POSITION alerts.

    Isolated positions get a 5x multiplier to their notional for threshold checks.

    Args:
        token: Token symbol (e.g., "BTC", "ETH", "DOGE")
        position_value: Position notional value in USD
        distance_pct: Current distance to liquidation (%)
        is_isolated: Whether the position uses isolated margin

    Returns:
        True if position should trigger a new position alert
    """
    # Apply isolated multiplier
    effective_value = position_value * ISOLATED_MULTIPLIER if is_isolated else position_value

    # Determine which tier thresholds to check based on distance
    # Check "close" tier first (≤1%), then "standard" tier (≤3%)
    tiers_to_check = []
    if distance_pct <= NEW_POSITION_THRESHOLDS["close"]["max_distance_pct"]:
        tiers_to_check.append("close")
    if distance_pct <= NEW_POSITION_THRESHOLDS["standard"]["max_distance_pct"]:
        tiers_to_check.append("standard")

    if not tiers_to_check:
        # Position is too far from liquidation (>3%)
        return False

    # Check if position meets threshold for any applicable tier
    for tier in tiers_to_check:
        thresholds = NEW_POSITION_THRESHOLDS[tier]

        # Get minimum value for this token type
        if token == "BTC":
            min_value = thresholds["btc_min_value"]
        elif token in MAJORS:
            min_value = thresholds["majors_min_value"]
        else:
            min_value = thresholds["alts_min_value"]

        if effective_value >= min_value:
            return True

    return False


def get_secondary_watchlist_threshold(token: str, exchange: str, is_isolated: bool) -> float:
    """
    Get the minimum notional threshold for secondary monitor watchlist inclusion.

    Uses the same tier-based logic as get_watchlist_threshold but applies
    SECONDARY_THRESHOLD_DIVISOR to lower all thresholds.

    Args:
        token: Token symbol (e.g., "BTC", "TSLA", "GOLD")
        exchange: Exchange name ("main", "xyz", "flx", "vntl", "hyna", "km")
        is_isolated: Whether the position uses isolated margin

    Returns:
        Minimum notional value in USD (lower than primary thresholds)
    """
    primary_threshold = get_watchlist_threshold(token, exchange, is_isolated)
    return primary_threshold / SECONDARY_THRESHOLD_DIVISOR
