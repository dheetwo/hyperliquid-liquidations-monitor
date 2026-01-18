"""
SQLite Persistence for Monitor Service
======================================

Lightweight persistence layer for:
- Watchlist state (survives restarts)
- Baseline position keys
- Position history (with retention)
- Alert log

Storage: data/monitor.db (persisted via Docker volume)
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .service import WatchedPosition

logger = logging.getLogger(__name__)

# Default retention periods
POSITION_HISTORY_RETENTION_DAYS = 7
ALERT_LOG_RETENTION_DAYS = 30

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
    # Maintenance Operations
    # =========================================================================

    def prune_old_data(
        self,
        history_days: int = POSITION_HISTORY_RETENTION_DAYS,
        alert_days: int = ALERT_LOG_RETENTION_DAYS
    ) -> dict:
        """
        Remove old data to keep database size bounded.

        Args:
            history_days: Days of position history to keep
            alert_days: Days of alert logs to keep

        Returns:
            Dict with counts of deleted rows
        """
        history_cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).isoformat()
        alert_cutoff = (datetime.now(timezone.utc) - timedelta(days=alert_days)).isoformat()

        deleted = {'position_history': 0, 'alert_log': 0}

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM position_history WHERE timestamp < ?", (history_cutoff,)
            )
            deleted['position_history'] = cursor.rowcount

            cursor = conn.execute(
                "DELETE FROM alert_log WHERE timestamp < ?", (alert_cutoff,)
            )
            deleted['alert_log'] = cursor.rowcount

        if deleted['position_history'] > 0 or deleted['alert_log'] > 0:
            logger.info(
                f"Pruned old data: {deleted['position_history']} history rows, "
                f"{deleted['alert_log']} alert rows"
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

            for table in ['watchlist', 'baseline_positions', 'position_history', 'alert_log']:
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
