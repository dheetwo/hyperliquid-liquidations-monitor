#!/usr/bin/env python3
"""
Fetch liquidation messages from Telegram channel via web preview.

Scheduled script that fetches recent messages from @liquidations_hyperliquid
using Telegram's public web preview (no API credentials required) and adds
qualifying addresses to the wallet registry.

Usage:
    # Run fetch (fetches recent messages from web preview)
    python scripts/fetch_liq_channel.py

    # Dry run (don't add to database)
    python scripts/fetch_liq_channel.py --dry-run

Cron setup (hourly):
    0 * * * * cd /path/to/kolkata && python scripts/fetch_liq_channel.py >> logs/liq_fetch.log 2>&1

No API credentials required - uses public web preview at t.me/s/channel_name
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config
from src.db.wallet_db import WalletDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Message Parsing (reused from import_liq_history.py)
# =============================================================================

MESSAGE_PATTERN = re.compile(
    r'([🔴🟢])\s*'  # Direction emoji
    r'#(\[?[\w]+\]?:?[\w]+)\s+'  # Token (with optional [xyz]: prefix)
    r'(Long|Short)\s+Liquidation:\s*'  # Direction text
    r'\$([0-9,.]+)([KMB]?)\s*'  # Notional value
    r'@\s*\$?([0-9,.]+)',  # Price
    re.IGNORECASE
)

ADDRESS_PATTERN = re.compile(r'0x[a-fA-F0-9]{40}')
MULTIPLIERS = {'': 1, 'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}


def parse_message_text(text: str) -> tuple | None:
    """
    Parse a Telegram liquidation message text.

    Returns:
        (address, token, side, notional, price) or None
    """
    # Extract address
    address_match = ADDRESS_PATTERN.search(text)
    if not address_match:
        return None
    address = address_match.group(0).lower()

    # Parse content
    match = MESSAGE_PATTERN.search(text)
    if not match:
        return None

    emoji, token_raw, side, notional_str, multiplier, price_str = match.groups()

    # Parse token
    token_raw = token_raw.lstrip('#')
    if token_raw.startswith('['):
        m = re.match(r'\[(\w+)\]:(\w+)', token_raw)
        token = f"{m.group(1)}:{m.group(2)}" if m else token_raw
    elif ':' in token_raw:
        token = token_raw
    else:
        token = token_raw.upper()

    # Parse notional
    notional = float(notional_str.replace(',', ''))
    notional *= MULTIPLIERS.get(multiplier.upper() if multiplier else '', 1)

    # Parse price
    price = float(price_str.replace(',', ''))

    return address, token, side.capitalize(), notional, price


# =============================================================================
# Web Preview Fetcher
# =============================================================================

def fetch_channel_messages(channel: str) -> list[str]:
    """
    Fetch recent messages from a Telegram channel via web preview.

    Uses https://t.me/s/channel_name which shows recent messages publicly.
    No API credentials required.

    Args:
        channel: Channel username (without @)

    Returns:
        List of message text strings
    """
    url = f"https://t.me/s/{channel}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch channel: {e}")
        return []

    html = response.text

    # Extract message text from the HTML
    # Messages are in <div class="tgme_widget_message_text">...</div>
    message_pattern = re.compile(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL
    )

    # Also extract links which contain addresses
    # Links are in <a href="...">...</a> within messages
    link_pattern = re.compile(r'href="([^"]*0x[a-fA-F0-9]{40}[^"]*)"')

    messages = []
    for match in message_pattern.finditer(html):
        msg_html = match.group(1)

        # Clean HTML tags but preserve text
        # Replace <br> with newlines
        text = re.sub(r'<br\s*/?>', '\n', msg_html)
        # Remove other tags but keep content
        text = re.sub(r'<[^>]+>', ' ', text)
        # Decode HTML entities
        text = text.replace('&#036;', '$')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&#39;', "'")
        text = text.replace('&quot;', '"')
        text = text.replace('&nbsp;', ' ')
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Also try to find address in links within this message block
        links = link_pattern.findall(msg_html)
        for link in links:
            addr_match = ADDRESS_PATTERN.search(link)
            if addr_match:
                # Append address to text if not already present
                addr = addr_match.group(0)
                if addr.lower() not in text.lower():
                    text = f"{text} {addr}"

        if text:
            messages.append(text)

    logger.info(f"Fetched {len(messages)} messages from web preview")
    return messages


# =============================================================================
# Main Processing
# =============================================================================

def process_messages(
    messages: list,
    wallet_db: WalletDB,
    min_notional: float = 50_000,
    dry_run: bool = False,
) -> dict:
    """
    Process liquidation messages and add to wallet DB.

    Args:
        messages: List of message text strings
        wallet_db: Wallet database
        min_notional: Absolute minimum notional to consider
        dry_run: If True, don't actually add to database

    Returns:
        Dict with processing stats
    """
    # Track addresses with their best qualifying liquidation
    addresses: dict[str, tuple[float, bool, str | None]] = {}
    total_liqs = 0
    skipped_below_min = 0
    qualifying_liqs = 0

    for text in messages:
        result = parse_message_text(text)
        if not result:
            continue

        address, token, side, notional, price = result
        total_liqs += 1

        # Skip tiny liquidations
        if notional < min_notional:
            skipped_below_min += 1
            continue

        # Determine exchange from token prefix
        if token.startswith("xyz:") or token.startswith("[xyz]"):
            exchange = "xyz"
            token_name = token.replace("xyz:", "").replace("[xyz]:", "")
        else:
            exchange = ""
            token_name = token

        # Get the isolated threshold for this token
        threshold = config.get_notional_threshold(token_name, exchange, is_isolated=True)
        qualifies = notional >= threshold

        if qualifies:
            qualifying_liqs += 1

        # Update address tracking
        if address not in addresses:
            addresses[address] = (notional, qualifies, token_name if qualifies else None)
        else:
            prev_notional, prev_qualifies, prev_token = addresses[address]
            new_qualifies = prev_qualifies or qualifies
            new_token = prev_token if prev_qualifies else (token_name if qualifies else None)
            new_notional = max(prev_notional, notional)
            addresses[address] = (new_notional, new_qualifies, new_token)

    # Count results
    normal_count = sum(1 for _, qualifies, _ in addresses.values() if qualifies)
    infrequent_count = len(addresses) - normal_count

    logger.info(f"Parsed {total_liqs} liquidation events")
    logger.info(f"Skipped {skipped_below_min} below ${min_notional:,.0f} absolute minimum")
    logger.info(f"Found {qualifying_liqs} liquidations meeting token-specific thresholds")
    logger.info(f"Unique addresses: {len(addresses)} ({normal_count} normal, {infrequent_count} infrequent)")

    # Add addresses to wallet DB
    new_count = 0
    if not dry_run:
        for address, (notional, qualifies, token) in addresses.items():
            freq = "normal" if qualifies else "infrequent"
            was_new = wallet_db.add_wallet(
                address,
                source="liq_feed",
                position_value=notional,
                scan_frequency=freq
            )
            if was_new:
                new_count += 1
        logger.info(f"Added {new_count} new addresses to wallet registry")
    else:
        logger.info("[DRY RUN] Would add addresses to wallet registry")

    return {
        "total_messages": len(messages),
        "total_liqs": total_liqs,
        "skipped_below_min": skipped_below_min,
        "qualifying_liqs": qualifying_liqs,
        "unique_addresses": len(addresses),
        "normal_count": normal_count,
        "infrequent_count": infrequent_count,
        "new_added": new_count,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch liquidation messages from Telegram")
    parser.add_argument(
        "--channel",
        default="liquidations_hyperliquid",
        help="Telegram channel username (default: liquidations_hyperliquid)"
    )
    parser.add_argument(
        "--min-notional",
        type=float,
        default=50_000,
        help="Minimum notional to consider (default: 50000)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't add addresses to database"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Path to wallet database"
    )

    args = parser.parse_args()

    # Initialize wallet DB
    wallet_db = WalletDB(args.db_path) if args.db_path else WalletDB()

    # Fetch messages from web preview
    messages = fetch_channel_messages(args.channel)

    if not messages:
        logger.info("No messages to process")
        return

    # Process messages
    stats = process_messages(
        messages=messages,
        wallet_db=wallet_db,
        min_notional=args.min_notional,
        dry_run=args.dry_run,
    )

    # Show final wallet DB stats
    if not args.dry_run:
        db_stats = wallet_db.get_stats()
        logger.info(f"Wallet registry now has {db_stats.total_wallets} wallets "
                   f"({db_stats.from_liq_history} from liq sources)")


if __name__ == "__main__":
    main()
