"""
Position Cache Module
====================

Smart position caching with tiered refresh based on liquidation distance.

Components:
- CachedPosition: Full position data with caching metadata
- PositionCache: In-memory cache with tier management and SQLite persistence
- TieredRefreshScheduler: Priority-based refresh scheduling
- DiscoveryScheduler: Dynamic frequency discovery for new positions
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .database import MonitorDatabase

logger = logging.getLogger(__name__)


# Tier configuration (imported from settings at runtime to avoid circular imports)
DEFAULT_TIER_CRITICAL_PCT = 0.125
DEFAULT_TIER_HIGH_PCT = 0.25
DEFAULT_REFRESH_CRITICAL_SEC = 0.2
DEFAULT_REFRESH_HIGH_SEC = 2.5
DEFAULT_REFRESH_NORMAL_SEC = 30.0


@dataclass
class CachedPosition:
    """
    Full position data with caching metadata.

    Stores all position information needed for monitoring, plus metadata
    for cache management (refresh timestamps, tier classification).
    """
    # Position identification
    position_key: str
    address: str
    token: str
    exchange: str
    side: str

    # Position data from API
    size: float
    leverage: float
    leverage_type: str
    entry_price: float
    position_value: float
    liq_price: Optional[float]
    margin_used: float
    unrealized_pnl: float

    # Computed fields
    mark_price: float
    distance_pct: Optional[float]
    cohort: str

    # Caching metadata
    refresh_tier: str = "normal"  # "critical", "high", "normal"
    last_full_refresh: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_price_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Tracking
    is_in_watchlist: bool = False

    # Distance tracking for alerts (shows change since last daily summary)
    distance_at_last_summary: Optional[float] = None

    @classmethod
    def from_position_dict(cls, pos: dict, cohort: str, mark_price: float) -> "CachedPosition":
        """
        Create CachedPosition from a position dict (from step2_position).

        Args:
            pos: Position dict with API data
            cohort: Cohort name
            mark_price: Current mark price

        Returns:
            CachedPosition instance
        """
        # Calculate distance if liquidation price exists
        liq_price = pos.get('liquidation_price') or pos.get('liq_price')
        distance_pct = None
        if liq_price is not None and mark_price > 0:
            side = pos.get('side', 'Long')
            if side == 'Long':
                distance_pct = ((mark_price - liq_price) / mark_price) * 100
            else:
                distance_pct = ((liq_price - mark_price) / mark_price) * 100

        address = pos.get('address', '')
        token = pos.get('token', '')
        exchange = pos.get('exchange', 'main')
        side = pos.get('side', 'Long')
        position_key = f"{address}:{token}:{exchange}:{side}"

        now = datetime.now(timezone.utc)

        return cls(
            position_key=position_key,
            address=address,
            token=token,
            exchange=exchange,
            side=side,
            size=float(pos.get('size', 0)),
            leverage=float(pos.get('leverage', 1)),
            leverage_type=pos.get('leverage_type', 'cross'),
            entry_price=float(pos.get('entry_price', 0)),
            position_value=float(pos.get('position_value', 0)),
            liq_price=liq_price,
            margin_used=float(pos.get('margin_used', 0)),
            unrealized_pnl=float(pos.get('unrealized_pnl', 0)),
            mark_price=mark_price,
            distance_pct=distance_pct,
            cohort=cohort,
            refresh_tier=classify_tier(distance_pct),
            last_full_refresh=now,
            last_price_update=now,
            created_at=now,
            is_in_watchlist=pos.get('is_in_watchlist', False),
        )

    def to_dict(self) -> dict:
        """Convert to dict for database storage."""
        return {
            'position_key': self.position_key,
            'address': self.address,
            'token': self.token,
            'exchange': self.exchange,
            'side': self.side,
            'size': self.size,
            'leverage': self.leverage,
            'leverage_type': self.leverage_type,
            'entry_price': self.entry_price,
            'position_value': self.position_value,
            'liq_price': self.liq_price,
            'margin_used': self.margin_used,
            'unrealized_pnl': self.unrealized_pnl,
            'mark_price': self.mark_price,
            'distance_pct': self.distance_pct,
            'cohort': self.cohort,
            'refresh_tier': self.refresh_tier,
            'last_full_refresh': self.last_full_refresh.isoformat(),
            'last_price_update': self.last_price_update.isoformat(),
            'created_at': self.created_at.isoformat(),
            'is_in_watchlist': self.is_in_watchlist,
            'distance_at_last_summary': self.distance_at_last_summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CachedPosition":
        """Create from database dict."""
        return cls(
            position_key=d['position_key'],
            address=d['address'],
            token=d['token'],
            exchange=d['exchange'],
            side=d['side'],
            size=d['size'],
            leverage=d['leverage'],
            leverage_type=d['leverage_type'],
            entry_price=d['entry_price'],
            position_value=d['position_value'],
            liq_price=d['liq_price'],
            margin_used=d['margin_used'],
            unrealized_pnl=d['unrealized_pnl'],
            mark_price=d['mark_price'],
            distance_pct=d['distance_pct'],
            cohort=d['cohort'],
            refresh_tier=d['refresh_tier'],
            last_full_refresh=datetime.fromisoformat(d['last_full_refresh']) if isinstance(d['last_full_refresh'], str) else d['last_full_refresh'],
            last_price_update=datetime.fromisoformat(d['last_price_update']) if isinstance(d['last_price_update'], str) else d['last_price_update'],
            created_at=datetime.fromisoformat(d['created_at']) if isinstance(d['created_at'], str) else d['created_at'],
            is_in_watchlist=d.get('is_in_watchlist', False),
            distance_at_last_summary=d.get('distance_at_last_summary'),
        )

    def update_price(self, mark_price: float):
        """Update mark price and recalculate distance."""
        self.mark_price = mark_price
        self.last_price_update = datetime.now(timezone.utc)

        if self.liq_price is not None and mark_price > 0:
            if self.side == 'Long':
                self.distance_pct = ((mark_price - self.liq_price) / mark_price) * 100
            else:
                self.distance_pct = ((self.liq_price - mark_price) / mark_price) * 100

            # Reclassify tier
            self.refresh_tier = classify_tier(self.distance_pct)
        else:
            self.distance_pct = None
            self.refresh_tier = 'normal'


def classify_tier(
    distance_pct: Optional[float],
    critical_pct: float = DEFAULT_TIER_CRITICAL_PCT,
    high_pct: float = DEFAULT_TIER_HIGH_PCT
) -> str:
    """
    Classify position into refresh tier based on distance.

    Args:
        distance_pct: Distance to liquidation (%)
        critical_pct: Threshold for critical tier
        high_pct: Threshold for high tier

    Returns:
        Tier name: 'critical', 'high', or 'normal'
    """
    if distance_pct is None:
        return 'normal'
    if distance_pct <= critical_pct:
        return 'critical'
    if distance_pct <= high_pct:
        return 'high'
    return 'normal'


class PositionCache:
    """
    In-memory position cache with tier management and SQLite persistence.

    Maintains all positions with their current state, organized by refresh tier.
    Persists to SQLite on every update for restart recovery.
    """

    def __init__(self, db: "MonitorDatabase"):
        """
        Initialize the position cache.

        Args:
            db: MonitorDatabase instance for persistence
        """
        self.db = db
        self.positions: Dict[str, CachedPosition] = {}
        self.tier_queues: Dict[str, List[str]] = {
            'critical': [],
            'high': [],
            'normal': [],
        }

    def load_from_db(self) -> int:
        """
        Load cache from SQLite on startup.

        Returns:
            Number of positions loaded
        """
        rows = self.db.load_position_cache()
        self.positions.clear()
        self.tier_queues = {'critical': [], 'high': [], 'normal': []}

        for row in rows:
            pos = CachedPosition.from_dict(row)
            self.positions[pos.position_key] = pos
            self.tier_queues[pos.refresh_tier].append(pos.position_key)

        logger.info(f"Loaded {len(self.positions)} positions from cache")
        return len(self.positions)

    def update_prices(self, mark_prices: Dict[str, float]):
        """
        Update all positions with new mark prices and reclassify tiers.

        Args:
            mark_prices: Dict of token -> price (may include exchange prefixes)
        """
        # Reset tier queues
        self.tier_queues = {'critical': [], 'high': [], 'normal': []}
        positions_to_update = []

        for key, pos in self.positions.items():
            # Get price for this token (try exchange-prefixed first for sub-exchanges)
            price = None
            if pos.exchange != 'main':
                prefixed = f"{pos.exchange}:{pos.token}"
                price = mark_prices.get(prefixed)
            if price is None:
                price = mark_prices.get(pos.token)

            if price is not None and price > 0:
                old_tier = pos.refresh_tier
                pos.update_price(price)

                # Track for batch DB update
                positions_to_update.append({
                    'position_key': pos.position_key,
                    'mark_price': pos.mark_price,
                    'distance_pct': pos.distance_pct,
                    'refresh_tier': pos.refresh_tier,
                    'last_price_update': pos.last_price_update.isoformat(),
                })

            # Add to tier queue
            self.tier_queues[pos.refresh_tier].append(key)

        # Sort tier queues by distance (closest first)
        for tier in self.tier_queues:
            self.tier_queues[tier].sort(
                key=lambda k: self.positions[k].distance_pct or float('inf')
            )

        # Batch update database
        if positions_to_update:
            self._batch_update_prices(positions_to_update)

    def _batch_update_prices(self, updates: List[dict]):
        """Batch update price data in database."""
        # For immediate persistence, we update each position
        # This could be optimized with a single transaction if needed
        for upd in updates:
            self.db.update_position_price(
                upd['position_key'],
                upd['mark_price'],
                upd['distance_pct'],
                upd['refresh_tier'],
                upd['last_price_update'],
            )

    def update_position(self, position: CachedPosition, persist: bool = True):
        """
        Update or add a single position.

        Args:
            position: CachedPosition to update
            persist: Whether to immediately persist to SQLite
        """
        old_tier = None
        if position.position_key in self.positions:
            old_pos = self.positions[position.position_key]
            old_tier = old_pos.refresh_tier

        self.positions[position.position_key] = position

        # Update tier queues
        if old_tier and old_tier != position.refresh_tier:
            # Remove from old tier
            if position.position_key in self.tier_queues[old_tier]:
                self.tier_queues[old_tier].remove(position.position_key)
            # Add to new tier
            self.tier_queues[position.refresh_tier].append(position.position_key)
        elif old_tier is None:
            # New position
            self.tier_queues[position.refresh_tier].append(position.position_key)

        # Persist to SQLite
        if persist:
            self.db.save_cached_position(position.to_dict())

    def update_positions_batch(self, positions: List[CachedPosition]):
        """
        Update multiple positions efficiently.

        Args:
            positions: List of CachedPosition to update
        """
        for pos in positions:
            old_tier = None
            if pos.position_key in self.positions:
                old_tier = self.positions[pos.position_key].refresh_tier

            self.positions[pos.position_key] = pos

            # Update tier queues
            if old_tier and old_tier != pos.refresh_tier:
                if pos.position_key in self.tier_queues[old_tier]:
                    self.tier_queues[old_tier].remove(pos.position_key)
                self.tier_queues[pos.refresh_tier].append(pos.position_key)
            elif old_tier is None:
                self.tier_queues[pos.refresh_tier].append(pos.position_key)

        # Batch persist
        self.db.save_cached_positions_batch([p.to_dict() for p in positions])
        logger.debug(f"Updated {len(positions)} positions in cache")

    def remove_position(self, position_key: str):
        """Remove a position from cache."""
        if position_key in self.positions:
            tier = self.positions[position_key].refresh_tier
            del self.positions[position_key]
            if position_key in self.tier_queues[tier]:
                self.tier_queues[tier].remove(position_key)
            self.db.delete_cached_positions([position_key])

    def remove_closed_positions(self, open_position_keys: Set[str]):
        """
        Remove positions that are no longer open.

        Args:
            open_position_keys: Set of currently open position keys
        """
        to_remove = [k for k in self.positions if k not in open_position_keys]
        for key in to_remove:
            self.remove_position(key)

        if to_remove:
            logger.info(f"Removed {len(to_remove)} closed positions from cache")

    def get_tier_counts(self) -> Dict[str, int]:
        """Get count of positions in each tier."""
        return {
            'critical': len(self.tier_queues['critical']),
            'high': len(self.tier_queues['high']),
            'normal': len(self.tier_queues['normal']),
        }

    def get_positions_by_tier(self, tier: str) -> List[CachedPosition]:
        """Get all positions in a specific tier."""
        return [self.positions[k] for k in self.tier_queues.get(tier, [])]

    def get_watchlist_positions(self) -> List[CachedPosition]:
        """Get all positions marked as in watchlist."""
        return [p for p in self.positions.values() if p.is_in_watchlist]

    def get_oldest_refresh(self) -> Optional[datetime]:
        """Get the oldest last_full_refresh timestamp."""
        if not self.positions:
            return None
        return min(p.last_full_refresh for p in self.positions.values())


class TieredRefreshScheduler:
    """
    Manages position refresh with tier-based priority.

    Ensures critical positions are refreshed as fast as rate limits allow,
    while high and normal positions get less frequent updates.
    """

    def __init__(
        self,
        cache: PositionCache,
        critical_interval: float = DEFAULT_REFRESH_CRITICAL_SEC,
        high_interval: float = DEFAULT_REFRESH_HIGH_SEC,
        normal_interval: float = DEFAULT_REFRESH_NORMAL_SEC
    ):
        """
        Initialize the refresh scheduler.

        Args:
            cache: PositionCache to schedule refreshes for
            critical_interval: Seconds between critical tier refreshes
            high_interval: Seconds between high tier refreshes
            normal_interval: Seconds between normal tier refreshes
        """
        self.cache = cache
        self.intervals = {
            'critical': critical_interval,
            'high': high_interval,
            'normal': normal_interval,
        }
        self.last_refresh: Dict[str, float] = {}  # position_key -> timestamp

    def get_next_position(self) -> Optional[str]:
        """
        Get next position needing refresh based on tier priority.

        Returns:
            Position key to refresh, or None if nothing needs refresh
        """
        now = time.time()

        # Check each tier in priority order
        for tier in ['critical', 'high', 'normal']:
            interval = self.intervals[tier]
            for key in self.cache.tier_queues[tier]:
                last = self.last_refresh.get(key, 0)
                if now - last >= interval:
                    return key

        return None

    def get_positions_to_refresh(self, max_count: int = 10) -> List[str]:
        """
        Get multiple positions needing refresh.

        Args:
            max_count: Maximum number of positions to return

        Returns:
            List of position keys to refresh
        """
        now = time.time()
        to_refresh = []

        for tier in ['critical', 'high', 'normal']:
            if len(to_refresh) >= max_count:
                break

            interval = self.intervals[tier]
            for key in self.cache.tier_queues[tier]:
                if len(to_refresh) >= max_count:
                    break
                last = self.last_refresh.get(key, 0)
                if now - last >= interval:
                    to_refresh.append(key)

        return to_refresh

    def mark_refreshed(self, position_key: str):
        """Record that a position was refreshed."""
        self.last_refresh[position_key] = time.time()

    def mark_refreshed_batch(self, position_keys: List[str]):
        """Record that multiple positions were refreshed."""
        now = time.time()
        for key in position_keys:
            self.last_refresh[key] = now

    def clear_stale_entries(self):
        """Remove refresh tracking for positions no longer in cache."""
        valid_keys = set(self.cache.positions.keys())
        self.last_refresh = {
            k: v for k, v in self.last_refresh.items()
            if k in valid_keys
        }

    def has_critical_positions(self) -> bool:
        """Check if there are any positions in the critical tier."""
        return len(self.cache.tier_queues.get('critical', [])) > 0

    def get_critical_exchanges(self) -> set:
        """
        Get the set of exchanges that have critical positions.

        Returns:
            Set of exchange names (e.g., {"main", "xyz"})
        """
        exchanges = set()
        for position_key in self.cache.tier_queues.get('critical', []):
            pos = self.cache.positions.get(position_key)
            if pos:
                # Normalize: empty string means "main"
                exchange = pos.exchange if pos.exchange else "main"
                exchanges.add(exchange)
        return exchanges


class DiscoveryScheduler:
    """
    Manages cohort/position discovery with dynamic frequency.

    Discovery frequency adjusts based on API pressure from critical positions:
    - More critical positions = less frequent discovery (preserve API budget)
    - Fewer critical positions = more frequent discovery (use spare budget)
    """

    def __init__(
        self,
        cache: PositionCache,
        db: "MonitorDatabase",
        min_interval_minutes: int = 30,
        max_interval_minutes: int = 240,
        critical_weight: int = 15,
        high_weight: int = 5
    ):
        """
        Initialize the discovery scheduler.

        Args:
            cache: PositionCache for tier count queries
            db: MonitorDatabase for known addresses
            min_interval_minutes: Minimum interval between discoveries
            max_interval_minutes: Maximum interval between discoveries
            critical_weight: Minutes to add per critical position
            high_weight: Minutes to add per high position
        """
        self.cache = cache
        self.db = db
        self.min_interval = min_interval_minutes
        self.max_interval = max_interval_minutes
        self.critical_weight = critical_weight
        self.high_weight = high_weight

        self.last_discovery: Optional[datetime] = None
        self.known_addresses: Set[str] = set()

    def load_known_addresses(self):
        """Load known addresses from database."""
        self.known_addresses = self.db.load_known_addresses()
        logger.info(f"Loaded {len(self.known_addresses)} known addresses")

    def get_discovery_interval_minutes(self) -> int:
        """
        Calculate discovery interval based on API pressure.

        Returns:
            Interval in minutes
        """
        tier_counts = self.cache.get_tier_counts()
        pressure = (
            tier_counts['critical'] * self.critical_weight +
            tier_counts['high'] * self.high_weight
        )
        interval = self.min_interval + pressure
        return min(max(interval, self.min_interval), self.max_interval)

    def should_run_discovery(self) -> bool:
        """
        Check if discovery scan should run based on API pressure.

        Returns:
            True if discovery should run
        """
        if self.last_discovery is None:
            return True

        interval_minutes = self.get_discovery_interval_minutes()
        elapsed = (datetime.now(timezone.utc) - self.last_discovery).total_seconds() / 60

        return elapsed >= interval_minutes

    def find_new_addresses(self, current_addresses: List[tuple]) -> List[tuple]:
        """
        Find addresses not in known_addresses.

        Args:
            current_addresses: List of (address, cohort) tuples

        Returns:
            List of new (address, cohort) tuples
        """
        return [
            (addr, cohort)
            for addr, cohort in current_addresses
            if addr not in self.known_addresses
        ]

    def mark_discovery_complete(self, new_addresses: List[tuple]):
        """
        Record that discovery completed.

        Args:
            new_addresses: List of (address, cohort) tuples that were discovered
        """
        self.last_discovery = datetime.now(timezone.utc)

        # Update known addresses
        for addr, cohort in new_addresses:
            self.known_addresses.add(addr)

        # Persist to database
        if new_addresses:
            self.db.save_known_addresses_batch(new_addresses)
            logger.info(f"Discovery complete: {len(new_addresses)} new addresses")

        # Save discovery timestamp
        self.db.set_state('last_discovery', self.last_discovery.isoformat())

    def restore_last_discovery(self):
        """Restore last discovery timestamp from database."""
        value = self.db.get_state('last_discovery')
        if value:
            try:
                self.last_discovery = datetime.fromisoformat(value)
            except ValueError:
                self.last_discovery = None
