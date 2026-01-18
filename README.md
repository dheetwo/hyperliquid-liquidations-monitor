# Hyperdash Large Position Scanner

Automated scanner for detecting large trader positions on Hyperliquid via Hyperdash analytics.

## Overview

This tool monitors Hyperdash.com to identify significant positions that meet configurable criteria:
- **n_1**: Major asset threshold (BTC, ETH, SOL) - default $10M
- **n_2**: Default notional threshold for other assets - default $1M  
- **x**: Percentage of Open Interest threshold - default 5%

A position is flagged if it meets **ANY** of these conditions.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Hyperdash Scanner                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────┐     ┌──────────────────┐             │
│  │  Hyperliquid API │     │  Hyperdash       │             │
│  │  (OI, Prices)    │     │  (Top Traders)   │             │
│  └────────┬─────────┘     └────────┬─────────┘             │
│           │                        │                        │
│           ▼                        ▼                        │
│  ┌──────────────────────────────────────────┐              │
│  │          Position Filter                  │              │
│  │  • Major asset threshold (n_1)           │              │
│  │  • Default threshold (n_2)               │              │
│  │  • OI percentage (x)                     │              │
│  └────────────────────┬─────────────────────┘              │
│                       │                                     │
│                       ▼                                     │
│  ┌──────────────────────────────────────────┐              │
│  │        Output Formatters                  │              │
│  │  • Console  • JSON  • CSV  • Telegram    │              │
│  └──────────────────────────────────────────┘              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### 1. Clone and Setup

```bash
cd hyperdash_scanner

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 2. Configure Thresholds

Edit `config.py` to set your thresholds:

```python
# n_1: Threshold for major assets (BTC, ETH, SOL) in USD
MAJOR_ASSET_THRESHOLD = 10_000_000  # $10M

# n_2: Threshold for all other assets in USD
DEFAULT_NOTIONAL_THRESHOLD = 1_000_000  # $1M

# x: Percentage of Open Interest threshold (as decimal)
OI_PERCENTAGE_THRESHOLD = 0.05  # 5%
```

### 3. Optional: Telegram Alerts

For Telegram notifications, set:

```python
TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID = "your_chat_id"
```

## Usage

### Basic Scan

```bash
# Scan all assets with default thresholds
python scanner.py

# Scan with visible browser (for debugging)
python scanner.py --no-headless
```

### Custom Thresholds

```bash
# Higher thresholds for majors, lower for altcoins
python scanner.py \
    --major-threshold 20000000 \
    --default-threshold 500000 \
    --oi-percentage 0.03
```

### Scan Specific Assets

```bash
# Only scan specific assets
python scanner.py --assets BTC,ETH,SOL,DOGE,PEPE
```

### Multiple Output Formats

```bash
# Output to console, JSON, and Telegram
python scanner.py --output console,json,telegram
```

### Test Mode

```bash
# Dry run with mock data (no actual scraping)
python scanner.py --dry-run
```

## Command Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--major-threshold` | $10M | n_1: Threshold for BTC/ETH/SOL |
| `--default-threshold` | $1M | n_2: Threshold for other assets |
| `--oi-percentage` | 0.05 | x: OI percentage threshold |
| `--assets` | all | Comma-separated list of assets |
| `--max-assets` | None | Limit number of assets (for testing) |
| `--output` | console | Output format(s): console,json,csv,telegram |
| `--headless` | true | Run browser in headless mode |
| `--no-headless` | - | Run browser visibly |
| `--dry-run` | - | Test mode with mock data |
| `--log-level` | INFO | DEBUG, INFO, WARNING, ERROR |

## Output Example

```
============================================================
HYPERDASH LARGE POSITION SCAN RESULTS
============================================================
Timestamp: 2025-01-14T15:30:00
Assets Scanned: 50
Positions Checked: 250
Positions Flagged: 5

------------------------------------------------------------
FLAGGED POSITIONS
------------------------------------------------------------

📊 BTC (2 positions)
   🟢 $15,000,000 (3.00% OI)
      Trader: 0x1234567890ab...
      Alert: major_asset_threshold

   🔴 $12,000,000 (2.40% OI)
      Trader: 0xabcdef123456...
      Alert: major_asset_threshold

📊 DOGE (1 position)
   🔴 $3,000,000 (6.00% OI)
      Trader: 0x9876543210fe...
      Alert: notional_threshold, oi_percentage

============================================================
```

## File Structure

```
hyperdash_scanner/
├── config.py              # Configuration and thresholds
├── models.py              # Data models (Position, FlaggedPosition, etc.)
├── hyperliquid_client.py  # Hyperliquid API client for OI data
├── scraper.py             # Playwright-based Hyperdash scraper
├── filter.py              # Position filtering logic
├── output.py              # Output formatters (console, JSON, CSV, Telegram)
├── scanner.py             # Main orchestrator
├── requirements.txt       # Python dependencies
└── README.md              # This file
```

## Important Notes

### Rate Limiting
- The scraper includes delays between asset page loads (configurable)
- Be respectful of Hyperdash's servers
- Consider running during off-peak hours for large scans

### Data Freshness
- Position data is point-in-time when scraped
- OI data from Hyperliquid API is near real-time
- For continuous monitoring, consider running as a scheduled job

### Browser Requirements
- Playwright requires Chromium to be installed
- Run `playwright install chromium` after pip install
- On headless servers, may need additional dependencies

## VPS Deployment

For running on a VPS:

```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt-get update
sudo apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0

# Set up cron job for periodic scanning
crontab -e
# Add: 0 */4 * * * /path/to/venv/bin/python /path/to/scanner.py --output json,telegram
```

## Limitations

1. **Hyperdash UI Changes**: If Hyperdash updates their UI, scraper selectors may need updating
2. **Rate Limits**: Aggressive scraping may trigger rate limits
3. **Authentication**: Some Hyperdash features may require login
4. **Data Completeness**: Top Traders tab may not show all positions

## Future Enhancements

- [ ] WebSocket-based real-time monitoring
- [ ] Historical position tracking
- [ ] Position change detection (new/closed/size changes)
- [ ] Integration with trading alerts
- [ ] Database storage for analysis

## License

MIT License - See LICENSE file
