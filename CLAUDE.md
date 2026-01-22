# CLAUDE.md - Hyperdash Liquidation Monitor

## Project Objective

Monitor publicly visible positions on Hyperdash.com and alert subscribers about **liquidation status changes** on Hyperliquid perpetual futures.

### Core Goal
Provide **daily intelligence** on large positions approaching liquidation, plus **real-time alerts** when positions enter critical zones or experience liquidation events.

Alert subscribers about:
1. **Daily watchlist summary** - Once daily overview of all monitored positions (6am EST)
2. **Critical proximity alerts** - Positions entering danger zone (<0.25%)
3. **Collateral additions** - When users add margin to restore position health
4. **Partial/Full liquidations** - When positions are liquidated

### Alert Types

| Alert | Description | When Triggered |
|-------|-------------|----------------|
| 📊 DAILY SUMMARY | Overview of all monitored positions | 6am EST |
| 🚨 IMMINENT LIQUIDATION | Position critically close to liquidation | Distance < 0.125% |
| ⚠️ APPROACHING LIQUIDATION | Position entering danger zone | Distance < 0.25% |
| 💰 COLLATERAL ADDED | User added margin, position safer | Liq price moved away from current |
| ⚠️ PARTIAL LIQUIDATION | Position partially liquidated | Position value dropped >10% |
| 🔴 LIQUIDATED | Position fully liquidated | Position disappeared from API |

### What We DON'T Alert On
- **Natural price recovery** - When price moves favorably without user action (silent)
- **New position discoveries** - Added to cache silently, shown in daily summaries
- **Positions below notional thresholds** - Filtered by tier (BTC $100M cross, ETH $75M cross, etc.)

## Project Structure

```
hyperdash_scanner/
├── src/                         # Source code
│   ├── pipeline/                # Data pipeline (Steps 1-3)
│   │   ├── __init__.py          # Package exports
│   │   ├── step1_cohort.py      # Step 1: Fetch wallet addresses
│   │   ├── step2_position.py    # Step 2: Fetch positions
│   │   └── step3_filter.py      # Step 3: Filter and score
│   ├── monitor/                 # Monitor service
│   │   ├── __init__.py          # Package exports
│   │   ├── orchestrator.py      # Main MonitorService, tiered refresh
│   │   ├── cache.py             # Position cache, tier scheduling
│   │   ├── scan_phase.py        # Scan phase logic
│   │   ├── monitor_phase.py     # Monitor phase logic
│   │   ├── watchlist.py         # Watchlist management
│   │   ├── alerts.py            # Telegram alerts, daily summaries
│   │   ├── database.py          # SQLite persistence
│   │   └── liquidation_feed.py  # Telegram liquidation feed parser
│   ├── api/                     # External API clients
│   │   ├── __init__.py          # Package exports
│   │   ├── hyperliquid.py       # HyperliquidAPIClient, RateLimiter
│   │   └── orderbook.py         # L2Book, cascade detection
│   ├── utils/                   # Shared utilities
│   │   ├── __init__.py          # Package exports
│   │   ├── paths.py             # Path validation, directories
│   │   ├── csv_helpers.py       # CSV sanitization
│   │   └── prices.py            # Mark price fetching
│   ├── models/                  # Shared data models
│   │   ├── __init__.py          # Package exports
│   │   ├── position.py          # Position, WatchedPosition
│   │   └── trader.py            # CohortTrader
│   ├── scrapers/                # DEPRECATED: Use pipeline/
│   └── filters/                 # DEPRECATED: Use pipeline/
├── config/
│   ├── settings.py              # Thresholds and configuration
│   └── monitor_settings.py      # Monitor service configuration
├── data/
│   ├── raw/                     # Direct API outputs
│   │   ├── cohort_data*.csv
│   │   └── position_data*.csv
│   └── processed/               # Filtered/scored outputs
│       └── filtered_*.csv
├── scripts/                     # CLI entry points
│   ├── scan_cohorts.py
│   ├── scan_positions.py
│   ├── filter_positions.py
│   └── run_monitor.py           # Continuous monitor service
├── docs/
│   └── pipeline-flowchart.md    # Visual data pipeline flow
├── archive/                     # Legacy v1 code
└── logs/
```

## Data Pipeline

**Step 1: Cohort Scraper** (`src/pipeline/step1_cohort.py`)
- Source: `https://api.hyperdash.com/graphql` (GetSizeCohort query)
- Fetches wallet addresses grouped by account size and PnL
- Priority order: kraken → large_whale → whale → rekt → shark → very_unprofitable
- Output: `data/raw/cohort_data*.csv`

**Step 2: Position Scraper** (`src/pipeline/step2_position.py`)
- Source: `https://api.hyperliquid.xyz/info` (clearinghouseState)
- Scans 5 exchanges per wallet: main + 4 sub-exchanges (xyz, flx, hyna, km)
- Note: vntl excluded - private equity assets have no external price discovery
- Captures all positions with full details
- Output: `data/raw/position_data*.csv`

**Step 3: Liquidation Filter** (`src/pipeline/step3_filter.py`)
- Filters out positions without liquidation price
- Fetches current mark prices from all exchanges
- Calculates distance to liquidation
- Sorts results by distance (closest first)
- Output: `data/processed/filtered_*.csv`

## Cohort Definitions

| Cohort | Account Size | Priority | Typical Count | Scan Modes |
|--------|-------------|----------|---------------|------------|
| kraken | $5M+ | 1 (highest) | ~65 | all |
| large_whale | $1M-$5M | 2 | ~256 | all |
| whale | $250K-$1M | 3 | ~280 | all |
| rekt | Large realized losses | 3 | varies | all |
| extremely_profitable | Large realized profits | 3 | varies | all |
| very_unprofitable | Large unrealized losses | 4 | varies | all |
| very_profitable | Large unrealized profits | 4 | varies | all |
| profitable | Realized profits | 4 | varies | all |
| unprofitable | Unrealized losses | 4 | varies | all |
| shark | $100K-$250K | 5 (lowest) | ~1559 | all |

### Wallet Filtering
- **Minimum wallet value**: $300K total position value (skip low-value wallets)
- **Leverage filter**: Exclude wallets with leverage ≤1.0 AND long-only bias (no liquidation risk)
- **Short/neutral wallets**: Always included regardless of leverage (can still be liquidated)

## Exchange Coverage

| Exchange | DEX Param | Description | Margin Type |
|----------|-----------|-------------|-------------|
| Main | "" | Primary Hyperliquid perps | Cross/Isolated |
| xyz (TradeXYZ) | "xyz" | Stocks, indices, commodities | **All Isolated** |
| flx (Felix) | "flx" | Select perps | **All Isolated** |
| hyna (Hyena) | "hyna" | Sub-exchange | **All Isolated** |
| km | "km" | Sub-exchange | **All Isolated** |

**Excluded:** vntl (private equity - ANTHROPIC, OPENAI, SPACEX) has no external price discovery, making cascade detection impossible.

### Why Sub-Exchange Positions Matter
- **Isolated margin** = liquidation affects only that position (no cross-collateral buffer)
- **Thinner liquidity** = larger price impact per dollar liquidated
- **Slower convergence** = especially equities with no weekend markets

## Output CSV Columns

### cohort_data.csv
`Address, Perp Equity, Perp Bias, Position Value, Leverage, Sum UPNL, PNL Cohort, Cohort`

### position_data.csv
`Address, Token, Side, Size, Leverage, Leverage Type, Entry Price, Mark Price, Position Value, Unrealized PnL, ROE, Liquidation Price, Margin Used, Funding (Since Open), Cohort, Exchange, Isolated`

**Key fields for filtering:**
- `Liquidation Price` - empty if none (safe for numeric ops)
- `Mark Price` - current price at scan time
- `Position Value` - notional size in USD
- `Exchange` - "main", "xyz", "flx", "hyna", or "km"
- `Isolated` - True/False (all sub-exchange = True)

### filtered_position_data.csv
All columns from position_data.csv plus:
`Current Price, Distance to Liq (%)`

**Key fields:**
- `Distance to Liq (%)` - positive = price must move against position (sorted closest first)

## Current Stats (Latest Scan)

**Priority Cohorts (kraken + large_whale + whale):**
- Addresses: 601
- Positions: 3,715
- Main exchange: 3,519
- Sub-exchange (xyz): 179
- Sub-exchange (flx): 17
- Isolated positions: 434 (12%)
- With liquidation price: 2,672 (72%)

## Liquidation Filter (`src/pipeline/step3_filter.py`)

### Calculated Columns

| Column | Description | Formula |
|--------|-------------|---------|
| `Current Price` | Live mark price at filter time | From `allMids` API |
| `Distance to Liq (%)` | How far price must move to trigger | `(current - liq) / current * 100` for longs |

Results are sorted by distance to liquidation (closest first).

## Next Steps

**Completed:**
- [x] Cache-based monitoring with tiered refresh (replaces scheduled scans)
- [x] SQLite persistence for positions (survive restarts)
- [x] Daily summary alert at 6am EST
- [x] Notional threshold filtering by token tier
- [x] Position change detection (collateral adds, partial/full liquidations)
- [x] Removed order book fetching for faster scans (~7-12 min total)
- [x] Wallet filtering (leverage, bias, minimum $300K value)
- [x] Added profitable/unprofitable PnL cohorts
- [x] Liquidation history tracking (recidivist monitoring from Telegram feeds)
- [x] Fixed xyz token prefix bug in threshold lookups

**TODO: Improvements**
- [ ] Add cascade detection (chain of liquidations via order book)
- [ ] Add market cap / OI percentage columns
- [ ] WebSocket for real-time price updates (reduce API polling)
- [ ] Historical tracking of positions across scans
- [ ] Automated Telegram bot for liquidation feed ingestion

## Development Guidelines

### Coding Patterns
- **SQLite Logging:** Use `SQLiteLoggingHandler` with background thread for batch writes to avoid blocking main application
- **Database Schema:** Use WAL mode for concurrent reads, proper indexing for query performance
- **Token Prefix Handling:** API returns `xyz:SILVER` but config uses `SILVER` - always handle both formats when looking up thresholds
- **Progressive Startup:** Use scan modes (`whale-only`, `shark-incremental`) to gradually increase coverage

### Best Practices
- **Tiered Refresh:** Classify by liquidation distance - critical positions need frequent updates, normal positions can refresh slowly
- **Database for State:** SQLite persistence (`MonitorDatabase`) ensures state survives restarts
- **Error Handling:** Gracefully handle network errors, rate limits, unexpected API responses - use cached data as fallback
- **Async Operations:** Use `aiohttp` and `asyncio` for concurrent data fetching to improve performance
- **Data Validation:** Validate file paths (`validate_file_path`) before operations

### Common Pitfalls
- **Token Naming Mismatch:** `xyz:TOKEN` prefix in API vs `TOKEN` in config - strip prefixes when needed
- **Cohort Gaps:** Hyperdash cohorts can miss accounts - supplement with liquidation history tracking
- **Over-reliance on Cohorts:** Some $5M+ accounts may not appear in any cohort - use alternative discovery methods

### Data Protection
- **Wallet addresses are valuable:** Never clear `known_addresses`, `cohort_cache`, `wallet_registry`, or `liquidation_history.db` without explicit request
- **Use `--clear-cache`** instead of `--clear-db` for normal cache rebuilds
- **Liquidation history** is stored separately in `data/liquidation_history.db` and accumulates over time
- **Wallet registry** is non-decreasing - wallets are only added, never removed (Column A database)

## Wallet Registry (Column A - Non-Decreasing)

The wallet registry (`wallet_registry` table) is the unified source of all known wallet addresses:

### Sources
- **Hyperdash cohorts**: Wallets from size/PnL cohorts with full metadata
- **Liquidation history**: Wallets from Telegram feed with liquidation event data

### Scan Frequency Classification
Wallets are classified based on their position value from the most recent scan:
- **Normal frequency**: `position_value >= WALLET_ACTIVE_THRESHOLD` ($60K) → scanned every discovery cycle
- **Infrequent**: `position_value < WALLET_ACTIVE_THRESHOLD` → scanned every `INFREQUENT_SCAN_INTERVAL_HOURS` (24h)
- **Never scanned**: `last_scanned IS NULL` → always scanned next cycle

### Schema
```sql
wallet_registry (
    address TEXT PRIMARY KEY,
    source TEXT,              -- 'hyperdash' or 'liq_history'
    cohort TEXT,              -- cohort name if from hyperdash
    position_value REAL,      -- NULL until first scan
    total_collateral REAL,
    position_count INTEGER,
    scan_frequency TEXT,      -- 'normal' or 'infrequent'
    first_seen TEXT,
    last_scanned TEXT,
    scan_count INTEGER
)
```

### Scan Snapshots
Comprehensive scans are logged to `scan_snapshots` table for auditing:
- `scan_type`: 'comprehensive', 'discovery', 'infrequent'
- `total_wallets_scanned`, `positions_found`, `total_position_value`
- `scan_duration_seconds`
- Enables tracking of scan efficiency over time

## Monitor Service (`src/monitor/`)

Continuous cache-based monitoring service with tiered refresh scheduling.

### Architecture: Cache-Based Monitoring

```
┌─────────────────────────────────────────────────────────────────┐
│                    MONITOR SERVICE (orchestrator.py)            │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ INITIAL SCAN (on startup)                               │   │
│  │  1. Load cached positions from SQLite (if fresh)        │   │
│  │  2. Or run comprehensive scan (all cohorts, all dexes)  │   │
│  │  3. Populate position cache with tier classification    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              ↓                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ TIERED REFRESH LOOP (continuous)                        │   │
│  │  Based on distance to liquidation:                      │   │
│  │    - Critical (≤0.125%): ~5 req/sec continuous         │   │
│  │    - High (0.125-0.25%): Every 2-3 seconds             │   │
│  │    - Normal (>0.25%): Every 30 seconds                 │   │
│  │                                                         │   │
│  │  On each refresh:                                       │   │
│  │    - Fetch position data from API                       │   │
│  │    - Recalculate distance, detect state changes         │   │
│  │    - Alert on proximity thresholds, collateral, liqs    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              ↓                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ DISCOVERY SCHEDULER (dynamic interval)                  │   │
│  │  - Scans for new addresses/positions                    │   │
│  │  - Interval adapts to API pressure (30min - 4hr)       │   │
│  │  - More critical positions = longer discovery interval  │   │
│  │  - New positions added silently to cache                │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              ↓                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ DAILY SUMMARY (6am EST)                                 │   │
│  │  - Lists all monitored positions by tier                │   │
│  │  - Shows token, side, value, distance, liq price        │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Position Cache Tiers

| Tier | Distance | Refresh Rate | Purpose |
|------|----------|--------------|---------|
| Critical | ≤0.125% | ~5 req/sec | Imminent liquidation detection |
| High | 0.125-0.25% | Every 2-3 sec | Approaching liquidation monitoring |
| Normal | >0.25% | Every 30 sec | Background tracking |

Positions are automatically promoted/demoted between tiers as distance changes.

### Configuration (`config/monitor_settings.py`)

```python
# Cache tier thresholds (distance to liquidation %)
CACHE_TIER_CRITICAL_PCT = 0.125   # ≤0.125% = critical tier
CACHE_TIER_HIGH_PCT = 0.25        # 0.125-0.25% = high tier

# Tier refresh intervals (seconds)
CACHE_REFRESH_CRITICAL_SEC = 0.2  # ~5 req/sec
CACHE_REFRESH_HIGH_SEC = 2.5      # Every 2-3 seconds
CACHE_REFRESH_NORMAL_SEC = 30.0   # Every 30 seconds

# Discovery scheduling
DISCOVERY_MIN_INTERVAL_MINUTES = 30   # Minimum between discoveries
DISCOVERY_MAX_INTERVAL_MINUTES = 240  # Maximum (4 hours)

# Daily summary time (EST)
DAILY_SUMMARY_TIMES = [(6, 0)]  # 6am EST

# Alert thresholds
PROXIMITY_ALERT_THRESHOLD_PCT = 0.25  # Approaching liquidation
CRITICAL_ALERT_PCT = 0.125            # Imminent liquidation

# Watchlist filtering
MAX_WATCH_DISTANCE_PCT = 5.0  # Max distance to include
MIN_WALLET_POSITION_VALUE = 60_000  # Skip wallets below $60K total value
```

### Watchlist Notional Thresholds

Positions must meet minimum size requirements to be monitored. Isolated positions use 5x lower thresholds.
Thresholds based on order book liquidity analysis (Jan 2025), raised to reduce alert volume.

**Main Exchange (Cross / Isolated):**

| Tier | Tokens | Cross Threshold | Isolated Threshold |
|------|--------|-----------------|-------------------|
| Mega Cap | BTC, ETH | $30M | $6M |
| Large Cap | SOL | $20M | $4M |
| Tier 1 Alts | DOGE, XRP, HYPE | $10M | $2M |
| Tier 2 Alts | BNB | $2M | $400K |
| Mid Alts | ADA, AVAX, LINK, SUI, AAVE, etc. | $1M | $200K |
| Low Alts | APT, ARB, memes, DeFi | $500K | $100K |
| Small Caps | Everything else | $300K | $60K |

**XYZ Exchange (All Isolated):**

| Category | Tokens | Threshold |
|----------|--------|-----------|
| Indices | XYZ100 | $2M |
| High Liq Equities | NFLX, NVDA, TSLA, GOOGL, AMZN, META, AAPL, MSFT, COIN, MSTR, etc. | $1M |
| Low Liq Equities | BABA, CRCL, HOOD, etc. | $500K |
| Gold | GOLD | $1M |
| Silver | SILVER | $1M |
| Oil | CL | $600K |
| Forex | EUR, JPY | $1M |
| Metals | COPPER | $400K |
| Energy | NATGAS | $300K |
| Uranium | URANIUM | $200K |

**Other Sub-Exchanges (flx, hyna, km):** $400K flat threshold

### Monitor Alert Types

1. **Daily Summary Alert**
   - Sent at 6am EST
   - Lists all monitored positions grouped by tier
   - Shows token, side, value, distance, liquidation price

2. **Approaching Liquidation Alert**
   - Sent when position crosses below 0.25% threshold
   - Shows current vs previous distance, liquidation price, current price

3. **Imminent Liquidation Alert**
   - Sent when position crosses below 0.125% threshold
   - Prefix: 🚨 IMMINENT LIQUIDATION

4. **Collateral Added Alert**
   - Sent when user adds margin and liquidation price moves to safer level
   - Prefix: 💰 COLLATERAL ADDED
   - Shows old vs new liq price and distance improvement

5. **Partial Liquidation Alert**
   - Sent when position value drops >10% (partial liquidation detected)
   - Prefix: ⚠️ PARTIAL LIQUIDATION
   - Shows old vs new position value and reduction percentage

6. **Full Liquidation Alert**
   - Sent when position disappears from API (fully liquidated)
   - Prefix: 🔴 LIQUIDATED

**Note:** Natural price recovery (price moving favorably without user action) does NOT trigger alerts.

### Environment Variables

```bash
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

### Monitor Commands

```bash
# Start monitor (cache-based, continuous)
python scripts/run_monitor.py

# Clear cache only (RECOMMENDED - preserves wallet addresses)
python scripts/run_monitor.py --clear-cache

# Clear ALL database including wallet addresses (use sparingly)
python scripts/run_monitor.py --clear-db

# Dry run (console alerts only, no Telegram)
python scripts/run_monitor.py --dry-run

# Custom poll interval
python scripts/run_monitor.py --poll 10

# Debug logging
python scripts/run_monitor.py --log-level DEBUG

# Test Telegram configuration
python scripts/run_monitor.py --test-telegram
```

### Database Protection Policy

**Wallet address data should be non-decreasing** - accumulated address databases are valuable and should not be cleared except explicitly requested.

| Flag | What it clears | What it preserves |
|------|---------------|-------------------|
| `--clear-cache` | position_cache, watchlist, baseline | known_addresses, cohort_cache, liquidation_history.db |
| `--clear-db` | ALL tables in monitor.db | liquidation_history.db (separate database) |

**Two separate databases:**
- `data/monitor.db` - Position cache, watchlist, known_addresses, cohort_cache
- `data/liquidation_history.db` - Liquidation events from Telegram feeds (never cleared by --clear-db)

## Scan Modes (Pipeline CLI Only)

These modes apply to the standalone pipeline scripts (`scan_positions.py`), not the monitor service.
The monitor service uses cache-based continuous monitoring instead.

| Mode | Cohorts | Exchanges | Use Case |
|------|---------|-----------|----------|
| high-priority | kraken, large_whale, rekt | main, xyz | Fast scan of largest + rekt traders |
| normal | all cohorts | main, xyz | Default balanced scan |
| comprehensive | all cohorts | all 5 exchanges | Full coverage, slower |

## Liquidation History (Recidivist Tracking)

The monitor tracks addresses that have been liquidated in the past via Telegram liquidation feeds.
This supplements Hyperdash cohort discovery for traders who may not be in any cohort but have
significant position sizes.

### Data Source

- Telegram channel: `@liquidations_hyperliquid`
- Message format: `🔴 #BTC Long Liquidation: $1.15M @ $88,827.1 [scan][dash]`
- Links contain wallet addresses: `https://hypurrscan.io/address/0x...`

### Database

- Location: `data/liquidation_history.db`
- Tables:
  - `liquidation_history`: Raw liquidation events
  - `liquidated_addresses`: Aggregated view with max notional per address

### CLI Management

```bash
# View statistics
python scripts/manage_liq_history.py stats

# Import from Telegram channel export (JSON)
python scripts/manage_liq_history.py import exported_channel.json

# Add a liquidation manually
python scripts/manage_liq_history.py add \
  --address 0x3bcae23e8c380dab4732e9a159c0456f12d866f3 \
  --token xyz:SILVER --notional 1950000 --price 96.07 \
  --side Short --exchange xyz

# Search for an address
python scripts/manage_liq_history.py search 0x3bcae23e8c380dab4732e9a159c0456f12d866f3

# List recidivists (addresses liquidated 2+ times)
python scripts/manage_liq_history.py recidivists --min-liqs 2
```

### Integration with Monitor

Addresses from liquidation history are automatically included in discovery scans if they
have been liquidated with notional >= $100K. They are assigned the special cohort `liq_history`
and scanned alongside regular cohort addresses.

## Common Commands

```bash
# Step 1: Fetch cohort data (wallet addresses)
python scripts/scan_cohorts.py
# Output: data/raw/cohort_data*.csv

# Step 2: Fetch position data (choose scan mode)
python scripts/scan_positions.py                       # Normal mode (default)
python scripts/scan_positions.py --mode high-priority  # Fast: kraken + large_whale + rekt, main + xyz
python scripts/scan_positions.py --mode comprehensive  # Full: all cohorts, all exchanges
python scripts/scan_positions.py -m normal -o out.csv  # Custom output
# Output: data/raw/position_data_{mode}.csv

# Step 3: Filter and score positions for hunting
python scripts/filter_positions.py                                    # Default: priority positions
python scripts/filter_positions.py data/raw/position_data.csv         # Filter all positions
python scripts/filter_positions.py -o data/processed/custom.csv       # Custom output
# Output: data/processed/filtered_*.csv

# Alternative: Run modules directly
python -m src.scrapers.cohort
python -m src.scrapers.position --mode high-priority
python -m src.filters.liquidation

# Run continuous monitor (cache-based, replaces pipeline steps)
python scripts/run_monitor.py              # Start monitor service
python scripts/run_monitor.py --clear-db   # Clear database and start fresh
python scripts/run_monitor.py --dry-run    # Console alerts only
python scripts/run_monitor.py --poll 10    # Custom poll interval (seconds)
```

## Dependencies

- `requests` - HTTP client for Hyperliquid/Hyperdash APIs
- `pytz` - Timezone handling for scheduled scans (EST)
- `python-dotenv` - Environment variable loading
- `playwright` - Browser automation (optional, for debugging)

## Rate Limiting

**Hyperliquid API (src/scrapers/position.py):**
- REQUEST_DELAY = 0.2s between calls
- BATCH_DELAY = 2.0s every 50 addresses
- DEX_DELAY = 0.1s between dex queries for same address

**Hyperdash GraphQL:**
- 1.0s delay between cohort requests
- Supports pagination (500 per page)

## Cascade Potential & Price Impact (Future)

> **Note:** Order book fetching was removed in v2 to speed up scans (~20 min → ~30 sec for Step 3).
> These features are planned for future implementation.

### Cascade Detection (TODO)
Cascades occur when one liquidation's price impact triggers another.

**Planned algorithm:**
1. Group positions by asset + direction (longs together, shorts together)
2. Sort by distance to liquidation (closest first)
3. For position A: estimate price impact if liquidated
4. Check if that impact reaches position B's liq price
5. If yes, add B's notional, recalculate combined impact
6. Repeat until no more positions reached

## API Reference

### Hyperdash GraphQL API

**Endpoint:** `https://api.hyperdash.com/graphql`

**GetSizeCohort Query:**
```python
# Fetch traders by size cohort
query = """
query GetSizeCohort($id: String!, $limit: Int!, $offset: Int!) {
  analytics {
    sizeCohort(id: $id) {
      totalTraders
      topTraders(limit: $limit, offset: $offset) {
        totalCount
        hasMore
        traders { address, accountValue, perpPnl, totalNotional, ... }
      }
    }
  }
}
"""
# cohort ids: "kraken", "large_whale", "whale", "rekt", "extremely_profitable", "shark", "very_unprofitable", "very_profitable"
```

### Hyperliquid API

**Endpoint:** `https://api.hyperliquid.xyz/info`

**Get positions (with sub-exchange support):**
```python
# Main exchange
{"type": "clearinghouseState", "user": "0x..."}

# Sub-exchange (xyz, flx, hyna, km)
{"type": "clearinghouseState", "user": "0x...", "dex": "xyz"}
```

**Get mark prices:**
```python
# Main exchange
{"type": "allMids"}

# Sub-exchange
{"type": "allMids", "dex": "xyz"}
```

**Get asset metadata:**
```python
{"type": "meta", "dex": "xyz"}  # Returns universe with onlyIsolated, maxLeverage, etc.
```

### Hyperliquid Order Book API (Not Currently Used)

> **Note:** Order book fetching was removed in v2 for faster scans. API reference kept for future cascade detection.

**L2 Book (aggregated depth):**
```python
{"type": "l2Book", "coin": "ETH"}
# Response: {"levels": [[bids...], [asks...]]}
# Each level: {"px": "3500.0", "sz": "100.5", "n": 5}
```

## Position Monitoring Criteria

| Factor | Threshold | Why It Matters |
|--------|-----------|----------------|
| Has liquidation price | Required | No liq price = no liquidation risk |
| Distance to liquidation | ≤5% to monitor | Closer = higher priority |
| Position notional | Token-tier based | Large positions have market impact |
| Margin type | Cross or Isolated | Isolated = 100% liquidated, Cross = partial |

## File Locations

**Pipeline (Data Processing):**
- `src/pipeline/step1_cohort.py` - Step 1: Fetch wallet addresses
- `src/pipeline/step2_position.py` - Step 2: Fetch positions
- `src/pipeline/step3_filter.py` - Step 3: Filter and score

**Monitor Service:**
- `src/monitor/orchestrator.py` - Main MonitorService class, tiered refresh loop
- `src/monitor/cache.py` - CachedPosition, PositionCache, TieredRefreshScheduler, DiscoveryScheduler
- `src/monitor/scan_phase.py` - Scan phase logic
- `src/monitor/monitor_phase.py` - Monitor phase logic (price polling)
- `src/monitor/watchlist.py` - Watchlist building and management
- `src/monitor/alerts.py` - Telegram alert system, daily summaries
- `src/monitor/database.py` - SQLite persistence (positions, addresses, logs)

**API Clients:**
- `src/api/hyperliquid.py` - HyperliquidAPIClient, RateLimiter
- `src/api/orderbook.py` - L2Book, cascade detection

**Shared Utilities:**
- `src/utils/paths.py` - Path validation, project directories
- `src/utils/csv_helpers.py` - CSV sanitization
- `src/utils/prices.py` - Mark price fetching

**Shared Models:**
- `src/models/position.py` - Position, WatchedPosition dataclasses
- `src/models/trader.py` - CohortTrader dataclass

**Configuration:**
- `config/settings.py` - Thresholds and constants
- `config/monitor_settings.py` - Monitor service configuration

**CLI Entry Points:**
- `scripts/scan_cohorts.py` - Run Step 1
- `scripts/scan_positions.py` - Run Step 2
- `scripts/filter_positions.py` - Run Step 3
- `scripts/run_monitor.py` - Continuous monitor service

**Data - Raw (API outputs):**
- `data/raw/cohort_data_priority.csv` - Kraken, Large Whale, Whale addresses
- `data/raw/cohort_data_shark.csv` - Shark addresses
- `data/raw/cohort_data.csv` - All cohorts
- `data/raw/position_data_priority.csv` - Priority cohort positions
- `data/raw/position_data_shark.csv` - Shark positions
- `data/raw/position_data.csv` - All positions

**Data - Processed (filtered/scored):**
- `data/processed/filtered_position_data_priority.csv`
- `data/processed/filtered_position_data.csv`

**Deprecated (backward compatible shims):**
- `src/scrapers/` - Re-exports from `src/pipeline/`
- `src/filters/` - Re-exports from `src/pipeline/`
- `src/monitor/service.py` - Re-exports from `src/monitor/orchestrator.py`

**Legacy (archived):**
- `archive/` - Old v1 architecture (filter.py, scraper.py, scanner.py, models.py, output.py)
