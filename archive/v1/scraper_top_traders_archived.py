"""
ARCHIVED: Top-Traders Scraping Approach
========================================

This file contains the archived top-traders scraping methods.
These were replaced by the terminal-based approach which provides
more comprehensive position data (ALL positions per ticker vs just top traders).

The terminal approach is now the primary method in scraper.py.
"""

import asyncio
import logging
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)


# These methods were removed from HyperdashScraper class:

async def set_min_notional_filter(self, min_notional: float) -> bool:
    """
    Set the Min Notional filter on the top traders page.

    This pre-filters traders to only show those with positions >= min_notional,
    reducing the amount of data we need to scrape.

    Args:
        min_notional: Minimum position notional in USD (e.g., 500000 for $500K)

    Returns:
        True if filter was set successfully, False otherwise
    """
    try:
        # Format the value for display (e.g., 500000 -> "500000")
        notional_str = str(int(min_notional))

        logger.info(f"Setting Min Notional filter to ${min_notional:,.0f}")

        # Look for filter button/panel - common patterns on Hyperdash
        # Try multiple selectors in case UI varies
        filter_selectors = [
            'button:has-text("Filter")',
            'button:has-text("Filters")',
            '[data-testid="filter-button"]',
            '.filter-button',
            'button[aria-label*="filter" i]',
        ]

        filter_opened = False
        for selector in filter_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.5)
                    filter_opened = True
                    logger.debug(f"Opened filter panel with selector: {selector}")
                    break
            except Exception:
                continue

        if not filter_opened:
            logger.warning("Could not find filter button - filter may already be visible or UI changed")

        # Look for Min Notional input field
        notional_input_selectors = [
            'input[placeholder*="Min Notional" i]',
            'input[placeholder*="Notional" i]',
            'input[name*="notional" i]',
            'input[name*="minNotional" i]',
            'label:has-text("Min Notional") + input',
            'label:has-text("Min Notional") ~ input',
            '[data-testid="min-notional-input"]',
        ]

        input_found = False
        for selector in notional_input_selectors:
            try:
                input_el = await self.page.query_selector(selector)
                if input_el:
                    # Clear existing value and set new one
                    await input_el.click()
                    await input_el.fill("")
                    await input_el.fill(notional_str)
                    input_found = True
                    logger.debug(f"Set min notional with selector: {selector}")
                    break
            except Exception:
                continue

        if not input_found:
            logger.warning("Could not find Min Notional input field - check Hyperdash UI for selector updates")
            return False

        # Apply the filter - look for apply/submit button
        apply_selectors = [
            'button:has-text("Apply")',
            'button:has-text("Submit")',
            'button:has-text("Search")',
            'button[type="submit"]',
            '.filter-apply',
        ]

        for selector in apply_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn:
                    await btn.click()
                    await asyncio.sleep(1)
                    logger.debug(f"Applied filter with selector: {selector}")
                    break
            except Exception:
                continue

        # Wait for page to update with filtered results
        await asyncio.sleep(2)
        logger.info(f"Min Notional filter set to ${min_notional:,.0f}")
        return True

    except Exception as e:
        logger.error(f"Error setting min notional filter: {e}")
        return False


async def get_top_traders_list(self, min_notional: Optional[float] = None) -> List[str]:
    """
    Scrape the top traders page to get list of trader addresses.

    Args:
        min_notional: Optional minimum notional to filter by before scraping

    Returns list of trader addresses (0x...).
    """
    url = f"{self.BASE_URL}/top-traders"
    logger.info(f"Fetching top traders from {url}")

    traders = []
    try:
        await self.page.goto(url, wait_until="networkidle")
        await asyncio.sleep(3)  # Let table populate

        # Apply min notional filter if specified
        if min_notional is not None:
            await self.set_min_notional_filter(min_notional)

        # Find all trader links
        links = await self.page.query_selector_all('a[href*="/trader/0x"]')
        for link in links:
            href = await link.get_attribute('href')
            if href and '/trader/0x' in href:
                # Extract address from /trader/0x...
                address = href.split('/trader/')[-1]
                if address.startswith('0x') and len(address) >= 42:
                    traders.append(address[:42])

        # Remove duplicates while preserving order
        seen = set()
        traders = [t for t in traders if not (t in seen or seen.add(t))]
        logger.info(f"Found {len(traders)} unique traders")

    except Exception as e:
        logger.error(f"Error fetching top traders: {e}")

    return traders


async def get_trader_positions(self, trader_address: str) -> List:
    """
    Scrape all positions for a specific trader.

    Returns list of positions for the trader.
    """
    url = f"{self.BASE_URL}/trader/{trader_address}"
    logger.debug(f"Fetching positions for {trader_address[:10]}...")

    positions = []
    try:
        await self.page.goto(url, wait_until="networkidle")
        await asyncio.sleep(2)

        # Find position rows in the table
        rows = await self.page.query_selector_all('tbody tr')

        for row in rows:
            try:
                cells = await row.query_selector_all('td')
                if len(cells) < 4:
                    continue

                # Cell 0: Asset with leverage (e.g., "ETH\n5x")
                asset_text = await cells[0].inner_text()
                asset = asset_text.split('\n')[0].strip()

                # Cell 1: Type (LONG/SHORT)
                side_text = await cells[1].inner_text()
                side = "long" if "LONG" in side_text.upper() else "short"

                # Cell 2: Position Value / Size (e.g., "$683,366,918.76\n203,...")
                value_text = await cells[2].inner_text()
                value_line = value_text.split('\n')[0]
                # notional = parse_notional(value_line)

                # Cell 3: Unrealized PnL
                pnl = None
                pnl_text = await cells[3].inner_text()
                pnl_line = pnl_text.split('\n')[0]

                # Cell 4: Entry Price
                entry_price = None
                if len(cells) > 4:
                    entry_text = await cells[4].inner_text()

                # Cell 5: Current Price
                current_price = None
                if len(cells) > 5:
                    current_text = await cells[5].inner_text()

                # Cell 6: Liquidation Price
                liquidation_price = None
                if len(cells) > 6:
                    liq_text = await cells[6].inner_text()

                # Position creation would go here...

            except Exception as e:
                logger.debug(f"Error parsing position row: {e}")
                continue

        logger.debug(f"Found {len(positions)} positions for {trader_address[:10]}...")

    except Exception as e:
        logger.error(f"Error fetching trader positions: {e}")

    return positions


async def get_all_positions(
    self,
    max_traders: int = 50,
    delay: float = 2.0,
    min_notional: Optional[float] = None
) -> List:
    """
    Get all positions from top traders.

    1. Fetch list of top traders (optionally filtered by min notional)
    2. Visit each trader's page
    3. Collect all their positions

    Args:
        max_traders: Maximum number of traders to scrape (for rate limiting)
        delay: Seconds to wait between trader page loads
        min_notional: Optional minimum notional to pre-filter traders

    Returns list of all positions from all traders.
    """
    all_positions = []

    # Get list of top traders (with optional min notional filter)
    traders = await self.get_top_traders_list(min_notional=min_notional)
    traders = traders[:max_traders]  # Limit for rate limiting

    logger.info(f"Scraping positions from {len(traders)} traders...")

    for i, trader in enumerate(traders):
        logger.info(f"[{i+1}/{len(traders)}] Scraping {trader[:10]}...")
        positions = await self.get_trader_positions(trader)
        all_positions.extend(positions)

        # Rate limiting delay
        if i < len(traders) - 1:
            await asyncio.sleep(delay)

    logger.info(f"Total positions collected: {len(all_positions)}")
    return all_positions


async def get_asset_positions(self, asset: str) -> List:
    """
    Get all positions for a specific asset from top traders.

    This is a filtered view - scrapes all positions then filters by asset.
    For efficiency, use get_all_positions() and filter once.
    """
    all_positions = await self.get_all_positions(max_traders=30)
    return [p for p in all_positions if p.asset.upper() == asset.upper()]


async def scrape_analytics_page(self) -> Dict[str, Dict[str, Any]]:
    """
    Scrape the analytics page which shows aggregate positioning per asset.

    Returns dict: asset -> {total_notional, majority_side, oi_coverage, open_interest, etc.}
    """
    logger.info(f"Scraping analytics page: {self.ANALYTICS_URL}")

    data = {}

    try:
        await self.page.goto(self.ANALYTICS_URL, wait_until="networkidle")
        await asyncio.sleep(3)  # Let table populate

        # Find all data rows
        rows = await self.page.query_selector_all('tbody tr')

        for row in rows:
            try:
                cells = await row.query_selector_all('td')
                if len(cells) < 10:
                    continue

                # Columns: ASSET, 24H VOL, OI COVERAGE, TOTAL NOTIONAL, MAJORITY SIDE,
                #          L/S RATIO, MAJ SIDE NOTIONAL, MAJ SIDE P/L, TRADERS, OI
                asset = (await cells[0].inner_text()).strip()

                # data[asset] = {...}

            except Exception as e:
                logger.debug(f"Error parsing analytics row: {e}")
                continue

        logger.info(f"Scraped analytics for {len(data)} assets")

    except Exception as e:
        logger.error(f"Error scraping analytics page: {e}")

    return data
