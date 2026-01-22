"""
Liquidation Feed Parser and Listener
=====================================

Captures liquidation events from Telegram channels to build a historical
database of liquidated addresses. These addresses are then included in
the discovery phase for monitoring.

Source: @liquidations_hyperliquid (and similar channels)

Message format:
    🔴 #BTC Long Liquidation: $1.15M @ $88,827.1 [scan][dash]
    🟢 #[xyz]:SILVER Short Liquidation: $1.95M @ $96.07 [scan][dash]

Links contain wallet addresses:
    [scan] -> https://hypurrscan.io/address/0x...
    [dash] -> https://legacy.hyperdash.com/trader/0x...
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ParsedLiquidation:
    """Parsed liquidation event from Telegram message."""
    address: str
    token: str
    exchange: str  # "main" or "xyz", "flx", etc.
    side: str  # "Long" or "Short"
    notional: float  # USD value
    price: float  # Liquidation price
    timestamp: datetime
    raw_message: str


class LiquidationParser:
    """
    Parser for Telegram liquidation alert messages.

    Extracts:
    - Token symbol and exchange
    - Position direction (long/short)
    - Notional value
    - Liquidation price
    - Wallet address (from links)
    """

    # Regex patterns
    # Main pattern: 🔴 #BTC Long Liquidation: $1.15M @ $88,827.1
    # xyz pattern: 🟢 #[xyz]:SILVER Short Liquidation: $1.95M @ $96.07
    MESSAGE_PATTERN = re.compile(
        r'([🔴🟢])\s*'  # Direction emoji
        r'#(\[?[\w]+\]?:?[\w]+)\s+'  # Token (with optional [xyz]: prefix)
        r'(Long|Short)\s+Liquidation:\s*'  # Direction text
        r'\$([0-9,.]+)([KMB]?)\s*'  # Notional value
        r'@\s*\$?([0-9,.]+)',  # Price
        re.IGNORECASE
    )

    # Address pattern in URLs
    ADDRESS_PATTERN = re.compile(r'0x[a-fA-F0-9]{40}')

    # Notional multipliers
    MULTIPLIERS = {
        '': 1,
        'K': 1_000,
        'M': 1_000_000,
        'B': 1_000_000_000,
    }

    @classmethod
    def parse_message(cls, message: str, timestamp: Optional[datetime] = None) -> Optional[ParsedLiquidation]:
        """
        Parse a Telegram liquidation message.

        Args:
            message: Raw message text (may include HTML links)
            timestamp: Message timestamp (defaults to now)

        Returns:
            ParsedLiquidation if successfully parsed, None otherwise
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        # Extract address from links
        address_match = cls.ADDRESS_PATTERN.search(message)
        if not address_match:
            logger.debug(f"No address found in message: {message[:100]}")
            return None
        address = address_match.group(0).lower()

        # Parse main content
        match = cls.MESSAGE_PATTERN.search(message)
        if not match:
            logger.debug(f"Message doesn't match pattern: {message[:100]}")
            return None

        emoji, token_raw, side, notional_str, multiplier, price_str = match.groups()

        # Parse token and exchange
        token, exchange = cls._parse_token(token_raw)

        # Parse notional value
        notional = cls._parse_notional(notional_str, multiplier.upper() if multiplier else '')

        # Parse price
        price = float(price_str.replace(',', ''))

        # Normalize side
        side = side.capitalize()

        return ParsedLiquidation(
            address=address,
            token=token,
            exchange=exchange,
            side=side,
            notional=notional,
            price=price,
            timestamp=timestamp,
            raw_message=message[:500],  # Truncate for storage
        )

    @classmethod
    def _parse_token(cls, token_raw: str) -> Tuple[str, str]:
        """
        Parse token symbol and exchange from raw token string.

        Examples:
            "BTC" -> ("BTC", "main")
            "[xyz]:SILVER" -> ("xyz:SILVER", "xyz")
            "#[xyz]:TSLA" -> ("xyz:TSLA", "xyz")
        """
        # Remove leading # if present
        token_raw = token_raw.lstrip('#')

        # Check for exchange prefix: [xyz]:TOKEN or xyz:TOKEN
        if token_raw.startswith('['):
            # Format: [xyz]:TOKEN
            match = re.match(r'\[(\w+)\]:(\w+)', token_raw)
            if match:
                exchange, token = match.groups()
                return f"{exchange}:{token}", exchange.lower()
        elif ':' in token_raw:
            # Format: xyz:TOKEN
            parts = token_raw.split(':', 1)
            if len(parts) == 2:
                exchange, token = parts
                return f"{exchange}:{token}", exchange.lower()

        # No prefix = main exchange
        return token_raw.upper(), "main"

    @classmethod
    def _parse_notional(cls, value_str: str, multiplier: str) -> float:
        """Parse notional value with K/M/B suffix."""
        value = float(value_str.replace(',', ''))
        return value * cls.MULTIPLIERS.get(multiplier, 1)


class LiquidationHistoryDB:
    """
    SQLite storage for historical liquidation data.

    Stores liquidation events to build a database of addresses
    that have been liquidated in the past.
    """

    def __init__(self, db_path: Path):
        """
        Initialize the liquidation history database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self):
        """Get a database connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """Initialize the liquidation history schema."""
        conn = self._get_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL")

            # Main table: stores each liquidation event
            conn.execute("""
                CREATE TABLE IF NOT EXISTS liquidation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    token TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,
                    notional REAL NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    raw_message TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Index for address lookups
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_liq_history_address
                ON liquidation_history(address)
            """)

            # Index for filtering by notional
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_liq_history_notional
                ON liquidation_history(notional)
            """)

            # Index for recent liquidations
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_liq_history_timestamp
                ON liquidation_history(timestamp)
            """)

            # Aggregated view: addresses with their max liquidation
            # This is what we use for discovery - addresses that have been
            # liquidated with at least X notional
            conn.execute("""
                CREATE TABLE IF NOT EXISTS liquidated_addresses (
                    address TEXT PRIMARY KEY,
                    max_notional REAL NOT NULL,
                    total_liquidations INTEGER NOT NULL DEFAULT 1,
                    last_liquidation TEXT NOT NULL,
                    first_liquidation TEXT NOT NULL,
                    last_scanned TEXT,
                    tokens_liquidated TEXT  -- JSON array of tokens
                )
            """)

            # Index for filtering by max notional (for threshold checks)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_liq_addr_notional
                ON liquidated_addresses(max_notional)
            """)

            conn.commit()
            logger.info(f"Liquidation history DB initialized: {self.db_path}")
        finally:
            conn.close()

    def record_liquidation(self, liq: ParsedLiquidation) -> bool:
        """
        Record a liquidation event.

        Args:
            liq: Parsed liquidation data

        Returns:
            True if this is a new liquidation, False if duplicate
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()

        try:
            # Check for duplicate (same address, token, timestamp within 1 minute)
            cursor = conn.execute("""
                SELECT id FROM liquidation_history
                WHERE address = ? AND token = ?
                AND ABS(julianday(timestamp) - julianday(?)) < 0.0007  -- ~1 minute
            """, (liq.address, liq.token, liq.timestamp.isoformat()))

            if cursor.fetchone():
                logger.debug(f"Duplicate liquidation ignored: {liq.address[:10]}... {liq.token}")
                return False

            # Insert liquidation event
            conn.execute("""
                INSERT INTO liquidation_history
                (address, token, exchange, side, notional, price, timestamp, raw_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                liq.address,
                liq.token,
                liq.exchange,
                liq.side,
                liq.notional,
                liq.price,
                liq.timestamp.isoformat(),
                liq.raw_message,
                now,
            ))

            # Update aggregated address table
            cursor = conn.execute(
                "SELECT * FROM liquidated_addresses WHERE address = ?",
                (liq.address,)
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing record
                max_notional = max(existing['max_notional'], liq.notional)
                total = existing['total_liquidations'] + 1

                # Update tokens list
                tokens = existing['tokens_liquidated'] or '[]'
                import json
                token_list = json.loads(tokens)
                if liq.token not in token_list:
                    token_list.append(liq.token)

                conn.execute("""
                    UPDATE liquidated_addresses
                    SET max_notional = ?,
                        total_liquidations = ?,
                        last_liquidation = ?,
                        tokens_liquidated = ?
                    WHERE address = ?
                """, (
                    max_notional,
                    total,
                    liq.timestamp.isoformat(),
                    json.dumps(token_list),
                    liq.address,
                ))
            else:
                # Insert new record
                import json
                conn.execute("""
                    INSERT INTO liquidated_addresses
                    (address, max_notional, total_liquidations, last_liquidation, first_liquidation, tokens_liquidated)
                    VALUES (?, ?, 1, ?, ?, ?)
                """, (
                    liq.address,
                    liq.notional,
                    liq.timestamp.isoformat(),
                    liq.timestamp.isoformat(),
                    json.dumps([liq.token]),
                ))

            conn.commit()
            logger.info(
                f"Recorded liquidation: {liq.address[:10]}... {liq.token} "
                f"${liq.notional:,.0f} @ ${liq.price:,.2f}"
            )
            return True

        finally:
            conn.close()

    def record_liquidations_batch(self, liquidations: List[ParsedLiquidation]) -> int:
        """
        Record multiple liquidations efficiently.

        Args:
            liquidations: List of parsed liquidations

        Returns:
            Number of new liquidations recorded
        """
        recorded = 0
        for liq in liquidations:
            if self.record_liquidation(liq):
                recorded += 1
        return recorded

    def get_addresses_above_threshold(self, min_notional: float) -> List[str]:
        """
        Get addresses that have been liquidated with at least min_notional.

        Args:
            min_notional: Minimum liquidation size in USD

        Returns:
            List of wallet addresses
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT address FROM liquidated_addresses
                WHERE max_notional >= ?
            """, (min_notional,))
            return [row['address'] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_addresses_for_discovery(
        self,
        min_notional: float = 100_000,
        max_scan_age_hours: int = 24
    ) -> List[Tuple[str, float]]:
        """
        Get addresses to include in discovery scan.

        Returns addresses that:
        - Have been liquidated with at least min_notional
        - Haven't been scanned recently (or never scanned)

        Args:
            min_notional: Minimum max liquidation to include
            max_scan_age_hours: Re-scan if last scan older than this

        Returns:
            List of (address, max_notional) tuples, sorted by max_notional desc
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_scan_age_hours)).isoformat()

        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT address, max_notional
                FROM liquidated_addresses
                WHERE max_notional >= ?
                AND (last_scanned IS NULL OR last_scanned < ?)
                ORDER BY max_notional DESC
            """, (min_notional, cutoff))
            return [(row['address'], row['max_notional']) for row in cursor.fetchall()]
        finally:
            conn.close()

    def mark_addresses_scanned(self, addresses: List[str]):
        """
        Mark addresses as scanned (update last_scanned timestamp).

        Args:
            addresses: List of addresses that were scanned
        """
        if not addresses:
            return

        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        try:
            placeholders = ','.join('?' * len(addresses))
            conn.execute(f"""
                UPDATE liquidated_addresses
                SET last_scanned = ?
                WHERE address IN ({placeholders})
            """, [now] + addresses)
            conn.commit()
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Get statistics about the liquidation history."""
        conn = self._get_connection()
        try:
            stats = {}

            # Total events
            cursor = conn.execute("SELECT COUNT(*) as count FROM liquidation_history")
            stats['total_events'] = cursor.fetchone()['count']

            # Unique addresses
            cursor = conn.execute("SELECT COUNT(*) as count FROM liquidated_addresses")
            stats['unique_addresses'] = cursor.fetchone()['count']

            # Addresses by notional tier
            tiers = [
                ('$100K+', 100_000),
                ('$500K+', 500_000),
                ('$1M+', 1_000_000),
                ('$5M+', 5_000_000),
                ('$10M+', 10_000_000),
            ]
            stats['by_tier'] = {}
            for name, threshold in tiers:
                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM liquidated_addresses WHERE max_notional >= ?",
                    (threshold,)
                )
                stats['by_tier'][name] = cursor.fetchone()['count']

            # Recent activity (last 24h)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM liquidation_history WHERE timestamp > ?",
                (cutoff,)
            )
            stats['last_24h'] = cursor.fetchone()['count']

            return stats
        finally:
            conn.close()

    def get_address_history(self, address: str) -> List[dict]:
        """
        Get liquidation history for a specific address.

        Args:
            address: Wallet address

        Returns:
            List of liquidation events for this address
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM liquidation_history
                WHERE address = ?
                ORDER BY timestamp DESC
            """, (address.lower(),))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_recidivists(self, min_liquidations: int = 2) -> List[dict]:
        """
        Get addresses that have been liquidated multiple times.

        Args:
            min_liquidations: Minimum number of liquidations

        Returns:
            List of address records with liquidation counts
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM liquidated_addresses
                WHERE total_liquidations >= ?
                ORDER BY total_liquidations DESC, max_notional DESC
            """, (min_liquidations,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


# =============================================================================
# Telegram Integration (placeholder for actual bot implementation)
# =============================================================================

class TelegramLiquidationListener:
    """
    Listener for Telegram liquidation channels.

    This is a placeholder - actual implementation would use:
    - python-telegram-bot or telethon library
    - Bot token or user session
    - Channel subscription/forwarding

    For now, provides methods to manually ingest messages or
    read from exported channel history.
    """

    def __init__(self, db: LiquidationHistoryDB):
        """
        Initialize the listener.

        Args:
            db: Database for storing liquidations
        """
        self.db = db
        self.parser = LiquidationParser()

    def process_message(self, message: str, timestamp: Optional[datetime] = None) -> bool:
        """
        Process a single Telegram message.

        Args:
            message: Raw message text
            timestamp: Message timestamp

        Returns:
            True if liquidation was recorded
        """
        parsed = self.parser.parse_message(message, timestamp)
        if parsed:
            return self.db.record_liquidation(parsed)
        return False

    def process_messages_batch(
        self,
        messages: List[Tuple[str, Optional[datetime]]]
    ) -> int:
        """
        Process multiple messages.

        Args:
            messages: List of (message_text, timestamp) tuples

        Returns:
            Number of liquidations recorded
        """
        recorded = 0
        for msg, ts in messages:
            if self.process_message(msg, ts):
                recorded += 1
        return recorded

    def import_from_export(self, export_path: Path) -> int:
        """
        Import liquidations from a Telegram channel export (JSON).

        Telegram Desktop allows exporting channel history as JSON.

        Args:
            export_path: Path to exported JSON file

        Returns:
            Number of liquidations imported
        """
        import json

        with open(export_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        messages = data.get('messages', [])
        recorded = 0

        for msg in messages:
            # Extract text (may be list of text entities)
            text = msg.get('text', '')
            if isinstance(text, list):
                text = ''.join(
                    item if isinstance(item, str) else item.get('text', '')
                    for item in text
                )

            # Extract timestamp
            timestamp = None
            if 'date' in msg:
                try:
                    timestamp = datetime.fromisoformat(msg['date'].replace('Z', '+00:00'))
                except ValueError:
                    pass

            if self.process_message(text, timestamp):
                recorded += 1

        logger.info(f"Imported {recorded} liquidations from {export_path}")
        return recorded
