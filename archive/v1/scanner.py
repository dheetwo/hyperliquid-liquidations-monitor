#!/usr/bin/env python3
"""
Hyperdash Large Position Scanner
=================================

Main entry point for scanning Hyperdash for large trader positions.

Uses the terminal page approach:
    https://legacy.hyperdash.com/terminal?ticker={TICKER}

This scans ALL positions per ticker (not just top traders).

Usage:
    python scanner.py [options]

Options:
    --major-threshold    n_1: Threshold for BTC/ETH/SOL in USD (default: $10M)
    --default-threshold  n_2: Threshold for other assets in USD (default: $1M)
    --oi-percentage      x: OI percentage threshold as decimal (default: 0.05 = 5%)
    --max-tickers        Maximum number of tickers to scan (default: all)
    --output             Output format: console,json,csv,telegram (default: console,json,csv)
    --headless           Run browser in headless mode (default: true)
    --dry-run            Test mode - don't actually scrape, use mock data

Examples:
    # Scan all assets with default thresholds
    python scanner.py

    # Custom thresholds
    python scanner.py --major-threshold 20000000 --oi-percentage 0.03

    # Scan specific assets only
    python scanner.py --assets BTC,ETH,SOL,DOGE

    # Scan top 20 tickers by OI
    python scanner.py --max-tickers 20
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import List, Optional

from models import Position, PositionSide, ScanResult, FlaggedPosition, LiquidationProximitySummary
from hyperliquid_client import HyperliquidAPIClient
from scraper import HyperdashScraper, TerminalPosition
from filter import PositionFilter, FilterThresholds, create_filter_from_params, ensure_asset_coverage, calculate_liquidation_proximity, calculate_dynamic_min_notional
from output import create_formatters
from config import (
    MAJOR_ASSET_THRESHOLD,
    DEFAULT_NOTIONAL_THRESHOLD,
    OI_PERCENTAGE_THRESHOLD,
    MAJOR_ASSETS,
    SKIP_TOP_MC_COINS,
    HEADLESS_MODE,
    REQUEST_DELAY,
    MAX_ASSETS_TO_SCAN,
    OUTPUT_FORMAT,
    OUTPUT_DIR,
    OUTPUT_FILENAME,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    LOG_LEVEL,
    LOG_FILE
)

# Set up logging
def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """Configure logging"""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=log_format,
        handlers=handlers
    )

logger = logging.getLogger(__name__)


class HyperdashScanner:
    """
    Main scanner class that orchestrates:
    1. Fetching OI data from Hyperliquid API
    2. Scraping position data from Hyperdash terminal pages
    3. Filtering positions based on thresholds
    4. Outputting results
    """

    def __init__(
        self,
        major_threshold: float = MAJOR_ASSET_THRESHOLD,
        default_threshold: float = DEFAULT_NOTIONAL_THRESHOLD,
        oi_percentage: float = OI_PERCENTAGE_THRESHOLD,
        headless: bool = HEADLESS_MODE,
        request_delay: float = REQUEST_DELAY,
        min_liq_notional: float = 500_000,
        max_liq_proximity: float = 0.10,
        prefilter_oi_pct: float = 0.005,
        prefilter_floor: float = 500_000,
    ):
        self.filter = create_filter_from_params(
            major_threshold=major_threshold,
            default_threshold=default_threshold,
            oi_percentage=oi_percentage
        )
        self.headless = headless
        self.request_delay = request_delay
        self.min_liq_notional = min_liq_notional
        self.max_liq_proximity = max_liq_proximity
        self.prefilter_oi_pct = prefilter_oi_pct
        self.prefilter_floor = prefilter_floor

        self.hl_client = HyperliquidAPIClient()
        self.errors: List[str] = []

    async def load_market_data(self) -> dict:
        """Load OI and market data from Hyperliquid API"""
        logger.info("Loading market data from Hyperliquid...")

        try:
            assets = self.hl_client.get_meta_and_asset_contexts()

            # Cache OI in filter
            oi_data = {name: asset.open_interest for name, asset in assets.items()}
            self.filter.bulk_set_open_interest(oi_data)

            logger.info(f"Loaded data for {len(assets)} assets")
            return assets
        except Exception as e:
            error_msg = f"Failed to load market data: {e}"
            logger.error(error_msg)
            self.errors.append(error_msg)
            return {}

    def _convert_scraped_position(
        self,
        scraped: TerminalPosition,
        oi: Optional[float] = None
    ) -> Position:
        """Convert scraped position to internal Position model"""
        return Position(
            trader_address=scraped.trader_address,
            asset=scraped.asset,
            side=PositionSide.LONG if scraped.side == "long" else PositionSide.SHORT,
            notional_usd=scraped.notional_usd,
            size=scraped.size,
            entry_price=scraped.entry_price,
            current_price=scraped.current_price,
            liquidation_price=scraped.liquidation_price,
            unrealized_pnl=scraped.pnl,
            asset_open_interest=oi
        )

    def _calculate_liquidation_summary(self, positions: List[Position]) -> LiquidationProximitySummary:
        """Calculate summary of liquidation proximity across all positions"""
        within_5pct = 0
        within_10pct = 0
        within_50pct = 0
        closest = None
        closest_proximity = float('inf')

        for pos in positions:
            proximity = calculate_liquidation_proximity(pos)
            if proximity is None:
                continue

            if proximity <= 0.05:
                within_5pct += 1
            if proximity <= 0.10:
                within_10pct += 1
            if proximity <= 0.50:
                within_50pct += 1

            if proximity < closest_proximity:
                closest_proximity = proximity
                closest = {
                    "asset": pos.asset,
                    "side": pos.side.value,
                    "notional_usd": pos.notional_usd,
                    "proximity_pct": round(proximity * 100, 2),
                    "current_price": pos.current_price,
                    "liquidation_price": pos.liquidation_price
                }

        return LiquidationProximitySummary(
            positions_within_5pct=within_5pct,
            positions_within_10pct=within_10pct,
            positions_within_50pct=within_50pct,
            closest_position=closest
        )

    async def run(
        self,
        assets: Optional[List[str]] = None,
        max_tickers: Optional[int] = MAX_ASSETS_TO_SCAN,
        skip_coins: Optional[List[str]] = None,
    ) -> ScanResult:
        """
        Run a complete scan using terminal page approach.

        Workflow:
        1. Load OI data from Hyperliquid API
        2. Get list of top tickers by OI
        3. For each ticker, scrape terminal page for ALL positions
        4. Filter positions against thresholds

        Args:
            assets: List of specific assets to scan (None = top by OI)
            max_tickers: Maximum number of tickers to scan
            skip_coins: List of coins to skip (e.g., top by market cap)

        Returns:
            ScanResult with flagged positions
        """
        start_time = datetime.now(timezone.utc)
        self.errors = []

        # Load market data
        market_data = await self.load_market_data()
        if not market_data:
            return ScanResult(
                timestamp=start_time,
                assets_scanned=0,
                total_positions_checked=0,
                flagged_positions=[],
                errors=self.errors
            )

        print(self.filter.summary())

        # Determine which tickers to scan
        if assets:
            # Use specified assets
            tickers_to_scan = [a.upper() for a in assets]
        else:
            # Get top tickers by OI
            sorted_by_oi = sorted(
                market_data.items(),
                key=lambda x: x[1].open_interest,
                reverse=True
            )
            tickers_to_scan = [name for name, _ in sorted_by_oi]

        # Skip specified coins (e.g., top by market cap - too liquid)
        if skip_coins:
            skip_set = {c.upper() for c in skip_coins}
            skipped = [t for t in tickers_to_scan if t in skip_set]
            tickers_to_scan = [t for t in tickers_to_scan if t not in skip_set]
            if skipped:
                logger.info(f"Skipping {len(skipped)} coins (too liquid): {', '.join(skipped)}")

        # Apply max_tickers limit
        if max_tickers:
            tickers_to_scan = tickers_to_scan[:max_tickers]

        logger.info(f"Will scan {len(tickers_to_scan)} tickers")

        # Calculate dynamic min notional for pre-filtering
        oi_data = {name: asset.open_interest for name, asset in market_data.items()}
        min_notional = calculate_dynamic_min_notional(
            oi_data=oi_data,
            oi_percentage=self.prefilter_oi_pct,
            floor=self.prefilter_floor,
        )
        logger.info(f"Pre-filter min notional: ${min_notional:,.0f}")

        # Scrape positions from terminal pages
        all_positions: List[Position] = []

        async with HyperdashScraper(headless=self.headless) as scraper:
            logger.info(f"Scraping positions from {len(tickers_to_scan)} ticker terminal pages...")
            scraped = await scraper.get_positions_for_tickers(
                tickers=tickers_to_scan,
                min_notional=min_notional,
                delay=self.request_delay
            )

            # Convert to internal format
            for sp in scraped:
                oi = self.filter.get_open_interest(sp.asset)
                all_positions.append(self._convert_scraped_position(sp, oi))

        # Get unique assets scanned
        assets_found = {p.asset for p in all_positions}

        # Filter positions using threshold criteria
        flagged = self.filter.filter_positions(all_positions)

        # Ensure coverage: at least 1 long + 1 short per asset
        # Uses liquidation proximity filter to fill gaps
        flagged = ensure_asset_coverage(
            all_positions=all_positions,
            flagged_positions=flagged,
            min_notional=self.min_liq_notional,
            max_proximity=self.max_liq_proximity,
        )

        # Calculate liquidation proximity summary
        liq_summary = self._calculate_liquidation_summary(all_positions)

        return ScanResult(
            timestamp=start_time,
            assets_scanned=len(assets_found),
            total_positions_checked=len(all_positions),
            flagged_positions=flagged,
            errors=self.errors,
            liquidation_summary=liq_summary
        )


async def run_dry_run() -> ScanResult:
    """Run a test scan with mock data (no actual scraping)"""
    from models import AlertReason

    logger.info("Running in DRY RUN mode with mock data...")

    # Create mock flagged positions
    mock_positions = [
        FlaggedPosition(
            position=Position(
                trader_address="0x1234567890abcdef1234567890abcdef12345678",
                asset="BTC",
                side=PositionSide.LONG,
                notional_usd=15_000_000,
                size=150.5,
                asset_open_interest=500_000_000
            ),
            alert_reasons=[AlertReason.MAJOR_ASSET_THRESHOLD]
        ),
        FlaggedPosition(
            position=Position(
                trader_address="0xabcdef1234567890abcdef1234567890abcdef12",
                asset="DOGE",
                side=PositionSide.SHORT,
                notional_usd=3_000_000,
                size=50_000_000,
                asset_open_interest=50_000_000
            ),
            alert_reasons=[
                AlertReason.NOTIONAL_THRESHOLD,
                AlertReason.OI_PERCENTAGE_THRESHOLD
            ]
        ),
    ]

    return ScanResult(
        timestamp=datetime.now(timezone.utc),
        assets_scanned=5,
        total_positions_checked=50,
        flagged_positions=mock_positions,
        errors=[]
    )


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Scan Hyperdash for large trader positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--major-threshold",
        type=float,
        default=MAJOR_ASSET_THRESHOLD,
        help=f"n_1: Threshold for BTC/ETH/SOL in USD (default: ${MAJOR_ASSET_THRESHOLD:,.0f})"
    )

    parser.add_argument(
        "--default-threshold",
        type=float,
        default=DEFAULT_NOTIONAL_THRESHOLD,
        help=f"n_2: Threshold for other assets in USD (default: ${DEFAULT_NOTIONAL_THRESHOLD:,.0f})"
    )

    parser.add_argument(
        "--oi-percentage",
        type=float,
        default=OI_PERCENTAGE_THRESHOLD,
        help=f"x: OI percentage threshold as decimal (default: {OI_PERCENTAGE_THRESHOLD})"
    )

    parser.add_argument(
        "--assets",
        type=str,
        default=None,
        help="Comma-separated list of assets to scan (default: top by OI)"
    )

    parser.add_argument(
        "--max-tickers",
        type=int,
        default=MAX_ASSETS_TO_SCAN,
        help="Maximum number of tickers to scan (default: all)"
    )

    parser.add_argument(
        "--skip-top-mc",
        action="store_true",
        default=True,
        help="Skip top coins by market cap (default: True) - they're too liquid for liquidation hunting"
    )

    parser.add_argument(
        "--no-skip-top-mc",
        action="store_true",
        help="Include top market cap coins in the scan"
    )

    parser.add_argument(
        "--liq-min-notional",
        type=float,
        default=500_000,
        help="Minimum notional for liquidation proximity filter (default: $500,000)"
    )

    parser.add_argument(
        "--liq-max-proximity",
        type=float,
        default=0.10,
        help="Maximum distance to liquidation for coverage fill (default: 0.10 = 10%%)"
    )

    parser.add_argument(
        "--prefilter-oi-pct",
        type=float,
        default=0.005,
        help="OI percentage for Hyperdash pre-filter (default: 0.005 = 0.5%%)"
    )

    parser.add_argument(
        "--prefilter-floor",
        type=float,
        default=500_000,
        help="Minimum notional floor for Hyperdash pre-filter (default: $500,000)"
    )

    parser.add_argument(
        "--output",
        type=str,
        default=OUTPUT_FORMAT,
        help="Output format(s): console,json,csv,telegram (comma-separated)"
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        default=HEADLESS_MODE,
        help="Run browser in headless mode"
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible mode (for debugging)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test mode - use mock data instead of scraping"
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default=LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )

    return parser.parse_args()


async def main():
    """Main entry point"""
    args = parse_args()

    # Set up logging
    setup_logging(args.log_level, LOG_FILE)

    # Determine headless mode
    headless = args.headless and not args.no_headless

    # Parse assets if provided
    assets = None
    if args.assets:
        assets = [a.strip().upper() for a in args.assets.split(",")]

    # Parse output formats
    output_formats = [f.strip().lower() for f in args.output.split(",")]

    # Create formatter
    formatter = create_formatters(
        console="console" in output_formats,
        json_output="json" in output_formats,
        csv_output="csv" in output_formats,
        telegram_token=TELEGRAM_BOT_TOKEN if "telegram" in output_formats else None,
        telegram_chat=TELEGRAM_CHAT_ID if "telegram" in output_formats else None,
        output_dir=OUTPUT_DIR,
        filename=OUTPUT_FILENAME
    )

    # Determine which coins to skip
    skip_coins = None
    if args.skip_top_mc and not args.no_skip_top_mc:
        skip_coins = SKIP_TOP_MC_COINS

    # Run scan
    if args.dry_run:
        result = await run_dry_run()
    else:
        scanner = HyperdashScanner(
            major_threshold=args.major_threshold,
            default_threshold=args.default_threshold,
            oi_percentage=args.oi_percentage,
            headless=headless,
            request_delay=REQUEST_DELAY,
            min_liq_notional=args.liq_min_notional,
            max_liq_proximity=args.liq_max_proximity,
            prefilter_oi_pct=args.prefilter_oi_pct,
            prefilter_floor=args.prefilter_floor,
        )
        result = await scanner.run(
            assets=assets,
            max_tickers=args.max_tickers,
            skip_coins=skip_coins
        )

    # Output results
    formatter.output(result)

    return 0 if not result.errors else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
