"""
Position Database

Stores position cache and monitoring state.
"""

import sqlite3
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, TypeVar
from dataclasses import dataclass

from ..config import Position, Bucket, config

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Database connection settings
DB_TIMEOUT = 10.0  # seconds
DB_RETRIES = 3
DB_RETRY_BACKOFF = 0.5  # seconds


@dataclass
class CachedPosition:
    """A position with monitoring metadata."""
    position: Position
    bucket: Bucket
    distance_pct: Optional[float]
    last_updated: str

    # Alert state
    alerted_proximity: bool = False
    alerted_critical: bool = False
    alert_message_id: Optional[int] = None

    # Change detection
    previous_liq_price: Optional[float] = None
    previous_position_value: Optional[float] = None

    @property
    def key(self) -> str:
        return self.position.key


@dataclass
class PositionStats:
    """Statistics about the position cache."""
    total_positions: int
    critical_count: int
    high_count: int
    normal_count: int
    total_notional: float


class PositionDB:
    """
    SQLite database for position cache and monitoring state.

    Tracks:
    - Current positions being monitored
    - Bucket classification (critical/high/normal)
    - Alert state (to prevent duplicate alerts)
    - Change detection baselines
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.positions_db_path
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create database directory: {e}")
            raise
        self._init_db()

    def _execute_with_retry(
        self,
        operation: Callable[[], T],
        retries: int = DB_RETRIES,
        backoff: float = DB_RETRY_BACKOFF,
    ) -> T:
        """
        Execute a database operation with retry logic for locked database.

        Args:
            operation: Callable that performs the database operation
            retries: Number of retry attempts
            backoff: Initial backoff time in seconds (doubles each retry)

        Returns:
            Result of the operation

        Raises:
            sqlite3.Error: If all retries fail
        """
        last_error = None
        for attempt in range(retries):
            try:
                return operation()
            except sqlite3.OperationalError as e:
                last_error = e
                if "locked" in str(e).lower() and attempt < retries - 1:
                    wait_time = backoff * (2 ** attempt)
                    logger.warning(
                        f"Database locked, retrying in {wait_time:.1f}s "
                        f"({attempt + 1}/{retries})"
                    )
                    time.sleep(wait_time)
                    continue
                raise
            except sqlite3.Error as e:
                logger.error(f"Database error: {e}")
                raise
        raise last_error

    def _init_db(self):
        """Initialize database schema."""
        try:
            with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
                conn.execute("PRAGMA journal_mode=WAL")

                # Main position cache
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        position_key TEXT PRIMARY KEY,
                        address TEXT NOT NULL,
                        token TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        side TEXT NOT NULL,
                        size REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        mark_price REAL NOT NULL,
                        liquidation_price REAL,
                        position_value REAL NOT NULL,
                        unrealized_pnl REAL NOT NULL,
                        leverage REAL NOT NULL,
                        leverage_type TEXT NOT NULL,
                        margin_used REAL NOT NULL,
                        bucket TEXT NOT NULL,
                        distance_pct REAL,
                        last_updated TEXT NOT NULL,
                        alerted_proximity INTEGER DEFAULT 0,
                        alerted_critical INTEGER DEFAULT 0,
                        alert_message_id INTEGER,
                        previous_liq_price REAL,
                        previous_position_value REAL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_positions_bucket
                    ON positions(bucket)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_positions_address
                    ON positions(address)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_positions_distance
                    ON positions(distance_pct)
                """)

                # Position history (for tracking changes over time)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS position_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        position_key TEXT NOT NULL,
                        mark_price REAL NOT NULL,
                        distance_pct REAL,
                        position_value REAL NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_history_key
                    ON position_history(position_key)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_history_timestamp
                    ON position_history(timestamp)
                """)

                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize position database: {e}")
            raise

    # -------------------------------------------------------------------------
    # Position CRUD
    # -------------------------------------------------------------------------

    def upsert_position(
        self,
        position: Position,
        distance_pct: Optional[float] = None,
    ) -> CachedPosition:
        """
        Insert or update a position in the cache.

        Args:
            position: Position data from API
            distance_pct: Pre-calculated distance to liquidation (or None to calculate)

        Returns:
            CachedPosition with monitoring metadata
        """
        now = datetime.now(timezone.utc).isoformat()

        # Calculate distance if not provided
        if distance_pct is None:
            distance_pct = position.distance_to_liq()

        # Classify into bucket
        bucket = config.classify_bucket(distance_pct)

        def _do_upsert():
            with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
                conn.row_factory = sqlite3.Row

                # Check if exists (to preserve alert state)
                existing = conn.execute(
                    "SELECT * FROM positions WHERE position_key = ?",
                    (position.key,)
                ).fetchone()

                if existing:
                    # Preserve alert state, update position data
                    conn.execute("""
                        UPDATE positions
                        SET address = ?, token = ?, exchange = ?, side = ?,
                            size = ?, entry_price = ?, mark_price = ?,
                            liquidation_price = ?, position_value = ?,
                            unrealized_pnl = ?, leverage = ?, leverage_type = ?,
                            margin_used = ?, bucket = ?, distance_pct = ?,
                            last_updated = ?,
                            previous_liq_price = liquidation_price,
                            previous_position_value = position_value
                        WHERE position_key = ?
                    """, (
                        position.address, position.token, position.exchange, position.side,
                        position.size, position.entry_price, position.mark_price,
                        position.liquidation_price, position.position_value,
                        position.unrealized_pnl, position.leverage, position.leverage_type,
                        position.margin_used, bucket.value, distance_pct,
                        now, position.key
                    ))
                    conn.commit()

                    return CachedPosition(
                        position=position,
                        bucket=bucket,
                        distance_pct=distance_pct,
                        last_updated=now,
                        alerted_proximity=bool(existing["alerted_proximity"]),
                        alerted_critical=bool(existing["alerted_critical"]),
                        alert_message_id=existing["alert_message_id"],
                        previous_liq_price=existing["liquidation_price"],
                        previous_position_value=existing["position_value"],
                    )
                else:
                    # New position
                    conn.execute("""
                        INSERT INTO positions
                        (position_key, address, token, exchange, side, size,
                         entry_price, mark_price, liquidation_price, position_value,
                         unrealized_pnl, leverage, leverage_type, margin_used,
                         bucket, distance_pct, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        position.key, position.address, position.token, position.exchange,
                        position.side, position.size, position.entry_price, position.mark_price,
                        position.liquidation_price, position.position_value,
                        position.unrealized_pnl, position.leverage, position.leverage_type,
                        position.margin_used, bucket.value, distance_pct, now
                    ))
                    conn.commit()

                    return CachedPosition(
                        position=position,
                        bucket=bucket,
                        distance_pct=distance_pct,
                        last_updated=now,
                    )

        return self._execute_with_retry(_do_upsert)

    def get_position(self, key: str) -> Optional[CachedPosition]:
        """Get a single position by key."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM positions WHERE position_key = ?",
                (key,)
            ).fetchone()

            return self._row_to_cached_position(row) if row else None

    def get_positions_by_bucket(self, bucket: Bucket) -> List[CachedPosition]:
        """Get all positions in a bucket, sorted by distance."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM positions
                WHERE bucket = ?
                ORDER BY distance_pct ASC NULLS LAST
            """, (bucket.value,)).fetchall()

            return [self._row_to_cached_position(row) for row in rows]

    def get_all_positions(self) -> List[CachedPosition]:
        """Get all positions, sorted by distance."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM positions
                ORDER BY distance_pct ASC NULLS LAST
            """).fetchall()

            return [self._row_to_cached_position(row) for row in rows]

    def get_positions_for_address(self, address: str) -> List[CachedPosition]:
        """Get all positions for a specific address."""
        address = address.lower()
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM positions WHERE address = ?
            """, (address,)).fetchall()

            return [self._row_to_cached_position(row) for row in rows]

    def remove_position(self, key: str):
        """Remove a position from the cache (e.g., closed or liquidated)."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.execute("DELETE FROM positions WHERE position_key = ?", (key,))
            conn.commit()

    def remove_stale_positions(self, max_age_hours: int = 24):
        """Remove positions that haven't been updated recently."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            result = conn.execute(
                "DELETE FROM positions WHERE last_updated < ?",
                (cutoff,)
            )
            deleted = result.rowcount
            conn.commit()

        if deleted > 0:
            logger.info(f"Removed {deleted} stale positions (older than {max_age_hours}h)")

        return deleted

    # -------------------------------------------------------------------------
    # Alert State Management
    # -------------------------------------------------------------------------

    def set_alerted_proximity(self, key: str, message_id: Optional[int] = None):
        """Mark position as having received proximity alert."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.execute("""
                UPDATE positions
                SET alerted_proximity = 1, alert_message_id = ?
                WHERE position_key = ?
            """, (message_id, key))
            conn.commit()

    def set_alerted_critical(self, key: str, message_id: Optional[int] = None):
        """Mark position as having received critical alert."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.execute("""
                UPDATE positions
                SET alerted_critical = 1, alert_message_id = ?
                WHERE position_key = ?
            """, (message_id, key))
            conn.commit()

    def reset_alerts(self, key: str):
        """Reset alert state for a position (e.g., after recovery)."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.execute("""
                UPDATE positions
                SET alerted_proximity = 0, alerted_critical = 0,
                    alert_message_id = NULL
                WHERE position_key = ?
            """, (key,))
            conn.commit()

    # -------------------------------------------------------------------------
    # History
    # -------------------------------------------------------------------------

    def record_snapshot(self, position: CachedPosition):
        """Record a position snapshot for historical tracking."""
        now = datetime.now(timezone.utc).isoformat()

        try:
            with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
                conn.execute("""
                    INSERT INTO position_history
                    (position_key, mark_price, distance_pct, position_value, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    position.key,
                    position.position.mark_price,
                    position.distance_pct,
                    position.position.position_value,
                    now
                ))
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Failed to record position snapshot: {e}")

    def prune_history(self, days: int = 7):
        """Remove history older than specified days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            result = conn.execute(
                "DELETE FROM position_history WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = result.rowcount
            conn.commit()

        if deleted > 0:
            logger.info(f"Pruned {deleted} history records older than {days} days")

        return deleted

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def get_stats(self) -> PositionStats:
        """Get statistics about the position cache."""
        try:
            with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
                total = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

                critical = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE bucket = 'critical'"
                ).fetchone()[0]

                high = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE bucket = 'high'"
                ).fetchone()[0]

                normal = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE bucket = 'normal'"
                ).fetchone()[0]

                total_notional = conn.execute(
                    "SELECT COALESCE(SUM(position_value), 0) FROM positions"
                ).fetchone()[0]

                return PositionStats(
                    total_positions=total,
                    critical_count=critical,
                    high_count=high,
                    normal_count=normal,
                    total_notional=total_notional,
                )
        except sqlite3.Error as e:
            logger.error(f"Failed to get position stats: {e}")
            return PositionStats(
                total_positions=0,
                critical_count=0,
                high_count=0,
                normal_count=0,
                total_notional=0,
            )

    def clear(self):
        """Clear all positions (use with caution)."""
        with sqlite3.connect(self.db_path, timeout=DB_TIMEOUT) as conn:
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM position_history")
            conn.commit()
        logger.warning("Cleared all positions from cache")

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    def _row_to_cached_position(self, row: sqlite3.Row) -> CachedPosition:
        """Convert a database row to a CachedPosition object."""
        position = Position(
            address=row["address"],
            token=row["token"],
            exchange=row["exchange"],
            side=row["side"],
            size=row["size"],
            entry_price=row["entry_price"],
            mark_price=row["mark_price"],
            liquidation_price=row["liquidation_price"],
            position_value=row["position_value"],
            unrealized_pnl=row["unrealized_pnl"],
            leverage=row["leverage"],
            leverage_type=row["leverage_type"],
            margin_used=row["margin_used"],
        )

        return CachedPosition(
            position=position,
            bucket=Bucket(row["bucket"]),
            distance_pct=row["distance_pct"],
            last_updated=row["last_updated"],
            alerted_proximity=bool(row["alerted_proximity"]),
            alerted_critical=bool(row["alerted_critical"]),
            alert_message_id=row["alert_message_id"],
            previous_liq_price=row["previous_liq_price"],
            previous_position_value=row["previous_position_value"],
        )


# =============================================================================
# Testing
# =============================================================================

def test_position_db():
    """Quick test of the position database."""
    import tempfile

    # Use temp file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        db = PositionDB(db_path)

        # Create a test position
        position = Position(
            address="0x123abc",
            token="BTC",
            exchange="",
            side="long",
            size=1.5,
            entry_price=90000,
            mark_price=91000,
            liquidation_price=85000,
            position_value=136500,
            unrealized_pnl=1500,
            leverage=10.0,
            leverage_type="cross",
            margin_used=13650,
        )

        # Insert position
        cached = db.upsert_position(position)
        print(f"Inserted position: {cached.key}")
        print(f"  Bucket: {cached.bucket.value}")
        print(f"  Distance: {cached.distance_pct:.2f}%")

        # Get stats
        stats = db.get_stats()
        print(f"\nStats: {stats}")

        # Update position (simulating price move)
        position.mark_price = 86000
        cached = db.upsert_position(position)
        print(f"\nUpdated position distance: {cached.distance_pct:.2f}%")
        print(f"  New bucket: {cached.bucket.value}")
        print(f"  Previous liq price preserved: {cached.previous_liq_price}")

        # Mark as alerted
        db.set_alerted_proximity(cached.key, message_id=12345)
        cached = db.get_position(cached.key)
        print(f"\nAfter alert: alerted_proximity={cached.alerted_proximity}")

        # Get by bucket
        critical = db.get_positions_by_bucket(Bucket.CRITICAL)
        print(f"\nCritical positions: {len(critical)}")

    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_position_db()
