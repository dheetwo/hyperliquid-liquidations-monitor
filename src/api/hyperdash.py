"""
Hyperdash GraphQL API Client

Single responsibility: fetch cohort data from Hyperdash.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import aiohttp

from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class WalletInfo:
    """Wallet information from Hyperdash cohort API."""
    address: str
    account_value: float
    perp_pnl: float
    total_notional: float
    long_notional: float
    short_notional: float
    cohort: str

    @property
    def leverage(self) -> float:
        """Calculate effective leverage."""
        if self.account_value <= 0:
            return 0.0
        return self.total_notional / self.account_value

    @property
    def bias(self) -> str:
        """Get position bias (long/short/neutral)."""
        if self.total_notional <= 0:
            return "Neutral"
        long_pct = self.long_notional / self.total_notional * 100
        short_pct = self.short_notional / self.total_notional * 100
        if long_pct > 60:
            return f"Long ({long_pct:.0f}%)"
        elif short_pct > 60:
            return f"Short ({short_pct:.0f}%)"
        return "Neutral"


# GraphQL Queries
SIZE_COHORT_QUERY = """
query GetSizeCohort($id: String!, $limit: Int!, $offset: Int!) {
  analytics {
    sizeCohort(id: $id) {
      totalTraders
      topTraders(limit: $limit, offset: $offset) {
        totalCount
        hasMore
        traders {
          address
          accountValue
          perpPnl
          totalNotional
          longNotional
          shortNotional
        }
      }
    }
  }
}
"""

PNL_COHORT_QUERY = """
query GetPnlCohort($id: String!, $limit: Int!, $offset: Int!) {
  analytics {
    pnlCohort(id: $id) {
      totalTraders
      topTraders(limit: $limit, offset: $offset) {
        totalCount
        hasMore
        traders {
          address
          accountValue
          perpPnl
          totalNotional
          longNotional
          shortNotional
        }
      }
    }
  }
}
"""

# Size cohorts use sizeCohort query, PnL cohorts use pnlCohort query
SIZE_COHORTS = {"kraken", "large_whale", "whale", "shark"}
PNL_COHORTS = {"extremely_profitable", "very_profitable", "profitable",
               "unprofitable", "very_unprofitable", "rekt"}


class HyperdashClient:
    """
    Async client for Hyperdash GraphQL API.

    Handles:
    - Fetching wallet addresses by cohort
    - Pagination for large cohorts
    """

    def __init__(
        self,
        url: str = None,
        page_size: int = 500,
        page_delay: float = 0.5,
    ):
        self.url = url or config.hyperdash_url
        self.page_size = page_size
        self.page_delay = page_delay
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_session(self):
        """Create session if needed."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self):
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def _graphql_request(
        self,
        query: str,
        variables: dict,
        operation_name: str,
    ) -> Optional[dict]:
        """Make a GraphQL request."""
        await self._ensure_session()

        payload = {
            "query": query,
            "variables": variables,
            "operationName": operation_name,
        }

        headers = {
            "Content-Type": "application/json",
            "Origin": "https://hyperdash.com",
            "Referer": "https://hyperdash.com/",
        }

        try:
            async with self._session.post(
                self.url,
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    logger.error(f"Hyperdash API error {response.status}: {await response.text()}")
                    return None

                data = await response.json()

                # Check for GraphQL errors
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    return None

                return data.get("data")

        except aiohttp.ClientError as e:
            logger.error(f"Hyperdash request error: {e}")
            return None

    # -------------------------------------------------------------------------
    # Public Methods
    # -------------------------------------------------------------------------

    async def get_cohort_addresses(
        self,
        cohort: str,
        progress_callback: callable = None,
    ) -> List[WalletInfo]:
        """
        Fetch all wallet addresses for a cohort.

        Args:
            cohort: Cohort identifier (e.g., "kraken", "rekt")
            progress_callback: Optional callback(fetched, total) for progress

        Returns:
            List of WalletInfo objects
        """
        # Determine query type
        if cohort in SIZE_COHORTS:
            query = SIZE_COHORT_QUERY
            operation_name = "GetSizeCohort"
            result_path = "sizeCohort"
        elif cohort in PNL_COHORTS:
            query = PNL_COHORT_QUERY
            operation_name = "GetPnlCohort"
            result_path = "pnlCohort"
        else:
            logger.warning(f"Unknown cohort type: {cohort}, trying size cohort")
            query = SIZE_COHORT_QUERY
            operation_name = "GetSizeCohort"
            result_path = "sizeCohort"

        wallets = []
        offset = 0
        total = None

        while True:
            variables = {
                "id": cohort,
                "limit": self.page_size,
                "offset": offset,
            }

            data = await self._graphql_request(query, variables, operation_name)
            if not data:
                break

            cohort_data = data.get("analytics", {}).get(result_path)
            if not cohort_data:
                break

            traders_data = cohort_data.get("topTraders", {})
            traders = traders_data.get("traders", [])
            has_more = traders_data.get("hasMore", False)

            if total is None:
                total = traders_data.get("totalCount", 0)

            # Parse traders
            for trader in traders:
                try:
                    wallet = WalletInfo(
                        address=trader["address"].lower(),
                        account_value=float(trader.get("accountValue", 0)),
                        perp_pnl=float(trader.get("perpPnl", 0)),
                        total_notional=float(trader.get("totalNotional", 0)),
                        long_notional=float(trader.get("longNotional", 0)),
                        short_notional=float(trader.get("shortNotional", 0)),
                        cohort=cohort,
                    )
                    wallets.append(wallet)
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug(f"Error parsing trader: {e}")
                    continue

            if progress_callback and total:
                progress_callback(len(wallets), total)

            if not has_more:
                break

            offset += len(traders)
            await asyncio.sleep(self.page_delay)

        logger.info(f"Fetched {len(wallets)} wallets from {cohort} cohort")
        return wallets

    async def get_all_cohorts(
        self,
        cohorts: List[str] = None,
        progress_callback: callable = None,
    ) -> Dict[str, List[WalletInfo]]:
        """
        Fetch addresses from multiple cohorts.

        Args:
            cohorts: List of cohort identifiers (default from config)
            progress_callback: Optional callback(cohort, wallets) per cohort

        Returns:
            Dict mapping cohort name to list of WalletInfo
        """
        cohorts = cohorts or config.cohorts
        results: Dict[str, List[WalletInfo]] = {}

        for cohort in cohorts:
            logger.info(f"Fetching {cohort} cohort...")
            wallets = await self.get_cohort_addresses(cohort)
            results[cohort] = wallets

            if progress_callback:
                progress_callback(cohort, wallets)

            # Delay between cohorts to be nice to the API
            await asyncio.sleep(1.0)

        return results

    async def get_unique_addresses(
        self,
        cohorts: List[str] = None,
    ) -> Dict[str, WalletInfo]:
        """
        Get unique addresses across all cohorts.

        If an address appears in multiple cohorts, keeps the one
        with the higher account value.

        Args:
            cohorts: List of cohort identifiers

        Returns:
            Dict mapping address to WalletInfo
        """
        all_cohorts = await self.get_all_cohorts(cohorts)

        unique: Dict[str, WalletInfo] = {}
        for cohort, wallets in all_cohorts.items():
            for wallet in wallets:
                addr = wallet.address
                if addr not in unique or wallet.account_value > unique[addr].account_value:
                    unique[addr] = wallet

        logger.info(f"Got {len(unique)} unique addresses across all cohorts")
        return unique


# =============================================================================
# Convenience function for quick testing
# =============================================================================

async def test_client():
    """Quick test of the Hyperdash client."""
    async with HyperdashClient() as client:
        # Test fetching a single cohort
        print("Fetching kraken cohort...")
        wallets = await client.get_cohort_addresses("kraken")
        print(f"Got {len(wallets)} kraken wallets")

        if wallets:
            print("\nTop 5 by account value:")
            for w in sorted(wallets, key=lambda x: x.account_value, reverse=True)[:5]:
                print(f"  {w.address[:10]}... ${w.account_value:,.0f} "
                      f"leverage={w.leverage:.1f}x {w.bias}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_client())
