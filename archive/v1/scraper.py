"""
Hyperdash Scraper
==================

Browser-based scraper for Hyperdash terminal pages.
Uses Playwright for reliable browser automation.

Scrapes position data from the terminal page per ticker:
    https://legacy.hyperdash.com/terminal?ticker={TICKER}

This approach shows ALL positions for a ticker (not just top traders).
"""

import asyncio
import logging
from typing import List, Optional
from dataclasses import dataclass

try:
    from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Playwright not installed. Install with: pip install playwright && playwright install chromium")
    raise

from models import Position, PositionSide

logger = logging.getLogger(__name__)


@dataclass
class TerminalPosition:
    """Position data scraped from Hyperdash terminal page"""
    trader_address: str
    asset: str
    side: str  # "long", "short", or "unknown"
    notional_usd: float
    size: float
    entry_price: Optional[float] = None
    current_price: Optional[float] = None
    liquidation_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None


def parse_notional(text: str) -> float:
    """
    Parse notional value from display text.

    Examples:
        "$1.5M" -> 1500000
        "$500K" -> 500000
        "$12,345" -> 12345
        "1.2M" -> 1200000
    """
    text = text.strip().replace("$", "").replace(",", "")

    multipliers = {
        "B": 1_000_000_000,
        "M": 1_000_000,
        "K": 1_000,
    }

    for suffix, mult in multipliers.items():
        if suffix in text.upper():
            num = float(text.upper().replace(suffix, ""))
            return num * mult

    try:
        return float(text)
    except ValueError:
        logger.warning(f"Could not parse notional: {text}")
        return 0.0


class HyperdashScraper:
    """
    Scrapes position data from Hyperdash terminal pages.

    Strategy:
    1. Get list of top tickers by OI from Hyperliquid API
    2. For each ticker, visit terminal page at /terminal?ticker={TICKER}
    3. Parse the "All Positions" table
    4. Apply Min Notional filter to reduce noise
    """

    BASE_URL = "https://legacy.hyperdash.com"

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 30000,  # ms
        slow_mo: int = 100  # ms between actions
    ):
        self.headless = headless
        self.timeout = timeout
        self.slow_mo = slow_mo
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self):
        """Initialize browser"""
        logger.info("Starting browser...")
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo
        )
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(self.timeout)
        logger.info("Browser started")

    async def close(self):
        """Clean up browser"""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")

    async def get_terminal_positions(
        self,
        ticker: str,
        min_notional: float = 0
    ) -> List[TerminalPosition]:
        """
        Scrape positions for a specific ticker from the terminal page.

        This method provides comprehensive position data - ALL positions
        for a ticker, not just those from top traders.

        Args:
            ticker: Asset symbol (e.g., "BTC", "ETH", "FARTCOIN")
            min_notional: Minimum notional filter in USD (e.g., 1000000 for $1M)

        Returns:
            List of positions for this ticker
        """
        url = f"{self.BASE_URL}/terminal?ticker={ticker}"
        logger.info(f"Fetching positions for {ticker} from {url}")

        positions = []
        try:
            await self.page.goto(url, wait_until="networkidle")
            await asyncio.sleep(5)  # Let page fully load

            # Wait for table to appear
            try:
                await self.page.wait_for_selector('table', timeout=10000)
            except:
                logger.warning(f"Table not found after waiting for {ticker}")

            # Apply min notional filter if specified
            if min_notional > 0:
                logger.debug(f"Setting Min Notional filter to ${min_notional:,.0f}")
                await self.page.evaluate(f'''() => {{
                    const inputs = document.querySelectorAll('input[placeholder="Min"][type="number"]');
                    for (const inp of inputs) {{
                        const parentText = inp.parentElement?.innerText || '';
                        if (parentText.includes('Notional')) {{
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            nativeInputValueSetter.call(inp, '{int(min_notional)}');
                            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                    return false;
                }}''')
                await asyncio.sleep(2)  # Wait for filter to apply

            # Get current price for this ticker (for reference)
            current_price = None
            try:
                price_data = await self.page.evaluate('''() => {
                    const el = document.body.innerText.match(/\\$([\\d,]+\\.?\\d*)\\s/);
                    return el ? el[1] : null;
                }''')
                if price_data:
                    current_price = float(price_data.replace(',', ''))
            except:
                pass

            # Parse table rows
            all_tables = await self.page.query_selector_all('table')
            logger.debug(f"Found {len(all_tables)} tables on page for {ticker}")

            table = await self.page.query_selector('table')
            if not table:
                logger.warning(f"No table found for {ticker}")
                return positions

            rows = await table.query_selector_all('tbody tr')
            logger.debug(f"Found {len(rows)} rows for {ticker}")

            for row in rows:
                try:
                    cells = await row.query_selector_all('td')
                    if len(cells) < 4:
                        continue

                    # Parse cell values
                    # Headers: ADDRESS, NOTIONAL, ENTRY, LIQ. PRICE, UNREALIZED PNL, ...
                    addr_text = await cells[0].inner_text()
                    notional_text = await cells[1].inner_text()
                    entry_text = await cells[2].inner_text()
                    liq_text = await cells[3].inner_text()

                    # Extract address (format: 0x59bb...5f56)
                    trader_address = addr_text.strip()

                    # Parse notional
                    notional = parse_notional(notional_text)

                    # Parse entry price
                    entry_price = None
                    try:
                        entry_clean = entry_text.replace('$', '').replace(',', '')
                        entry_price = float(entry_clean)
                    except:
                        pass

                    # Parse liquidation price and determine side
                    liquidation_price = None
                    side = "unknown"

                    if liq_text.strip() != '-' and liq_text.strip():
                        try:
                            liq_clean = liq_text.replace('$', '').replace(',', '')
                            liquidation_price = float(liq_clean)

                            # Infer side from liq vs entry price
                            if entry_price and liquidation_price:
                                if liquidation_price < entry_price:
                                    side = "long"
                                else:
                                    side = "short"
                        except:
                            pass

                    # Parse PnL if available
                    pnl = None
                    if len(cells) > 4:
                        try:
                            pnl_text = await cells[4].inner_text()
                            pnl = parse_notional(pnl_text.replace('+', ''))
                            if '-' in pnl_text:
                                pnl = -abs(pnl)
                        except:
                            pass

                    positions.append(TerminalPosition(
                        trader_address=trader_address,
                        asset=ticker,
                        side=side,
                        notional_usd=notional,
                        size=0,  # Not provided in this view
                        entry_price=entry_price,
                        current_price=current_price,
                        liquidation_price=liquidation_price,
                        pnl=pnl
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing row for {ticker}: {e}")
                    continue

            logger.info(f"Found {len(positions)} positions for {ticker}")

        except PlaywrightTimeout:
            logger.warning(f"Timeout loading terminal page for {ticker}")
        except Exception as e:
            logger.error(f"Error fetching terminal positions for {ticker}: {e}")

        return positions

    async def get_positions_for_tickers(
        self,
        tickers: List[str],
        min_notional: float = 0,
        delay: float = 2.0
    ) -> List[TerminalPosition]:
        """
        Scrape positions for multiple tickers from terminal pages.

        Args:
            tickers: List of asset symbols to scan
            min_notional: Minimum notional filter in USD
            delay: Delay between requests (rate limiting)

        Returns:
            Combined list of all positions across all tickers
        """
        all_positions = []

        for i, ticker in enumerate(tickers):
            logger.info(f"[{i+1}/{len(tickers)}] Scanning {ticker}...")
            positions = await self.get_terminal_positions(ticker, min_notional)
            all_positions.extend(positions)

            if i < len(tickers) - 1:
                await asyncio.sleep(delay)

        logger.info(f"Total positions across {len(tickers)} tickers: {len(all_positions)}")
        return all_positions


async def test_scraper():
    """Test the terminal scraper"""
    logging.basicConfig(level=logging.INFO)

    async with HyperdashScraper(headless=True) as scraper:
        # Test getting positions for a few tickers
        print("=== Testing Terminal Scraper ===")
        tickers = ["BTC", "ETH", "SOL"]
        positions = await scraper.get_positions_for_tickers(
            tickers,
            min_notional=500_000,
            delay=3.0
        )

        print(f"\n=== All Positions ({len(positions)}) ===")
        for pos in positions[:20]:
            side_str = pos.side.upper() if pos.side != "unknown" else "???"
            liq_str = f"${pos.liquidation_price:,.0f}" if pos.liquidation_price else "N/A"
            print(f"{pos.asset:8} | {side_str:5} | ${pos.notional_usd:>12,.0f} | Liq: {liq_str}")


if __name__ == "__main__":
    asyncio.run(test_scraper())
