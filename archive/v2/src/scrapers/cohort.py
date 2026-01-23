"""
Hyperdash Cohort Scraper
========================

Fetches trader data from Hyperdash cohort API:
    https://api.hyperdash.com/graphql

Cohorts: kraken, large_whale, whale, shark

Outputs a CSV with columns:
    Address, Perp Equity, Perp Bias, Position Value, Leverage, Sum UPNL, PNL Cohort, Cohort
"""

import csv
import logging
import requests
import time
from typing import List, Dict, Any
from dataclasses import dataclass
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.hyperdash.com/graphql"

# Cohort groupings
PRIORITY_COHORTS = ["kraken", "large_whale", "whale"]
SECONDARY_COHORTS = ["shark"]
ALL_COHORTS = PRIORITY_COHORTS + SECONDARY_COHORTS

COHORT_QUERY = """
query GetSizeCohort($id: String!, $limit: Int!, $offset: Int!, $sortBy: CohortTraderSortInput) {
  analytics {
    sizeCohort(id: $id) {
      cohortInfo {
        id
        label
        range
        emoji
      }
      totalTraders
      totalAccountValue
      topTraders(limit: $limit, offset: $offset, sortBy: $sortBy) {
        totalCount
        hasMore
        traders {
          address
          accountValue
          perpPnl
          totalNotional
          longNotional
          shortNotional
          positions {
            coin
            size
            notionalSize
            unrealizedPnl
            entryPrice
          }
        }
      }
    }
  }
}
"""


@dataclass
class CohortTrader:
    """Trader data from Hyperdash cohort API"""
    address: str
    perp_equity: float
    perp_bias: str
    position_value: float
    leverage: float
    sum_upnl: float
    pnl_cohort: str
    cohort: str


def fetch_cohort_data(cohort_id: str, page_size: int = 500) -> List[Dict[str, Any]]:
    """
    Fetch all trader data for a specific cohort from the GraphQL API.
    Automatically paginates to fetch all traders.

    Args:
        cohort_id: Cohort identifier (kraken, large_whale, whale, shark)
        page_size: Number of traders to fetch per request

    Returns:
        List of trader dictionaries from API response
    """
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://hyperdash.com",
        "Referer": "https://hyperdash.com/"
    }

    all_traders = []
    offset = 0
    total_count = None

    while True:
        variables = {
            "id": cohort_id,
            "limit": page_size,
            "offset": offset,
            "sortBy": {
                "field": "accountValue",
                "order": "desc"
            }
        }

        try:
            response = requests.post(
                GRAPHQL_URL,
                json={
                    "query": COHORT_QUERY,
                    "variables": variables,
                    "operationName": "GetSizeCohort"
                },
                headers=headers,
                timeout=30
            )
            response.raise_for_status()

            data = response.json()

            if "errors" in data:
                logger.error(f"GraphQL errors for {cohort_id}: {data['errors']}")
                break

            cohort_data = data.get("data", {}).get("analytics", {}).get("sizeCohort", {})
            top_traders = cohort_data.get("topTraders", {})
            traders = top_traders.get("traders", [])
            has_more = top_traders.get("hasMore", False)

            if total_count is None:
                total_count = top_traders.get("totalCount", 0)
                logger.info(f"Cohort {cohort_id} has {total_count} total traders")

            all_traders.extend(traders)
            logger.info(f"Fetched {len(traders)} traders for {cohort_id} (offset={offset}, total so far: {len(all_traders)})")

            if not has_more or not traders:
                break

            offset += len(traders)
            time.sleep(0.5)  # Small delay between pagination requests

        except requests.RequestException as e:
            logger.error(f"Request failed for {cohort_id} at offset {offset}: {e}")
            break

    logger.info(f"Completed fetching {len(all_traders)} traders for cohort: {cohort_id}")
    return all_traders


def calculate_perp_bias(long_notional: float, short_notional: float) -> str:
    """
    Calculate perp bias based on long/short notional values.

    Returns:
        String indicating bias direction and percentage
    """
    total = long_notional + short_notional
    if total == 0:
        return "Neutral (0%)"

    net = long_notional - short_notional
    pct = abs(net / total) * 100

    if net > 0:
        return f"Long ({pct:.1f}%)"
    elif net < 0:
        return f"Short ({pct:.1f}%)"
    else:
        return "Neutral (0%)"


def calculate_sum_upnl(positions: List[Dict]) -> float:
    """Sum unrealized PnL from all positions"""
    return sum(pos.get("unrealizedPnl", 0) or 0 for pos in positions)


def determine_pnl_cohort(perp_pnl: float) -> str:
    """
    Determine PNL cohort based on realized PnL value.

    Rough categorization:
    - Profit: > $100K
    - Slight Profit: $0 - $100K
    - Slight Loss: -$100K - $0
    - Loss: < -$100K
    """
    if perp_pnl >= 1_000_000:
        return "Big Winner"
    elif perp_pnl >= 100_000:
        return "Winner"
    elif perp_pnl >= 0:
        return "Slight Profit"
    elif perp_pnl >= -100_000:
        return "Slight Loss"
    elif perp_pnl >= -1_000_000:
        return "Loser"
    else:
        return "Big Loser"


def process_trader(trader: Dict[str, Any], cohort_id: str) -> CohortTrader:
    """
    Process raw trader data into CohortTrader object.

    Args:
        trader: Raw trader dict from API
        cohort_id: Cohort identifier

    Returns:
        CohortTrader object
    """
    account_value = trader.get("accountValue", 0) or 0
    total_notional = trader.get("totalNotional", 0) or 0
    long_notional = trader.get("longNotional", 0) or 0
    short_notional = trader.get("shortNotional", 0) or 0
    perp_pnl = trader.get("perpPnl", 0) or 0
    positions = trader.get("positions", []) or []

    # Calculate leverage (avoid division by zero)
    leverage = total_notional / account_value if account_value > 0 else 0

    return CohortTrader(
        address=trader.get("address", ""),
        perp_equity=account_value,
        perp_bias=calculate_perp_bias(long_notional, short_notional),
        position_value=total_notional,
        leverage=leverage,
        sum_upnl=calculate_sum_upnl(positions),
        pnl_cohort=determine_pnl_cohort(perp_pnl),
        cohort=cohort_id
    )


def fetch_cohorts(cohorts: List[str], delay: float = 1.0) -> List[CohortTrader]:
    """
    Fetch traders from specified cohorts.

    Args:
        cohorts: List of cohort IDs to fetch
        delay: Delay between API requests (rate limiting)

    Returns:
        Combined list of all traders from specified cohorts
    """
    all_traders = []

    for i, cohort_id in enumerate(cohorts):
        logger.info(f"[{i+1}/{len(cohorts)}] Fetching cohort: {cohort_id}")

        raw_traders = fetch_cohort_data(cohort_id)

        for trader in raw_traders:
            processed = process_trader(trader, cohort_id)
            all_traders.append(processed)

        if i < len(cohorts) - 1:
            time.sleep(delay)

    logger.info(f"Total traders fetched: {len(all_traders)}")
    return all_traders


def format_currency(value: float) -> str:
    """Format large numbers with K/M/B suffixes"""
    if abs(value) >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    elif abs(value) >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif abs(value) >= 1_000:
        return f"${value/1_000:.2f}K"
    else:
        return f"${value:.2f}"


def save_to_csv(traders: List[CohortTrader], filename: str):
    """
    Save trader data to CSV file.

    Args:
        traders: List of CohortTrader objects
        filename: Output filename
    """
    if not traders:
        logger.warning("No traders to save")
        return

    fieldnames = [
        "Address",
        "Perp Equity",
        "Perp Bias",
        "Position Value",
        "Leverage",
        "Sum UPNL",
        "PNL Cohort",
        "Cohort"
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)

        for trader in traders:
            writer.writerow([
                trader.address,
                format_currency(trader.perp_equity),
                trader.perp_bias,
                format_currency(trader.position_value),
                f"{trader.leverage:.2f}x",
                format_currency(trader.sum_upnl),
                trader.pnl_cohort,
                trader.cohort
            ])

    logger.info(f"Saved {len(traders)} traders to {filename}")


def print_summary(traders: List[CohortTrader], cohorts: List[str], label: str):
    """Print summary for a cohort scan"""
    print(f"\n{'='*60}")
    print(f"Scan Complete: {label}")
    print(f"{'='*60}")
    print(f"Total traders: {len(traders)}")

    cohort_counts = {}
    for trader in traders:
        cohort_counts[trader.cohort] = cohort_counts.get(trader.cohort, 0) + 1

    print("\nBreakdown by cohort:")
    for cohort in cohorts:
        count = cohort_counts.get(cohort, 0)
        print(f"  {cohort}: {count}")


def main():
    """Main entry point - fetches priority cohorts first, then sharks separately"""

    # Phase 1: Priority cohorts (kraken, large_whale, whale)
    print("\n" + "="*60)
    print("PHASE 1: Priority Cohorts (Kraken, Large Whale, Whale)")
    print("="*60)

    priority_traders = fetch_cohorts(PRIORITY_COHORTS, delay=1.0)
    save_to_csv(priority_traders, "data/raw/cohort_data_priority.csv")
    print_summary(priority_traders, PRIORITY_COHORTS, "Priority Cohorts")
    print(f"Output saved to: data/raw/cohort_data_priority.csv")

    # Phase 2: Secondary cohorts (shark)
    print("\n" + "="*60)
    print("PHASE 2: Secondary Cohorts (Shark)")
    print("="*60)

    shark_traders = fetch_cohorts(SECONDARY_COHORTS, delay=1.0)
    save_to_csv(shark_traders, "data/raw/cohort_data_shark.csv")
    print_summary(shark_traders, SECONDARY_COHORTS, "Shark Cohort")
    print(f"Output saved to: data/raw/cohort_data_shark.csv")

    # Combined file
    all_traders = priority_traders + shark_traders
    save_to_csv(all_traders, "data/raw/cohort_data.csv")

    # Final summary
    print("\n" + "="*60)
    print("ALL SCANS COMPLETE")
    print("="*60)
    print(f"Total traders: {len(all_traders)}")
    print("\nOutput files:")
    print("  data/raw/cohort_data_priority.csv  - Kraken, Large Whale, Whale")
    print("  data/raw/cohort_data_shark.csv     - Shark only")
    print("  data/raw/cohort_data.csv           - All cohorts combined")


if __name__ == "__main__":
    main()
