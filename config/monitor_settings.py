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
# SCHEDULED SCAN SETTINGS (default mode)
# =============================================================================
# Times are in EST (Eastern Standard Time)
#
# Schedule:
#   - 6:30 AM EST: Comprehensive scan (baseline reset, full watchlist)
#   - Every hour (:00): Normal scan (alerts only for NEW positions since baseline)
#   - Every 30 min (:30): Priority scan (alerts only for NEW positions since baseline)
#
# Example daily schedule:
#   6:30 AM  - Comprehensive (new baseline)
#   7:00 AM  - Normal
#   7:30 AM  - Priority
#   8:00 AM  - Normal
#   ...
#   6:30 AM next day - Comprehensive (new baseline)

COMPREHENSIVE_SCAN_HOUR = 6    # Hour of comprehensive scan (24h format, EST)
COMPREHENSIVE_SCAN_MINUTE = 30  # Minute of comprehensive scan

# =============================================================================
# ASSET CLASSIFICATIONS
# =============================================================================

# Majors: Most liquid assets
MAJORS = ["ETH", "SOL", "BNB", "XRP"]

# Isolated position multiplier (isolated positions count as 5x their notional)
ISOLATED_MULTIPLIER = 5.0

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

PROXIMITY_ALERT_THRESHOLD_PCT = 5.0  # Alert when distance drops below 5%

# =============================================================================
# CRITICAL ZONE MONITORING
# =============================================================================
# Positions under CRITICAL_ZONE_PCT get priority monitoring with full position refresh.
# Alert at CRITICAL_ALERT_PCT threshold.
# Recovery alert when position goes from <CRITICAL_ZONE_PCT to >RECOVERY_PCT.

CRITICAL_ZONE_PCT = 0.2       # Positions under 0.2% get priority monitoring
CRITICAL_ALERT_PCT = 0.1      # Alert when crossing below 0.1%
RECOVERY_PCT = 0.5            # Alert when recovering from <0.2% to >0.5%

# Dynamic refresh interval for critical positions
# Scales based on number of positions to avoid rate limits
CRITICAL_REFRESH_MIN_INTERVAL = 2   # Minimum seconds (base interval)
CRITICAL_REFRESH_MAX_INTERVAL = 5   # Maximum seconds (many positions)
CRITICAL_REFRESH_SCALE_FACTOR = 0.3 # Seconds to add per position
MAX_CRITICAL_POSITIONS = 30         # Max positions to track (prioritize closest)

# =============================================================================
# WATCHLIST SETTINGS
# =============================================================================

# Minimum hunting score to include in watchlist (filters out low-priority positions)
MIN_HUNTING_SCORE = 0

# Maximum distance (%) to include in watchlist - positions farther won't be monitored
MAX_WATCH_DISTANCE_PCT = 5.0

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

# Available modes (from src/scrapers/position.py):
# - "high-priority": kraken + large_whale, main + xyz only
# - "normal": kraken + large_whale + whale, main + xyz only
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


def get_proximity_alert_threshold() -> float:
    """
    Get the proximity alert threshold.

    Returns:
        Distance threshold percentage for triggering proximity alerts.
    """
    return PROXIMITY_ALERT_THRESHOLD_PCT


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
