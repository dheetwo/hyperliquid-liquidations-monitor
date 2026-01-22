#!/usr/bin/env python3
"""
Analyze Order Book Liquidity
============================

Fetches L2 order book data for all tokens on main and xyz exchanges,
calculates liquidity depth within ±1% of mid price, and outputs
a sorted mapping to inform notional threshold decisions.
"""

import json
import os
import requests
import time
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

API_URL = "https://api.hyperliquid.xyz/info"
REQUEST_DELAY = 0.15  # seconds between requests


def post_request(payload: dict) -> dict:
    """Make a POST request to Hyperliquid API."""
    response = requests.post(API_URL, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_tokens(dex: str = "") -> List[str]:
    """Get all token symbols for an exchange."""
    payload = {"type": "meta"}
    if dex:
        payload["dex"] = dex

    data = post_request(payload)
    universe = data.get("universe", [])
    return [asset["name"] for asset in universe]


def get_l2_book(coin: str, dex: str = "") -> Optional[dict]:
    """Fetch L2 order book for a token."""
    payload = {"type": "l2Book", "coin": coin}
    if dex:
        payload["dex"] = dex

    try:
        data = post_request(payload)
        return data
    except Exception as e:
        print(f"  Error fetching {coin}: {e}", file=sys.stderr)
        return None


def calculate_liquidity_depth(book: dict, depth_pct: float = 1.0) -> Tuple[float, float, float]:
    """
    Calculate total USD liquidity within ±depth_pct% of mid price.

    Returns: (bid_liquidity, ask_liquidity, total_liquidity) in USD
    """
    levels = book.get("levels", [[], []])
    bids = levels[0]  # [[px, sz, n], ...]
    asks = levels[1]

    if not bids or not asks:
        return (0.0, 0.0, 0.0)

    # Get mid price
    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    mid_price = (best_bid + best_ask) / 2

    # Calculate price bounds
    lower_bound = mid_price * (1 - depth_pct / 100)
    upper_bound = mid_price * (1 + depth_pct / 100)

    # Sum bid liquidity within range
    bid_liquidity = 0.0
    for level in bids:
        px = float(level["px"])
        sz = float(level["sz"])
        if px >= lower_bound:
            bid_liquidity += px * sz
        else:
            break  # bids are sorted high to low

    # Sum ask liquidity within range
    ask_liquidity = 0.0
    for level in asks:
        px = float(level["px"])
        sz = float(level["sz"])
        if px <= upper_bound:
            ask_liquidity += px * sz
        else:
            break  # asks are sorted low to high

    return (bid_liquidity, ask_liquidity, bid_liquidity + ask_liquidity)


def analyze_exchange(dex: str = "", depth_pct: float = 1.0) -> Dict[str, dict]:
    """Analyze liquidity for all tokens on an exchange."""
    exchange_name = dex if dex else "main"
    print(f"\n{'='*60}")
    print(f"Analyzing {exchange_name.upper()} exchange (±{depth_pct}% depth)")
    print(f"{'='*60}")

    tokens = get_all_tokens(dex)
    print(f"Found {len(tokens)} tokens")

    results = {}
    for i, token in enumerate(tokens):
        time.sleep(REQUEST_DELAY)
        book = get_l2_book(token, dex)

        if book:
            bid_liq, ask_liq, total_liq = calculate_liquidity_depth(book, depth_pct)
            results[token] = {
                "bid_liquidity": bid_liq,
                "ask_liquidity": ask_liq,
                "total_liquidity": total_liq,
                "exchange": exchange_name,
            }

            # Progress indicator
            if (i + 1) % 20 == 0:
                print(f"  Processed {i + 1}/{len(tokens)} tokens...")

    return results


def format_usd(value: float) -> str:
    """Format USD value with appropriate suffix."""
    if value >= 1_000_000_000:
        return f"${value/1e9:.2f}B"
    elif value >= 1_000_000:
        return f"${value/1e6:.2f}M"
    elif value >= 1_000:
        return f"${value/1e3:.0f}K"
    else:
        return f"${value:.0f}"


def suggest_threshold(liquidity: float) -> float:
    """
    Suggest a notional threshold based on liquidity.

    Rule of thumb: threshold = ~5-10% of 1% depth liquidity
    This ensures a liquidation at threshold won't cause >1% slippage.
    """
    # Use 7.5% of total 1% depth as threshold
    return liquidity * 0.075


def main():
    depth_pct = 1.0  # ±1% from mid price

    # Analyze both exchanges
    all_results = {}

    # Main exchange
    main_results = analyze_exchange("", depth_pct)
    all_results.update(main_results)

    time.sleep(1)  # Brief pause between exchanges

    # XYZ exchange
    xyz_results = analyze_exchange("xyz", depth_pct)
    all_results.update({f"{k}@xyz": v for k, v in xyz_results.items()})

    # Sort by total liquidity (descending)
    sorted_results = sorted(
        all_results.items(),
        key=lambda x: x[1]["total_liquidity"],
        reverse=True
    )

    # Print results
    print(f"\n{'='*80}")
    print(f"LIQUIDITY ANALYSIS RESULTS (±{depth_pct}% depth from mid price)")
    print(f"{'='*80}")

    print(f"\n{'Token':<15} {'Exchange':<8} {'Bid Depth':<12} {'Ask Depth':<12} {'Total':<12} {'Suggested Threshold':<20}")
    print("-" * 80)

    for token, data in sorted_results:
        # Clean up token name for display
        display_token = token.replace("@xyz", "")
        exchange = data["exchange"]

        print(f"{display_token:<15} {exchange:<8} "
              f"{format_usd(data['bid_liquidity']):<12} "
              f"{format_usd(data['ask_liquidity']):<12} "
              f"{format_usd(data['total_liquidity']):<12} "
              f"{format_usd(suggest_threshold(data['total_liquidity'])):<20}")

    # Output as JSON for further processing
    script_dir = Path(__file__).parent.parent
    output_file = script_dir / "data" / "liquidity_analysis.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_data = {
        "depth_pct": depth_pct,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "results": {
            token: {
                **data,
                "suggested_threshold": suggest_threshold(data["total_liquidity"])
            }
            for token, data in sorted_results
        }
    }

    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\n\nResults saved to {output_file}")

    # Print summary tables by exchange
    print("\n" + "="*80)
    print("MAIN EXCHANGE - TOP 30 BY LIQUIDITY")
    print("="*80)
    main_sorted = [(t, d) for t, d in sorted_results if d["exchange"] == "main"][:30]
    for token, data in main_sorted:
        threshold = suggest_threshold(data["total_liquidity"])
        print(f"{token:<12} | Liq: {format_usd(data['total_liquidity']):<10} | Suggested: {format_usd(threshold)}")

    print("\n" + "="*80)
    print("XYZ EXCHANGE - ALL TOKENS BY LIQUIDITY")
    print("="*80)
    xyz_sorted = [(t.replace("@xyz", ""), d) for t, d in sorted_results if d["exchange"] == "xyz"]
    for token, data in xyz_sorted:
        threshold = suggest_threshold(data["total_liquidity"])
        print(f"{token:<12} | Liq: {format_usd(data['total_liquidity']):<10} | Suggested: {format_usd(threshold)}")


if __name__ == "__main__":
    main()
