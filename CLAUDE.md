# CLAUDE.md - Hyperdash Liquidation Monitor

## Project Objective

Monitor publicly visible positions on Hyperdash.com and alert subscribers about **liquidation status changes** on Hyperliquid perpetual futures.

### Core Goal
Alert subscribers about:
1. **Imminent liquidations** - Positions approaching liquidation threshold
2. **Collateral additions** - When users add margin to restore position health
3. **Partial/Full liquidations** - When positions are liquidated

### Alert Types

| Alert | Description | When Triggered |
|-------|-------------|----------------|
| 🚨 IMMINENT LIQUIDATION | Position is critically close to liquidation | Distance < 0.1% |
| APPROACHING LIQUIDATION | Position entering danger zone | Distance < 0.5% |
| 💰 COLLATERAL ADDED | User added margin, position safer | Liq price moved away from current |
| ⚠️ PARTIAL LIQUIDATION | Position partially liquidated | Position value dropped >10% |
| 🔴 LIQUIDATED | Position fully liquidated | Position disappeared from API |

### What We DON'T Alert On
- **Natural price recovery** - When price moves favorably without user action (silent)

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
│   │   ├── orchestrator.py      # Main MonitorService, scheduling
│   │   ├── scan_phase.py        # Scan phase logic
│   │   ├── monitor_phase.py     # Monitor phase logic
│   │   ├── watchlist.py         # Watchlist management
│   │   ├── alerts.py            # Telegram alert system
│   │   └── database.py          # SQLite persistence
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
- Note: vntl excluded - private equity assets have no external price discovery for cascade detection
- Captures all positions with full details
- Output: `data/raw/position_data*.csv`

**Step 3: Liquidation Filter** (`src/pipeline/step3_filter.py`)
- Filters out positions without liquidation price
- Fetches current mark prices from all exchanges
- Fetches L2 order books for all unique tokens
- Calculates hunting metrics (see below)
- Output: `data/processed/filtered_*.csv`

## Cohort Definitions

| Cohort | Account Size | Priority | Typical Count | Scan Modes |
|--------|-------------|----------|---------------|------------|
| kraken | $5M+ | 1 (highest) | ~65 | all |
| large_whale | $1M-$5M | 2 | ~256 | all |
| whale | $250K-$1M | 3 | ~280 | all |
| rekt | Realized losses | 3 | varies | all |
| extremely_profitable | Large realized profits | 3 | varies | all |
| very_unprofitable | Large unrealized losses | 4 | varies | all |
| very_profitable | Realized profits | 4 | varies | all |
| profitable | Realized profits (mid-tier) | 4 | varies | all |
| unprofitable | Unrealized losses (mid-tier) | 4 | varies | all |
| shark | $100K-$250K | 5 (lowest) | ~1559 | all |

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
`Current Price, Distance to Liq (%), Estimated Liquidatable Value, Notional to Trigger, Est Price Impact (%), Hunting Score`

**Key fields:**
- `Distance to Liq (%)` - positive = price must move against position
- `Estimated Liquidatable Value` - USD forced to market (100% isolated, 20% cross)
- `Notional to Trigger` - USD to push price to liquidation (lower = easier)
- `Est Price Impact (%)` - expected slippage from liquidation (higher = more cascade potential)
- `Hunting Score` - combined metric, sorted highest first

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
| `Estimated Liquidatable Value` | USD value that will be force-liquidated | Isolated: 100% notional, Cross: 20% notional |
| `Notional to Trigger` | USD needed to push price to liquidation | Sum of order book liquidity between current and liq price |
| `Est Price Impact (%)` | Slippage when liquidation executes | Walk order book with liquidatable value |
| `Hunting Score` | Combined attractiveness metric | See formula below |

### Hunting Score Formula

```
Hunting Score = (Est Liq Value × Est Price Impact %) / (Notional to Trigger × Distance %²)
```

**Interpretation:**
- Higher score = better hunting target
- Rewards: large liquidatable value, high price impact
- Penalizes: high cost to trigger, far from liquidation

**Fallback** (when order book data missing):
```
Hunting Score = Est Liq Value / Distance %²
```

### Notional to Trigger Calculation

Walks the order book from current price to liquidation price:
- **Long position**: Sum bids between `liq_price` and `current_price` (need to sell to push down)
- **Short position**: Sum asks between `current_price` and `liq_price` (need to buy to push up)

**Known limitation**: L2 order book data is truncated (~20-50 levels). For positions with liquidation prices far from current, the value is underestimated.

### Price Impact Calculation

Simulates the forced market order when liquidation triggers:
- **Long liquidated**: Walks bids from top (forced sell)
- **Short liquidated**: Walks asks from bottom (forced buy)

Uses current book depth as proxy for liquidity at liquidation time. Extrapolates if book exhausted.

### Configurable Parameters

```python
CROSS_POSITION_LIQUIDATABLE_RATIO = 0.20  # 20% of cross-margin positions liquidated
ORDERBOOK_DELAY = 0.1                      # Rate limit for order book fetches
```

## Next Steps

**Completed:**
- [x] Multi-tier scan scheduling (6:30 comprehensive, :00 normal, :30 priority)
- [x] Baseline tracking for NEW position detection
- [x] Manual mode fallback with `--manual` flag

**TODO: Improvements**
- [ ] Extrapolate notional-to-trigger for positions far from current price
- [ ] Add cascade detection (chain of liquidations)
- [ ] Add market cap / OI percentage columns
- [ ] WebSocket for real-time price updates (reduce API polling)
- [ ] Position change detection (size increases, liq price changes)
- [ ] Historical tracking of positions across scans

## Monitor Service (`src/monitor/`)

Continuous monitoring service with **scheduled multi-tier scanning** (default) or manual fixed-interval mode.

### Scheduled Mode (Default)

Time-based scan scheduling (all times EST):

| Time | Scan Mode | Description |
|------|-----------|-------------|
| 6:30 AM | comprehensive | **Baseline scan** - full watchlist reset, alerts all qualifying positions |
| Every hour (:00) | normal | Alerts only for NEW positions since baseline |
| Every 30 min (:30) | high-priority | Fast scan, alerts only for NEW positions since baseline |

**Example daily schedule:**
```
6:30 AM  - Comprehensive (new baseline for the day)
7:00 AM  - Normal (alerts only NEW positions since 6:30)
7:30 AM  - Priority (alerts only NEW positions since 6:30)
8:00 AM  - Normal
8:30 AM  - Priority
...
6:30 AM next day - Comprehensive (new baseline)
```

**On startup:** Immediate scan runs based on current time. If no baseline exists, first scan establishes baseline.

### Manual Mode (`--manual`)

Fixed interval between scans (original behavior). Every scan is treated as a baseline.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MONITOR SERVICE (service.py)                 │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ SCAN PHASE (scheduled or fixed interval)                │   │
│  │  1. Run cohort scraper → data/raw/cohort_data.csv      │   │
│  │  2. Run position scraper (mode based on schedule)       │   │
│  │  3. Run liquidation filter                              │   │
│  │  4. Compare with BASELINE → find NEW positions          │   │
│  │  5. Alert if new high-priority positions found          │   │
│  │  6. Update watchlist (merge or replace based on mode)   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              ↓                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ MONITOR PHASE (runs until next scheduled scan)          │   │
│  │  Loop every 5 seconds:                                  │   │
│  │    1. Fetch all mark prices (1 API call)               │   │
│  │    2. For each watched position:                        │   │
│  │       - Recalculate distance to liquidation            │   │
│  │       - If distance < threshold → ALERT                │   │
│  │    3. Track positions already alerted (no duplicates)   │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Baseline vs Non-Baseline Scans

| Scan Type | Watchlist Behavior | Alert Comparison |
|-----------|-------------------|------------------|
| Baseline (comprehensive) | Full replacement | vs previous baseline |
| Non-baseline (normal/priority) | Merge new positions | vs current baseline |

### Alert Thresholds (Asset-based Tiers)

| Tier | Assets | Min Position | Alert Distance |
|------|--------|--------------|----------------|
| Tier 1 | BTC, ETH, SOL, BNB | $50M+ | 3% |
| Tier 2 | XRP, DOGE, ADA, AVAX, LINK, LTC | $20M+ | 3% |
| Alts | All others | $5M+ | 3% |
| Default | Any size | Any | 1% |

### Configuration (`config/monitor_settings.py`)

```python
# Scheduling (times in EST)
COMPREHENSIVE_SCAN_HOUR = 6    # Hour of comprehensive/baseline scan
COMPREHENSIVE_SCAN_MINUTE = 30  # Minute of comprehensive scan

# Timing
SCAN_INTERVAL_MINUTES = 90   # Time between scans (manual mode only)
POLL_INTERVAL_SECONDS = 5    # Mark price poll frequency
MAX_WATCH_DISTANCE_PCT = 15  # Max distance to include in watchlist
```

### Monitor Phase Alert Types

1. **New Position Alert** (Scan Phase)
   - Sent when new high-priority positions are detected
   - Shows token, side, value, distance, and hunting score

2. **Proximity Alert** (Monitor Phase)
   - Sent when a watched position crosses below 0.5% threshold
   - Shows current vs previous distance, liquidation price, current price

3. **Critical Alert** (Monitor Phase)
   - Sent when position crosses below 0.1% threshold (imminent liquidation)
   - Prefix: 🚨 IMMINENT LIQUIDATION

4. **Collateral Added Alert** (Monitor Phase)
   - Sent when user adds margin and liquidation price moves to safer level
   - Prefix: 💰 COLLATERAL ADDED
   - Shows old vs new liq price and distance improvement

5. **Partial Liquidation Alert** (Monitor Phase)
   - Sent when position value drops >10% (partial liquidation detected)
   - Prefix: ⚠️ PARTIAL LIQUIDATION
   - Shows old vs new position value and reduction percentage

6. **Full Liquidation Alert** (Monitor Phase)
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
# Start monitor with scheduled mode (default)
# 6:30 AM comprehensive, :00 normal, :30 priority
python scripts/run_monitor.py

# Start with manual mode (fixed 90 min interval)
python scripts/run_monitor.py --manual

# Manual mode with custom interval
python scripts/run_monitor.py --manual --interval 60

# Dry run (console alerts only, no Telegram)
python scripts/run_monitor.py --dry-run

# Test Telegram configuration
python scripts/run_monitor.py --test-telegram
```

## Scan Modes

| Mode | Cohorts | Exchanges | Use Case |
|------|---------|-----------|----------|
| high-priority | kraken, large_whale, rekt | main, xyz | Fast scan of largest + rekt traders |
| normal | kraken, large_whale, whale, rekt, extremely_profitable, very_unprofitable, very_profitable | main, xyz | Default balanced scan |
| comprehensive | all cohorts | all 6 exchanges | Full coverage, slower |

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

# Step 4: Run continuous monitor (combines all steps)
python scripts/run_monitor.py                       # Scheduled mode (default)
python scripts/run_monitor.py --manual              # Manual mode, 90min interval
python scripts/run_monitor.py --manual -i 60       # Manual mode, 60min interval
python scripts/run_monitor.py --dry-run            # Console alerts only
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

**Hyperliquid API (src/filters/liquidation.py):**
- ORDERBOOK_DELAY = 0.1s between order book fetches

**Hyperdash GraphQL:**
- 1.0s delay between cohort requests
- Supports pagination (500 per page)

## Cascade Potential & Price Impact

### Quantifying Slippage (Price Impact)
To estimate how much a liquidation moves price:
1. **Get order book depth** from Hyperliquid API
2. **Liquidation = forced market order** of size ≈ position notional
3. **Walk the book**: Sum liquidity at each price level until notional is filled
4. **Price impact** = difference between spot and final fill price

```
Example: $5M long liquidated on asset with thin book
- Spot: $1.00
- $2M liquidity at $0.99, $2M at $0.98, $3M at $0.97
- Liquidation sells $5M → fills through to $0.97
- Price impact ≈ 3%
```

### Cascade Detection
Cascades occur when one liquidation's price impact triggers another.

**Algorithm:**
1. Group positions by asset + direction (longs together, shorts together)
2. Sort by distance to liquidation (closest first)
3. For position A: estimate price impact if liquidated
4. Check if that impact reaches position B's liq price
5. If yes, add B's notional, recalculate combined impact
6. Repeat until no more positions reached

**Cascade score** = total notional of chainable liquidations

### Data Needed
- Order book depth (Hyperliquid API)
- All positions with liq prices on same asset
- Current spot price

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

### Hyperliquid Order Book API

**L2 Book (aggregated depth):**
```python
{"type": "l2Book", "coin": "ETH"}
# Response: {"levels": [[bids...], [asks...]]}
# Each level: {"px": "3500.0", "sz": "100.5", "n": 5}
```

**Price Impact Calculation:**
```python
def estimate_price_impact(order_book, liquidation_size, side):
    """
    Walk the book to estimate slippage from a liquidation.
    - Long liquidation = market SELL → walk down bids
    - Short liquidation = market BUY → walk up asks
    """
    levels = order_book["bids"] if side == "sell" else order_book["asks"]
    remaining = liquidation_size
    total_value = 0

    for level in levels:
        px, sz = float(level["px"]), float(level["sz"])
        fill = min(remaining, sz * px)  # notional fillable at this level
        total_value += fill
        remaining -= fill
        if remaining <= 0:
            return (px - spot_price) / spot_price  # % impact

    return None  # book exhausted before fill
```

## What Makes a Good Liquidation Target

| Factor | Threshold | Why It Matters |
|--------|-----------|----------------|
| Has liquidation price | Required | No liq price = can't be hunted |
| Liq price close to spot | ≤3% = high, ≤10% = watch | Closer = easier to trigger |
| Large notional | ≥ $25M | Bigger price impact |
| High % of OI | ≥ 0.025% | Liquidation moves market more |
| High % of MC | ≥ 0.05% | Significant relative to asset value |
| Cascade potential | Chain of positions | Triggers compound liquidations |

## File Locations

**Pipeline (Data Processing):**
- `src/pipeline/step1_cohort.py` - Step 1: Fetch wallet addresses
- `src/pipeline/step2_position.py` - Step 2: Fetch positions
- `src/pipeline/step3_filter.py` - Step 3: Filter and score

**Monitor Service:**
- `src/monitor/orchestrator.py` - Main MonitorService class with scheduling
- `src/monitor/scan_phase.py` - Scan phase logic
- `src/monitor/monitor_phase.py` - Monitor phase logic (price polling)
- `src/monitor/watchlist.py` - Watchlist building and management
- `src/monitor/alerts.py` - Telegram alert system
- `src/monitor/database.py` - SQLite persistence

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
