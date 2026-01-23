# CLAUDE.md - Hyperdash Liquidation Monitor v3

## Project Objective

Monitor publicly visible positions on Hyperdash.com and alert subscribers about **liquidation status changes** on Hyperliquid perpetual futures.

### Core Goal
Provide real-time alerts when positions enter critical zones or experience liquidation events.

### Alert Types

| Alert | Description | When Triggered |
|-------|-------------|----------------|
| IMMINENT LIQUIDATION | Position critically close to liquidation | Distance < 0.125% |
| APPROACHING LIQUIDATION | Position entering danger zone | Distance < 0.25% |

## Project Structure

```
kolkata/
├── src/
│   ├── config.py               # All configuration in one place
│   ├── api/                    # Clean API clients
│   │   ├── hyperliquid.py      # Hyperliquid REST API
│   │   └── hyperdash.py        # Hyperdash GraphQL API
│   ├── db/                     # Database layer
│   │   ├── wallet_db.py        # Wallet registry (Column A - non-decreasing)
│   │   └── position_db.py      # Position cache & history
│   ├── core/                   # Core business logic
│   │   ├── wallet_filter.py    # Wallet filtering logic
│   │   ├── position_fetcher.py # Position fetching with parallelism
│   │   └── monitor.py          # Main monitoring loop
│   └── alerts/                 # Alert system
│       └── telegram.py         # Telegram bot (placeholder)
├── scripts/
│   ├── run_monitor.py          # Main entry point
│   ├── fetch_liq_channel.py    # Scheduled Telegram channel fetch
│   ├── import_liq_history.py   # One-time import from Telegram export
│   └── test_apis.py            # Test API clients
├── data/
│   ├── wallets.db              # Wallet registry
│   └── positions.db            # Position cache
├── archive/
│   ├── v1/                     # Legacy code
│   └── v2/                     # Previous version
└── tests/                      # Unit tests
```

## Quick Start

```bash
# Test API clients
python3 scripts/test_apis.py

# Run monitor (dry run - no alerts)
python3 scripts/run_monitor.py --dry-run

# Run monitor with Telegram alerts
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
python3 scripts/run_monitor.py

# Import liquidation history from Telegram export (one-time)
python3 scripts/import_liq_history.py telegram_export.json

# Add single address manually
python3 scripts/import_liq_history.py --add 0x... --notional 500000

# Fetch recent liquidations from Telegram channel (scheduled)
export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
python3 scripts/fetch_liq_channel.py --hours 1
```

## Architecture

### Data Flow

```
1. WALLET DATABASE (Column A)
   ├── Hyperdash cohorts (kraken, whale, rekt, etc.)
   ├── Telegram liquidation feed (hourly fetch)
   └── Telegram liquidation history (one-time import)
           ↓
2. WALLET FILTERING
   ├── Minimum position value: $60K
   └── Scan frequency: normal vs infrequent
           ↓
3. POSITION FETCHING
   ├── Main exchange + xyz
   ├── Notional threshold filter
   └── Parallel fetching (5 concurrent)
           ↓
4. POSITION BUCKETING
   ├── Critical: ≤0.125% to liq → refresh 0.5s
   ├── High: 0.125-0.25% → refresh 3s
   └── Normal: >0.25% → refresh 30s
           ↓
5. MONITORING LOOP
   ├── Update distances from mark prices
   ├── Check alert thresholds
   └── Send alerts (Telegram)
```

### Key Design Decisions

1. **Simple modules** - Each file does one thing well
2. **Single config file** - All settings in `src/config.py`
3. **Non-decreasing wallet registry** - Addresses only added, never removed
4. **Bucket-based monitoring** - Critical positions get most attention
5. **Async throughout** - Uses `asyncio` + `aiohttp` for performance

## Configuration

All settings in `src/config.py`:

```python
# Bucket thresholds (distance to liquidation %)
critical_distance_pct: 0.125
high_distance_pct: 0.25

# Refresh intervals (seconds)
critical_refresh_sec: 0.5
high_refresh_sec: 3.0
normal_refresh_sec: 30.0

# Notional thresholds (TESTING - lower values)
BTC: $1M (prod: $30M)
ETH: $500K
SOL: $300K
_default: $100K

# API settings
max_concurrent_requests: 5
request_delay_sec: 0.25
```

## Database Schema

### Wallet Registry (`data/wallets.db`)

```sql
wallets (
    address TEXT PRIMARY KEY,
    source TEXT,              -- 'hyperdash', 'liq_feed', or 'liq_history'
    cohort TEXT,
    position_value REAL,
    scan_frequency TEXT,      -- 'normal' or 'infrequent'
    first_seen TEXT,
    last_scanned TEXT
)
```

### Position Cache (`data/positions.db`)

```sql
positions (
    position_key TEXT PRIMARY KEY,
    address, token, exchange, side,
    size, entry_price, mark_price, liquidation_price,
    position_value, leverage, leverage_type,
    bucket TEXT,              -- 'critical', 'high', 'normal'
    distance_pct REAL,
    alerted_proximity INTEGER,
    alerted_critical INTEGER,
    last_updated TEXT
)
```

## API Reference

### Hyperliquid API

```python
# Get positions
{"type": "clearinghouseState", "user": "0x...", "dex": "xyz"}

# Get mark prices
{"type": "allMids", "dex": "xyz"}
```

### Hyperdash GraphQL

```graphql
query GetSizeCohort($id: String!, $limit: Int!, $offset: Int!) {
  analytics {
    sizeCohort(id: $id) {
      topTraders(limit: $limit, offset: $offset) {
        traders { address, accountValue, totalNotional, ... }
      }
    }
  }
}
```

## Cohorts

| Cohort | Account Size | Typical Count |
|--------|-------------|---------------|
| kraken | $5M+ | ~65 |
| large_whale | $1M-$5M | ~250 |
| whale | $250K-$1M | ~280 |
| rekt | Large losses | ~200 |
| shark | $100K-$250K | ~1400 |

## Latest Scan Stats

- **Addresses scanned**: 3,078
- **Total positions**: 14,593
- **After threshold filter**: 2,907
- **With liquidation price**: 2,442
- **Total notional**: $5.1B

## Dependencies

- `aiohttp` - Async HTTP client
- `python-dotenv` - Environment variables
- `requests` - Telegram alerts (sync)
- `telethon` - Telegram API client (for liquidation channel fetch)

## Development Guidelines

- Always use `async/await` for I/O
- Respect rate limits (5 concurrent requests, 250ms delay)
- Keep wallet registry non-decreasing
- Test with `--dry-run` before enabling alerts
