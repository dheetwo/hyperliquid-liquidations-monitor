#!/usr/bin/env python3
"""
Import liquidation history from Telegram exports.

Parses Telegram channel export files and adds liquidated addresses
to the wallet registry.

Usage:
    python scripts/import_liq_history.py <export.json>
    python scripts/import_liq_history.py --add <address> [--notional 1000000]
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

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
# Parser for Telegram liquidation messages
# =============================================================================

# Regex patterns from archived code
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


def parse_message(message: str) -> Optional[Tuple[str, str, float]]:
    """
    Parse a Telegram liquidation message.

    Returns:
        (address, token, notional) or None if not a valid liquidation message
    """
    # Extract address from links
    address_match = ADDRESS_PATTERN.search(message)
    if not address_match:
        return None
    address = address_match.group(0).lower()

    # Parse content
    match = MESSAGE_PATTERN.search(message)
    if not match:
        return None

    _, token_raw, _, notional_str, multiplier, _ = match.groups()

    # Parse token
    token_raw = token_raw.lstrip('#')
    if token_raw.startswith('['):
        # [xyz]:TOKEN format
        m = re.match(r'\[(\w+)\]:(\w+)', token_raw)
        token = f"{m.group(1)}:{m.group(2)}" if m else token_raw
    elif ':' in token_raw:
        token = token_raw
    else:
        token = token_raw.upper()

    # Parse notional
    notional = float(notional_str.replace(',', ''))
    notional *= MULTIPLIERS.get(multiplier.upper() if multiplier else '', 1)

    return address, token, notional


def extract_from_message(msg: dict) -> Optional[Tuple[str, str, str, float, float, str]]:
    """
    Extract liquidation data from a Telegram message.

    Returns:
        (address, token, side, notional, price, timestamp) or None
    """
    text_parts = msg.get('text', '')
    if not isinstance(text_parts, list):
        return None

    # Extract address from href in text_link objects
    address = None
    for part in text_parts:
        if isinstance(part, dict) and part.get('type') == 'text_link':
            href = part.get('href', '')
            addr_match = ADDRESS_PATTERN.search(href)
            if addr_match:
                address = addr_match.group(0).lower()
                break

    if not address:
        return None

    # Build text string for parsing
    text = ''
    for part in text_parts:
        if isinstance(part, str):
            text += part
        elif isinstance(part, dict):
            text += part.get('text', '')

    # Parse the message content
    match = MESSAGE_PATTERN.search(text)
    if not match:
        return None

    emoji, token_raw, side, notional_str, multiplier, price_str = match.groups()

    # Parse token
    token_raw = token_raw.lstrip('#')
    if token_raw.startswith('['):
        # [xyz]:TOKEN format
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

    # Get timestamp
    timestamp = msg.get('date', '')

    return address, token, side, notional, price, timestamp


def import_telegram_export(export_path: Path, wallet_db: WalletDB, min_notional: float = 50_000) -> int:
    """
    Import liquidations from Telegram export JSON.

    Each liquidation is treated as an isolated position. If the notional meets
    the isolated threshold for that token, the wallet is marked as "normal" frequency.
    Otherwise it's marked as "infrequent".

    Args:
        export_path: Path to exported JSON file
        wallet_db: Wallet database to add addresses to
        min_notional: Absolute minimum notional to consider (default $50K)

    Returns:
        Number of addresses added
    """
    with open(export_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    messages = data.get('messages', [])

    # Track addresses with their best qualifying liquidation
    # Key: address, Value: (max_notional, qualifies_for_normal, qualifying_token)
    addresses: dict[str, tuple[float, bool, str]] = {}
    total_liqs = 0
    skipped_below_min = 0
    qualifying_liqs = 0

    for msg in messages:
        if msg.get('type') != 'message':
            continue

        result = extract_from_message(msg)
        if result:
            address, token, side, notional, price, timestamp = result
            total_liqs += 1

            # Skip tiny liquidations
            if notional < min_notional:
                skipped_below_min += 1
                continue

            # Determine exchange from token prefix (xyz:TOKEN format)
            if token.startswith("xyz:") or token.startswith("[xyz]"):
                exchange = "xyz"
                token_name = token.replace("xyz:", "").replace("[xyz]:", "")
            else:
                exchange = ""
                token_name = token

            # Get the isolated threshold for this token
            # All liquidations treated as isolated (conservative assumption)
            threshold = config.get_notional_threshold(token_name, exchange, is_isolated=True)
            qualifies = notional >= threshold

            if qualifies:
                qualifying_liqs += 1

            # Update address tracking
            if address not in addresses:
                addresses[address] = (notional, qualifies, token_name if qualifies else None)
            else:
                prev_notional, prev_qualifies, prev_token = addresses[address]
                # Keep max notional, and upgrade to qualifying if this one qualifies
                new_qualifies = prev_qualifies or qualifies
                new_token = prev_token if prev_qualifies else (token_name if qualifies else None)
                new_notional = max(prev_notional, notional)
                addresses[address] = (new_notional, new_qualifies, new_token)

    # Count results
    normal_count = sum(1 for _, qualifies, _ in addresses.values() if qualifies)
    infrequent_count = len(addresses) - normal_count

    logger.info(f"Parsed {total_liqs} liquidation events")
    logger.info(f"Skipped {skipped_below_min} below ${min_notional:,.0f} absolute minimum")
    logger.info(f"Found {qualifying_liqs} liquidations meeting token-specific isolated thresholds")
    logger.info(f"Unique addresses: {len(addresses)} ({normal_count} normal, {infrequent_count} infrequent)")

    # Add addresses to wallet DB
    new_count = 0
    upgraded_count = 0
    for address, (notional, qualifies, token) in addresses.items():
        freq = "normal" if qualifies else "infrequent"
        was_new = wallet_db.add_wallet(
            address,
            source="liq_history",
            position_value=notional,
            scan_frequency=freq
        )
        if was_new:
            new_count += 1

    logger.info(f"Added {new_count} new addresses to wallet registry")

    return new_count


def add_single_address(address: str, wallet_db: WalletDB, notional: float = None) -> bool:
    """Add a single address manually."""
    address = address.lower()
    if not ADDRESS_PATTERN.match(address):
        logger.error(f"Invalid address format: {address}")
        return False

    added = wallet_db.add_wallet(address, source="liq_history", position_value=notional)
    if added:
        logger.info(f"Added address: {address}")
    else:
        logger.info(f"Address already exists: {address}")
    return added


def main():
    parser = argparse.ArgumentParser(description="Import liquidation history")
    parser.add_argument(
        "export_file",
        nargs="?",
        help="Path to Telegram export JSON file"
    )
    parser.add_argument(
        "--add",
        metavar="ADDRESS",
        help="Add a single address manually"
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=100_000,
        help="Minimum notional for import (default: 100000)"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Path to wallet database (default: data/wallets.db)"
    )

    args = parser.parse_args()

    # Initialize wallet DB
    wallet_db = WalletDB(args.db_path) if args.db_path else WalletDB()

    if args.add:
        # Add single address
        add_single_address(args.add, wallet_db, args.notional)

    elif args.export_file:
        # Import from file
        export_path = Path(args.export_file)
        if not export_path.exists():
            logger.error(f"File not found: {export_path}")
            sys.exit(1)

        import_telegram_export(export_path, wallet_db, args.notional)

    else:
        parser.print_help()
        sys.exit(1)

    # Show stats
    stats = wallet_db.get_stats()
    print(f"\nWallet Registry Stats:")
    print(f"  Total wallets: {stats.total_wallets}")
    print(f"  From Hyperdash: {stats.from_hyperdash}")
    print(f"  From liq history: {stats.from_liq_history}")


if __name__ == "__main__":
    main()
