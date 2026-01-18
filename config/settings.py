"""
Configuration for Hyperdash Large Position Scanner
===================================================

Parameters:
- MAJOR_ASSET_THRESHOLD: Minimum notional (USD) for BTC, ETH, SOL positions to flag
- DEFAULT_NOTIONAL_THRESHOLD: Minimum notional (USD) for all other asset positions
- OI_PERCENTAGE_THRESHOLD: Minimum percentage of Open Interest to flag (0.05 = 5%)

The scanner will flag positions meeting ANY of these conditions:
1. BTC/ETH/SOL position >= MAJOR_ASSET_THRESHOLD
2. Any asset position >= DEFAULT_NOTIONAL_THRESHOLD
3. Any asset position >= OI_PERCENTAGE_THRESHOLD * asset's Open Interest
"""

import os

# =============================================================================
# CONFIGURABLE THRESHOLDS - Adjust these before running
# =============================================================================

# n_1: Threshold for major assets (BTC, ETH, SOL) in USD
MAJOR_ASSET_THRESHOLD = 10_000_000  # $10M default

# n_2: Threshold for all other assets in USD
DEFAULT_NOTIONAL_THRESHOLD = 1_000_000  # $1M default

# x: Percentage of Open Interest threshold (as decimal)
# 0.05 = 5% of OI, 0.01 = 1% of OI
OI_PERCENTAGE_THRESHOLD = 0.05  # 5% default

# =============================================================================
# ASSET CLASSIFICATIONS
# =============================================================================

# Assets considered "major" - use MAJOR_ASSET_THRESHOLD for these
MAJOR_ASSETS = ["BTC", "ETH", "SOL"]

# Top coins by market cap to SKIP by default (too liquid for liquidation hunting)
# These are typically too deep to move with liquidations
SKIP_TOP_MC_COINS = [
    "BTC",   # Bitcoin
    "ETH",   # Ethereum
    "XRP",   # Ripple
    "SOL",   # Solana
    "BNB",   # Binance Coin
    "DOGE",  # Dogecoin
    "ADA",   # Cardano
    "AVAX",  # Avalanche
    "LINK",  # Chainlink
    "LTC",   # Litecoin
]

# =============================================================================
# SCRAPER SETTINGS
# =============================================================================

# Hyperdash URLs
HYPERDASH_BASE_URL = "https://hyperdash.com"
HYPERDASH_ANALYTICS_URL = "https://hyperdash.info/analytics"

# Browser settings
HEADLESS_MODE = True  # Set to False for debugging to see the browser
PAGE_LOAD_TIMEOUT = 30  # seconds
ELEMENT_WAIT_TIMEOUT = 10  # seconds

# Rate limiting - be respectful to Hyperdash servers
REQUEST_DELAY = 6  # seconds between trader page loads (conservative for 200 traders)
MAX_ASSETS_TO_SCAN = None  # None = all assets, or set a number for testing

# =============================================================================
# OUTPUT SETTINGS  
# =============================================================================

# Output format options: "console", "json", "csv", "telegram"
# Always save to json and csv in addition to console output
OUTPUT_FORMAT = "console,json,csv"

# For JSON/CSV output
OUTPUT_DIR = "./data/output"
OUTPUT_FILENAME = "large_positions"

# Telegram settings (if OUTPUT_FORMAT includes "telegram")
# Set via environment variables: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# =============================================================================
# LOGGING
# =============================================================================

LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "./logs/scanner.log"
