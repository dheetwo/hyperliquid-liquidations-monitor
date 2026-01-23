"""
Wallet Filtering

Determines which wallets should be scanned based on:
- Time since last scan
- Position value (scan frequency classification)
- Wallet value thresholds
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List

from ..config import Wallet, config

logger = logging.getLogger(__name__)


def should_scan_wallet(wallet: Wallet) -> bool:
    """
    Determine if a wallet should be scanned.

    Args:
        wallet: Wallet from the registry

    Returns:
        True if wallet should be scanned
    """
    # Always scan wallets that have never been scanned
    if wallet.last_scanned is None:
        return True

    # Parse last scan time
    try:
        last_scanned = datetime.fromisoformat(wallet.last_scanned)
    except (ValueError, TypeError):
        return True  # Invalid timestamp, scan anyway

    now = datetime.now(timezone.utc)

    # Check scan frequency
    if wallet.scan_frequency == "normal":
        # Normal wallets are always scanned during discovery
        return True
    elif wallet.scan_frequency == "infrequent":
        # Infrequent wallets only scanned every N hours
        age = now - last_scanned
        return age > timedelta(hours=config.infrequent_scan_interval_hours)

    # Default: scan
    return True


def filter_wallets_for_scan(
    wallets: List[Wallet],
    include_infrequent: bool = False,
) -> List[Wallet]:
    """
    Filter a list of wallets to only those that should be scanned.

    Args:
        wallets: List of wallets from registry
        include_infrequent: Whether to include infrequent wallets
                           (typically done once per day)

    Returns:
        Filtered list of wallets to scan
    """
    result = []

    for wallet in wallets:
        if wallet.scan_frequency == "infrequent" and not include_infrequent:
            continue

        if should_scan_wallet(wallet):
            result.append(wallet)

    logger.info(f"Filtered {len(wallets)} wallets to {len(result)} for scanning")
    return result


def filter_wallets_by_value(
    wallets: List[Wallet],
    min_value: float = None,
) -> List[Wallet]:
    """
    Filter wallets by minimum position value.

    Args:
        wallets: List of wallets
        min_value: Minimum position value (default from config)

    Returns:
        Filtered list
    """
    min_value = min_value if min_value is not None else config.min_wallet_value

    return [
        w for w in wallets
        if w.position_value is None or w.position_value >= min_value
    ]
