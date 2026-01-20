"""
SQLite Persistence for Monitor Service
======================================

Lightweight persistence layer for:
- Watchlist state (survives restarts)
- Baseline position keys
- Position history (with retention)
- Alert log
- Service logs (persistent logging)

Storage: data/monitor.db (persisted via Docker volume)
"""

import logging
import logging.handlers
import sqlite3
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from queue import Queue, Empty
from typing import Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .service import WatchedPosition

logger = logging.getLogger(__name__)

# Default retention periods
POSITION_HISTORY_RETENTION_DAYS = 7
ALERT_LOG_RETENTION_DAYS = 30
SERVICE_LOG_RETENTION_DAYS = 7

# Database path
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "monitor.db"


class MonitorDatabase:
    """
    SQLite persistence for the monitor service.

    Designed for minimal overhead:
    - WAL mode for concurrent reads
    - Automatic pruning of old data
    - Lightweight schema
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_schema()
        logger.info(f"Database initialized: {self.db_path}")

    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper cleanup."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")

            # Watchlist: current watched positions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    position_key TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    token TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,
                    liq_price REAL NOT NULL,
                    position_value REAL NOT NULL,
                    is_isolated INTEGER NOT NULL,
                    hunting_score REAL NOT NULL,
                    last_distance_pct REAL,
                    last_mark_price REAL,
                    threshold_pct REAL,
                    alerted_proximity INTEGER DEFAULT 0,
                    alerted_critical INTEGER DEFAULT 0,
                    in_critical_zone INTEGER DEFAULT 0,
                    first_seen_scan TEXT,
                    alert_message_id INTEGER,
                    last_proximity_message_id INTEGER,
                    updated_at TEXT NOT NULL
                )
            """)

            # Baseline position keys
            conn.execute("""
                CREATE TABLE IF NOT EXISTS baseline_positions (
                    position_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
            """)

            # Position history (for tracking changes over time)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_key TEXT NOT NULL,
                    liq_price REAL NOT NULL,
                    position_value REAL NOT NULL,
                    distance_pct REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_position_history_key_time
                ON position_history(position_key, timestamp)
            """)

            # Alert log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_key TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    message_id INTEGER,
                    details TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_log_time
                ON alert_log(timestamp)
            """)

            # Service state (key-value store for misc state)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                )
            """)

            # Service logs (persists logs to database)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS service_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    logger_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    exc_info TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_service_logs_time
                ON service_logs(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_service_logs_level
                ON service_logs(level)
            """)

            # Position cache: full position data with caching metadata
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_cache (
                    position_key TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    token TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,

                    -- Position data
                    size REAL NOT NULL,
                    leverage REAL NOT NULL,
                    leverage_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    position_value REAL NOT NULL,
                    liq_price REAL,
                    margin_used REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,

                    -- Computed fields
                    mark_price REAL NOT NULL,
                    distance_pct REAL,
                    cohort TEXT NOT NULL,

                    -- Caching metadata
                    refresh_tier TEXT NOT NULL DEFAULT 'normal',
                    last_full_refresh TEXT NOT NULL,
                    last_price_update TEXT NOT NULL,
                    created_at TEXT NOT NULL,

                    -- Tracking
                    hunting_score REAL,
                    is_in_watchlist INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_position_cache_tier
                ON position_cache(refresh_tier)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_position_cache_distance
                ON position_cache(distance_pct)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_position_cache_address
                ON position_cache(address)
            """)

            # Known addresses: tracks discovered wallet addresses
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_addresses (
                    address TEXT PRIMARY KEY,
                    cohort TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_scanned TEXT NOT NULL
                )
            """)

    # =========================================================================
    # Watchlist Operations
    # =========================================================================

    def save_watchlist(self, watchlist: Dict[str, "WatchedPosition"]):
        """
        Save entire watchlist to database (replaces existing).

        Args:
            watchlist: Dict of position_key -> WatchedPosition
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            # Clear existing watchlist
            conn.execute("DELETE FROM watchlist")

            # Insert all positions
            for key, pos in watchlist.items():
                conn.execute("""
                    INSERT INTO watchlist (
                        position_key, address, token, exchange, side,
                        liq_price, position_value, is_isolated, hunting_score,
                        last_distance_pct, last_mark_price, threshold_pct,
                        alerted_proximity, alerted_critical, in_critical_zone,
                        first_seen_scan, alert_message_id, last_proximity_message_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    key, pos.address, pos.token, pos.exchange, pos.side,
                    pos.liq_price, pos.position_value, int(pos.is_isolated), pos.hunting_score,
                    pos.last_distance_pct, pos.last_mark_price, pos.threshold_pct,
                    int(pos.alerted_proximity), int(pos.alerted_critical), int(pos.in_critical_zone),
                    pos.first_seen_scan, pos.alert_message_id, pos.last_proximity_message_id,
                    now
                ))

        logger.debug(f"Saved {len(watchlist)} positions to watchlist")

    def load_watchlist(self) -> List[dict]:
        """
        Load watchlist from database.

        Returns:
            List of position dicts (to be converted to WatchedPosition by caller)
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM watchlist")
            rows = cursor.fetchall()

        positions = []
        for row in rows:
            positions.append({
                'position_key': row['position_key'],
                'address': row['address'],
                'token': row['token'],
                'exchange': row['exchange'],
                'side': row['side'],
                'liq_price': row['liq_price'],
                'position_value': row['position_value'],
                'is_isolated': bool(row['is_isolated']),
                'hunting_score': row['hunting_score'],
                'last_distance_pct': row['last_distance_pct'],
                'last_mark_price': row['last_mark_price'],
                'threshold_pct': row['threshold_pct'],
                'alerted_proximity': bool(row['alerted_proximity']),
                'alerted_critical': bool(row['alerted_critical']),
                'in_critical_zone': bool(row['in_critical_zone']),
                'first_seen_scan': row['first_seen_scan'],
                'alert_message_id': row['alert_message_id'],
                'last_proximity_message_id': row['last_proximity_message_id'],
            })

        logger.info(f"Loaded {len(positions)} positions from watchlist")
        return positions

    def clear_watchlist(self):
        """Clear all positions from watchlist."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM watchlist")
        logger.debug("Watchlist cleared")

    # =========================================================================
    # Baseline Operations
    # =========================================================================

    def save_baseline(self, position_keys: Set[str]):
        """
        Save baseline position keys (replaces existing).

        Args:
            position_keys: Set of position keys in baseline
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("DELETE FROM baseline_positions")

            for key in position_keys:
                conn.execute(
                    "INSERT INTO baseline_positions (position_key, created_at) VALUES (?, ?)",
                    (key, now)
                )

        logger.debug(f"Saved {len(position_keys)} baseline position keys")

    def load_baseline(self) -> Set[str]:
        """
        Load baseline position keys.

        Returns:
            Set of position keys
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT position_key FROM baseline_positions")
            keys = {row['position_key'] for row in cursor.fetchall()}

        logger.info(f"Loaded {len(keys)} baseline position keys")
        return keys

    def clear_baseline(self):
        """Clear baseline position keys."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM baseline_positions")
        logger.debug("Baseline cleared")

    # =========================================================================
    # Position Cache Operations
    # =========================================================================

    def save_cached_position(self, position: dict):
        """
        Save or update a single cached position.

        Args:
            position: Dict with all position fields
        """
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO position_cache (
                    position_key, address, token, exchange, side,
                    size, leverage, leverage_type, entry_price, position_value,
                    liq_price, margin_used, unrealized_pnl,
                    mark_price, distance_pct, cohort,
                    refresh_tier, last_full_refresh, last_price_update, created_at,
                    hunting_score, is_in_watchlist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position['position_key'],
                position['address'],
                position['token'],
                position['exchange'],
                position['side'],
                position['size'],
                position['leverage'],
                position['leverage_type'],
                position['entry_price'],
                position['position_value'],
                position.get('liq_price'),
                position['margin_used'],
                position['unrealized_pnl'],
                position['mark_price'],
                position.get('distance_pct'),
                position['cohort'],
                position.get('refresh_tier', 'normal'),
                position['last_full_refresh'],
                position['last_price_update'],
                position['created_at'],
                position.get('hunting_score'),
                int(position.get('is_in_watchlist', False)),
            ))

    def save_cached_positions_batch(self, positions: List[dict]):
        """
        Save multiple cached positions efficiently.

        Args:
            positions: List of position dicts
        """
        if not positions:
            return

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO position_cache (
                    position_key, address, token, exchange, side,
                    size, leverage, leverage_type, entry_price, position_value,
                    liq_price, margin_used, unrealized_pnl,
                    mark_price, distance_pct, cohort,
                    refresh_tier, last_full_refresh, last_price_update, created_at,
                    hunting_score, is_in_watchlist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    p['position_key'],
                    p['address'],
                    p['token'],
                    p['exchange'],
                    p['side'],
                    p['size'],
                    p['leverage'],
                    p['leverage_type'],
                    p['entry_price'],
                    p['position_value'],
                    p.get('liq_price'),
                    p['margin_used'],
                    p['unrealized_pnl'],
                    p['mark_price'],
                    p.get('distance_pct'),
                    p['cohort'],
                    p.get('refresh_tier', 'normal'),
                    p['last_full_refresh'],
                    p['last_price_update'],
                    p['created_at'],
                    p.get('hunting_score'),
                    int(p.get('is_in_watchlist', False)),
                )
                for p in positions
            ])

        logger.debug(f"Saved {len(positions)} cached positions")

    def load_position_cache(self) -> List[dict]:
        """
        Load all cached positions from database.

        Returns:
            List of position dicts
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM position_cache")
            rows = cursor.fetchall()

        positions = []
        for row in rows:
            positions.append({
                'position_key': row['position_key'],
                'address': row['address'],
                'token': row['token'],
                'exchange': row['exchange'],
                'side': row['side'],
                'size': row['size'],
                'leverage': row['leverage'],
                'leverage_type': row['leverage_type'],
                'entry_price': row['entry_price'],
                'position_value': row['position_value'],
                'liq_price': row['liq_price'],
                'margin_used': row['margin_used'],
                'unrealized_pnl': row['unrealized_pnl'],
                'mark_price': row['mark_price'],
                'distance_pct': row['distance_pct'],
                'cohort': row['cohort'],
                'refresh_tier': row['refresh_tier'],
                'last_full_refresh': row['last_full_refresh'],
                'last_price_update': row['last_price_update'],
                'created_at': row['created_at'],
                'hunting_score': row['hunting_score'],
                'is_in_watchlist': bool(row['is_in_watchlist']),
            })

        logger.info(f"Loaded {len(positions)} cached positions")
        return positions

    def get_positions_by_tier(self, tier: str) -> List[dict]:
        """
        Get cached positions by refresh tier.

        Args:
            tier: 'critical', 'high', or 'normal'

        Returns:
            List of position dicts
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM position_cache WHERE refresh_tier = ?",
                (tier,)
            )
            rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def update_position_price(
        self,
        position_key: str,
        mark_price: float,
        distance_pct: Optional[float],
        refresh_tier: str,
        last_price_update: str
    ):
        """
        Update mark price and distance for a cached position.

        Args:
            position_key: Position identifier
            mark_price: New mark price
            distance_pct: New distance to liquidation
            refresh_tier: New tier classification
            last_price_update: ISO timestamp
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE position_cache
                SET mark_price = ?, distance_pct = ?, refresh_tier = ?, last_price_update = ?
                WHERE position_key = ?
            """, (mark_price, distance_pct, refresh_tier, last_price_update, position_key))

    def update_position_full_refresh(
        self,
        position_key: str,
        position_data: dict,
        last_full_refresh: str
    ):
        """
        Update a cached position with fresh data from API.

        Args:
            position_key: Position identifier
            position_data: Dict with position fields to update
            last_full_refresh: ISO timestamp
        """
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE position_cache
                SET size = ?, leverage = ?, leverage_type = ?, entry_price = ?,
                    position_value = ?, liq_price = ?, margin_used = ?, unrealized_pnl = ?,
                    mark_price = ?, distance_pct = ?, refresh_tier = ?,
                    last_full_refresh = ?, last_price_update = ?
                WHERE position_key = ?
            """, (
                position_data['size'],
                position_data['leverage'],
                position_data['leverage_type'],
                position_data['entry_price'],
                position_data['position_value'],
                position_data.get('liq_price'),
                position_data['margin_used'],
                position_data['unrealized_pnl'],
                position_data['mark_price'],
                position_data.get('distance_pct'),
                position_data.get('refresh_tier', 'normal'),
                last_full_refresh,
                last_full_refresh,
                position_key,
            ))

    def delete_cached_positions(self, position_keys: List[str]):
        """
        Delete cached positions by key.

        Args:
            position_keys: List of position keys to delete
        """
        if not position_keys:
            return

        with self._get_connection() as conn:
            placeholders = ','.join('?' * len(position_keys))
            conn.execute(
                f"DELETE FROM position_cache WHERE position_key IN ({placeholders})",
                position_keys
            )

        logger.debug(f"Deleted {len(position_keys)} cached positions")

    def delete_stale_positions(self, max_age_hours: int = 24) -> int:
        """
        Delete cached positions that haven't been refreshed recently.

        Args:
            max_age_hours: Maximum age in hours before deletion

        Returns:
            Number of positions deleted
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM position_cache WHERE last_full_refresh < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"Deleted {deleted} stale cached positions (older than {max_age_hours}h)")

        return deleted

    def clear_position_cache(self):
        """Clear all cached positions."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM position_cache")
        logger.debug("Position cache cleared")

    def get_position_cache_stats(self) -> dict:
        """Get statistics about the position cache."""
        with self._get_connection() as conn:
            stats = {}

            # Count by tier
            cursor = conn.execute("""
                SELECT refresh_tier, COUNT(*) as count
                FROM position_cache
                GROUP BY refresh_tier
            """)
            stats['by_tier'] = {row['refresh_tier']: row['count'] for row in cursor.fetchall()}

            # Total count
            cursor = conn.execute("SELECT COUNT(*) as count FROM position_cache")
            stats['total'] = cursor.fetchone()['count']

            # Count with liquidation price
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM position_cache WHERE liq_price IS NOT NULL"
            )
            stats['with_liq_price'] = cursor.fetchone()['count']

        return stats

    # =========================================================================
    # Known Addresses Operations
    # =========================================================================

    def save_known_address(self, address: str, cohort: str):
        """
        Save or update a known address.

        Args:
            address: Wallet address
            cohort: Cohort name
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            # Check if exists
            cursor = conn.execute(
                "SELECT first_seen FROM known_addresses WHERE address = ?",
                (address,)
            )
            row = cursor.fetchone()

            if row:
                # Update existing
                conn.execute("""
                    UPDATE known_addresses
                    SET cohort = ?, last_scanned = ?
                    WHERE address = ?
                """, (cohort, now, address))
            else:
                # Insert new
                conn.execute("""
                    INSERT INTO known_addresses (address, cohort, first_seen, last_scanned)
                    VALUES (?, ?, ?, ?)
                """, (address, cohort, now, now))

    def save_known_addresses_batch(self, addresses: List[tuple]):
        """
        Save multiple known addresses efficiently.

        Args:
            addresses: List of (address, cohort) tuples
        """
        if not addresses:
            return

        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            # Use INSERT OR REPLACE for simplicity
            conn.executemany("""
                INSERT OR REPLACE INTO known_addresses (address, cohort, first_seen, last_scanned)
                VALUES (?, ?, COALESCE(
                    (SELECT first_seen FROM known_addresses WHERE address = ?),
                    ?
                ), ?)
            """, [
                (addr, cohort, addr, now, now)
                for addr, cohort in addresses
            ])

        logger.debug(f"Saved {len(addresses)} known addresses")

    def load_known_addresses(self) -> Set[str]:
        """
        Load all known addresses.

        Returns:
            Set of wallet addresses
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT address FROM known_addresses")
            addresses = {row['address'] for row in cursor.fetchall()}

        logger.info(f"Loaded {len(addresses)} known addresses")
        return addresses

    def get_known_addresses_with_cohort(self) -> List[tuple]:
        """
        Load all known addresses with their cohorts.

        Returns:
            List of (address, cohort) tuples
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT address, cohort FROM known_addresses")
            return [(row['address'], row['cohort']) for row in cursor.fetchall()]

    def get_addresses_by_cohort(self, cohort: str) -> List[str]:
        """
        Get addresses for a specific cohort.

        Args:
            cohort: Cohort name

        Returns:
            List of addresses
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT address FROM known_addresses WHERE cohort = ?",
                (cohort,)
            )
            return [row['address'] for row in cursor.fetchall()]

    def clear_known_addresses(self):
        """Clear all known addresses."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM known_addresses")
        logger.debug("Known addresses cleared")

    # =========================================================================
    # Position History Operations
    # =========================================================================

    def record_position_snapshot(
        self,
        position_key: str,
        liq_price: float,
        position_value: float,
        distance_pct: float,
        mark_price: float
    ):
        """
        Record a position snapshot to history.

        Args:
            position_key: Unique position identifier
            liq_price: Current liquidation price
            position_value: Current position value
            distance_pct: Current distance to liquidation
            mark_price: Current mark price
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO position_history
                (position_key, liq_price, position_value, distance_pct, mark_price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (position_key, liq_price, position_value, distance_pct, mark_price, now))

    def record_position_snapshots_batch(self, snapshots: List[dict]):
        """
        Record multiple position snapshots efficiently.

        Args:
            snapshots: List of dicts with position_key, liq_price, position_value,
                      distance_pct, mark_price
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO position_history
                (position_key, liq_price, position_value, distance_pct, mark_price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (s['position_key'], s['liq_price'], s['position_value'],
                 s['distance_pct'], s['mark_price'], now)
                for s in snapshots
            ])

        logger.debug(f"Recorded {len(snapshots)} position snapshots")

    def get_position_history(
        self,
        position_key: str,
        hours: int = 24
    ) -> List[dict]:
        """
        Get position history for a specific position.

        Args:
            position_key: Position identifier
            hours: How many hours of history to fetch

        Returns:
            List of history records
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM position_history
                WHERE position_key = ? AND timestamp > ?
                ORDER BY timestamp ASC
            """, (position_key, cutoff))

            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Alert Log Operations
    # =========================================================================

    def log_alert(
        self,
        position_key: str,
        alert_type: str,
        message_id: Optional[int] = None,
        details: Optional[str] = None
    ):
        """
        Log an alert that was sent.

        Args:
            position_key: Position identifier
            alert_type: Type of alert (new_position, proximity, critical, recovery)
            message_id: Telegram message ID if available
            details: Additional details
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO alert_log (position_key, alert_type, message_id, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (position_key, alert_type, message_id, details, now))

    def get_recent_alerts(self, hours: int = 24) -> List[dict]:
        """
        Get recent alerts.

        Args:
            hours: How many hours of alerts to fetch

        Returns:
            List of alert records
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM alert_log
                WHERE timestamp > ?
                ORDER BY timestamp DESC
            """, (cutoff,))

            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Service State Operations
    # =========================================================================

    def set_state(self, key: str, value: str):
        """Set a service state value."""
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO service_state (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (key, value, now))

    def get_state(self, key: str, default: str = None) -> Optional[str]:
        """Get a service state value."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT value FROM service_state WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row['value'] if row else default

    # =========================================================================
    # Service Log Operations
    # =========================================================================

    def write_log(
        self,
        timestamp: str,
        level: str,
        logger_name: str,
        message: str,
        exc_info: Optional[str] = None
    ):
        """
        Write a log entry to the database.

        Args:
            timestamp: ISO format timestamp
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            logger_name: Name of the logger
            message: Log message
            exc_info: Exception info if any
        """
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO service_logs (timestamp, level, logger_name, message, exc_info)
                VALUES (?, ?, ?, ?, ?)
            """, (timestamp, level, logger_name, message, exc_info))

    def write_logs_batch(self, logs: List[dict]):
        """
        Write multiple log entries efficiently.

        Args:
            logs: List of dicts with timestamp, level, logger_name, message, exc_info
        """
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO service_logs (timestamp, level, logger_name, message, exc_info)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (log['timestamp'], log['level'], log['logger_name'],
                 log['message'], log.get('exc_info'))
                for log in logs
            ])

    def get_logs(
        self,
        hours: int = 24,
        level: Optional[str] = None,
        limit: int = 1000
    ) -> List[dict]:
        """
        Get recent logs from the database.

        Args:
            hours: How many hours of logs to fetch
            level: Filter by log level (optional)
            limit: Maximum number of logs to return

        Returns:
            List of log records (newest first)
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        with self._get_connection() as conn:
            if level:
                cursor = conn.execute("""
                    SELECT * FROM service_logs
                    WHERE timestamp > ? AND level = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (cutoff, level, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM service_logs
                    WHERE timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (cutoff, limit))

            return [dict(row) for row in cursor.fetchall()]

    def prune_logs(self, days: int = SERVICE_LOG_RETENTION_DAYS) -> int:
        """
        Remove old log entries.

        Args:
            days: Days of logs to keep

        Returns:
            Number of rows deleted
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM service_logs WHERE timestamp < ?", (cutoff,)
            )
            return cursor.rowcount

    # =========================================================================
    # Maintenance Operations
    # =========================================================================

    def prune_old_data(
        self,
        history_days: int = POSITION_HISTORY_RETENTION_DAYS,
        alert_days: int = ALERT_LOG_RETENTION_DAYS,
        log_days: int = SERVICE_LOG_RETENTION_DAYS
    ) -> dict:
        """
        Remove old data to keep database size bounded.

        Args:
            history_days: Days of position history to keep
            alert_days: Days of alert logs to keep
            log_days: Days of service logs to keep

        Returns:
            Dict with counts of deleted rows
        """
        history_cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).isoformat()
        alert_cutoff = (datetime.now(timezone.utc) - timedelta(days=alert_days)).isoformat()
        log_cutoff = (datetime.now(timezone.utc) - timedelta(days=log_days)).isoformat()

        deleted = {'position_history': 0, 'alert_log': 0, 'service_logs': 0}

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM position_history WHERE timestamp < ?", (history_cutoff,)
            )
            deleted['position_history'] = cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM alert_log WHERE timestamp < ?", (alert_cutoff,)
            )
            deleted['alert_log'] = cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM service_logs WHERE timestamp < ?", (log_cutoff,)
            )
            deleted['service_logs'] = cursor.rowcount

        if any(v > 0 for v in deleted.values()):
            logger.info(
                f"Pruned old data: {deleted['position_history']} history, "
                f"{deleted['alert_log']} alerts, {deleted['service_logs']} logs"
            )

        return deleted

    def vacuum(self):
        """Reclaim disk space after deletions."""
        with self._get_connection() as conn:
            conn.execute("VACUUM")
        logger.info("Database vacuumed")

    def get_stats(self) -> dict:
        """Get database statistics."""
        with self._get_connection() as conn:
            stats = {}

            for table in ['watchlist', 'baseline_positions', 'position_history', 'alert_log', 'service_logs']:
                cursor = conn.execute(f"SELECT COUNT(*) as count FROM {table}")
                stats[table] = cursor.fetchone()['count']

            # Get database file size
            stats['file_size_mb'] = self.db_path.stat().st_size / (1024 * 1024)

        return stats

    def get_last_scan_time(self) -> Optional[datetime]:
        """
        Get the timestamp of the last completed scan.

        Returns:
            datetime of last scan, or None if no scan recorded
        """
        value = self.get_state('last_scan_time')
        if value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    def set_last_scan_time(self, scan_time: Optional[datetime] = None):
        """
        Record the timestamp of a completed scan.

        Args:
            scan_time: Time of scan (defaults to now)
        """
        if scan_time is None:
            scan_time = datetime.now(timezone.utc)
        self.set_state('last_scan_time', scan_time.isoformat())


# =============================================================================
# SQLite Logging Handler
# =============================================================================


class SQLiteLoggingHandler(logging.Handler):
    """
    A logging handler that writes log records to SQLite database.

    Uses a background thread to batch writes and avoid blocking the main thread.
    Logs are persisted even if the container restarts (as long as the database
    file is on a volume or in a persistent location).
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        batch_size: int = 50,
        flush_interval: float = 5.0,
        level: int = logging.INFO
    ):
        """
        Initialize the SQLite logging handler.

        Args:
            db_path: Path to SQLite database file
            batch_size: Number of logs to batch before writing
            flush_interval: Max seconds between flushes
            level: Minimum log level to capture
        """
        super().__init__(level)
        self.db_path = db_path
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Queue for log records
        self._queue: Queue = Queue()
        self._shutdown = threading.Event()

        # Start background writer thread
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="SQLiteLogWriter"
        )
        self._writer_thread.start()

    def emit(self, record: logging.LogRecord):
        """
        Emit a log record by adding it to the queue.

        Args:
            record: Log record to emit
        """
        try:
            # Format exception info if present
            exc_info = None
            if record.exc_info:
                exc_info = ''.join(traceback.format_exception(*record.exc_info))

            log_entry = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'level': record.levelname,
                'logger_name': record.name,
                'message': self.format(record),
                'exc_info': exc_info,
            }
            self._queue.put(log_entry)
        except Exception:
            # Don't let logging errors crash the app
            self.handleError(record)

    def _writer_loop(self):
        """Background loop that batches and writes logs to SQLite."""
        batch = []

        while not self._shutdown.is_set():
            try:
                # Collect logs until batch is full or timeout
                while len(batch) < self.batch_size:
                    try:
                        log_entry = self._queue.get(timeout=self.flush_interval)
                        batch.append(log_entry)
                    except Empty:
                        # Timeout - flush what we have
                        break

                # Write batch to database
                if batch:
                    self._write_batch(batch)
                    batch = []

            except Exception:
                # Log writer errors to stderr, don't crash
                import sys
                traceback.print_exc(file=sys.stderr)
                batch = []  # Clear batch to avoid infinite loop

        # Final flush on shutdown
        if batch:
            self._write_batch(batch)

        # Drain remaining queue
        while not self._queue.empty():
            try:
                log_entry = self._queue.get_nowait()
                batch.append(log_entry)
            except Empty:
                break

        if batch:
            self._write_batch(batch)

    def _write_batch(self, batch: List[dict]):
        """Write a batch of logs to the database."""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            try:
                conn.executemany("""
                    INSERT INTO service_logs (timestamp, level, logger_name, message, exc_info)
                    VALUES (?, ?, ?, ?, ?)
                """, [
                    (log['timestamp'], log['level'], log['logger_name'],
                     log['message'], log.get('exc_info'))
                    for log in batch
                ])
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # Write errors to stderr
            import sys
            traceback.print_exc(file=sys.stderr)

    def flush(self):
        """Flush any pending logs."""
        # Signal the writer thread to flush by waiting for queue to empty
        while not self._queue.empty():
            import time
            time.sleep(0.1)

    def close(self):
        """Close the handler and flush remaining logs."""
        self._shutdown.set()
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=10)
        super().close()
