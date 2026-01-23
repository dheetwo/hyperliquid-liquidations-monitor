"""
Wallet Database (Column A - Non-Decreasing)

Stores all known wallet addresses. Once added, wallets are never removed.
This is the source of truth for which wallets to scan.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass

from ..config import Wallet, config

logger = logging.getLogger(__name__)


@dataclass
class WalletStats:
    """Statistics about the wallet registry."""
    total_wallets: int
    from_hyperdash: int
    from_liq_history: int
    normal_frequency: int
    infrequent: int
    never_scanned: int
    total_position_value: float


class WalletDB:
    """
    SQLite database for wallet registry.

    The wallet registry is non-decreasing: wallets are only added, never removed.
    This ensures we don't lose track of interesting addresses over time.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.wallets_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    address TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    cohort TEXT,
                    position_value REAL,
                    total_collateral REAL,
                    position_count INTEGER,
                    scan_frequency TEXT DEFAULT 'normal',
                    first_seen TEXT NOT NULL,
                    last_scanned TEXT,
                    scan_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_wallets_frequency
                ON wallets(scan_frequency)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_wallets_last_scanned
                ON wallets(last_scanned)
            """)
            conn.commit()

    # -------------------------------------------------------------------------
    # Add / Update Wallets
    # -------------------------------------------------------------------------

    def add_wallet(
        self,
        address: str,
        source: str,
        cohort: Optional[str] = None,
        position_value: Optional[float] = None,
        scan_frequency: Optional[str] = None,
    ) -> bool:
        """
        Add a wallet to the registry. If it already exists, update metadata.

        Args:
            address: Wallet address (0x...)
            source: Source of this wallet ("hyperdash" or "liq_history")
            cohort: Cohort name if from Hyperdash
            position_value: Total position value if known
            scan_frequency: "normal" or "infrequent" (defaults based on source)

        Returns:
            True if wallet was added (new), False if updated (existing)
        """
        address = address.lower()
        now = datetime.now(timezone.utc).isoformat()

        # Default scan_frequency based on source if not specified
        if scan_frequency is None:
            scan_frequency = "normal" if source == "hyperdash" else "infrequent"

        with sqlite3.connect(self.db_path) as conn:
            # Check if exists
            existing = conn.execute(
                "SELECT address, scan_frequency FROM wallets WHERE address = ?",
                (address,)
            ).fetchone()

            if existing:
                # Update existing wallet (keep source, update cohort/value if provided)
                updates = []
                params = []

                if cohort is not None:
                    updates.append("cohort = ?")
                    params.append(cohort)

                if position_value is not None:
                    updates.append("position_value = ?")
                    params.append(position_value)

                # Only upgrade frequency (infrequent -> normal), never downgrade
                if scan_frequency == "normal" and existing[1] == "infrequent":
                    updates.append("scan_frequency = ?")
                    params.append("normal")

                if updates:
                    params.append(address)
                    conn.execute(
                        f"UPDATE wallets SET {', '.join(updates)} WHERE address = ?",
                        params
                    )
                    conn.commit()

                return False  # Not new

            else:
                # Insert new wallet
                conn.execute("""
                    INSERT INTO wallets
                    (address, source, cohort, position_value, first_seen, scan_frequency)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (address, source, cohort, position_value, now, scan_frequency))
                conn.commit()

                return True  # New wallet

    def add_wallets_batch(
        self,
        wallets: List[Dict],
    ) -> tuple[int, int]:
        """
        Add multiple wallets efficiently.

        Args:
            wallets: List of dicts with keys:
                - address (required)
                - source (required)
                - cohort (optional)
                - position_value (optional) - total notional from Hyperdash
                - scan_frequency (optional) - "normal" or "infrequent"

        Returns:
            Tuple of (new_count, updated_count)
        """
        now = datetime.now(timezone.utc).isoformat()
        new_count = 0
        updated_count = 0

        with sqlite3.connect(self.db_path) as conn:
            for w in wallets:
                address = w["address"].lower()
                source = w["source"]
                cohort = w.get("cohort")
                position_value = w.get("position_value")
                scan_frequency = w.get("scan_frequency", "normal")

                # Check if exists
                existing = conn.execute(
                    "SELECT address, scan_frequency FROM wallets WHERE address = ?",
                    (address,)
                ).fetchone()

                if existing:
                    # Update existing wallet
                    updates = []
                    params = []

                    if cohort:
                        updates.append("cohort = ?")
                        params.append(cohort)

                    if position_value is not None:
                        updates.append("position_value = ?")
                        params.append(position_value)

                    # Only upgrade frequency (infrequent -> normal), never downgrade
                    if scan_frequency == "normal" and existing[1] == "infrequent":
                        updates.append("scan_frequency = ?")
                        params.append("normal")

                    if updates:
                        params.append(address)
                        conn.execute(
                            f"UPDATE wallets SET {', '.join(updates)} WHERE address = ?",
                            params
                        )
                    updated_count += 1
                else:
                    conn.execute("""
                        INSERT INTO wallets
                        (address, source, cohort, position_value, first_seen, scan_frequency)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (address, source, cohort, position_value, now, scan_frequency))
                    new_count += 1

            conn.commit()

        logger.info(f"Added {new_count} new wallets, updated {updated_count}")
        return new_count, updated_count

    def update_scan_result(
        self,
        address: str,
        position_value: float,
        total_collateral: float = 0,
        position_count: int = 0,
    ):
        """
        Update wallet after scanning its positions.

        Also updates scan frequency based on position value.

        Args:
            address: Wallet address
            position_value: Total position value in USD
            total_collateral: Total collateral in USD
            position_count: Number of open positions
        """
        address = address.lower()
        now = datetime.now(timezone.utc).isoformat()

        # Determine scan frequency based on position value
        if position_value >= config.infrequent_scan_threshold:
            scan_frequency = "normal"
        else:
            scan_frequency = "infrequent"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE wallets
                SET position_value = ?,
                    total_collateral = ?,
                    position_count = ?,
                    scan_frequency = ?,
                    last_scanned = ?,
                    scan_count = scan_count + 1
                WHERE address = ?
            """, (position_value, total_collateral, position_count,
                  scan_frequency, now, address))
            conn.commit()

    # -------------------------------------------------------------------------
    # Query Wallets
    # -------------------------------------------------------------------------

    def get_wallets_for_scan(
        self,
        include_infrequent: bool = False,
    ) -> List[Wallet]:
        """
        Get wallets that should be scanned.

        Args:
            include_infrequent: Whether to include infrequent wallets
                               (typically done once per day)

        Returns:
            List of Wallet objects to scan
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if include_infrequent:
                # Get all wallets
                rows = conn.execute("""
                    SELECT * FROM wallets
                    ORDER BY position_value DESC NULLS LAST
                """).fetchall()
            else:
                # Get only normal frequency + never scanned
                rows = conn.execute("""
                    SELECT * FROM wallets
                    WHERE scan_frequency = 'normal' OR last_scanned IS NULL
                    ORDER BY position_value DESC NULLS LAST
                """).fetchall()

            return [self._row_to_wallet(row) for row in rows]

    def get_wallet(self, address: str) -> Optional[Wallet]:
        """Get a single wallet by address."""
        address = address.lower()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM wallets WHERE address = ?",
                (address,)
            ).fetchone()

            return self._row_to_wallet(row) if row else None

    def get_all_addresses(self) -> List[str]:
        """Get all wallet addresses."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT address FROM wallets").fetchall()
            return [row[0] for row in rows]

    def get_stats(self) -> WalletStats:
        """Get statistics about the wallet registry."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]

            from_hyperdash = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE source = 'hyperdash'"
            ).fetchone()[0]

            from_liq = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE source IN ('liq_history', 'liq_feed')"
            ).fetchone()[0]

            normal = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE scan_frequency = 'normal'"
            ).fetchone()[0]

            infrequent = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE scan_frequency = 'infrequent'"
            ).fetchone()[0]

            never = conn.execute(
                "SELECT COUNT(*) FROM wallets WHERE last_scanned IS NULL"
            ).fetchone()[0]

            total_value = conn.execute(
                "SELECT COALESCE(SUM(position_value), 0) FROM wallets"
            ).fetchone()[0]

            return WalletStats(
                total_wallets=total,
                from_hyperdash=from_hyperdash,
                from_liq_history=from_liq,
                normal_frequency=normal,
                infrequent=infrequent,
                never_scanned=never,
                total_position_value=total_value,
            )

    def get_cohort_breakdown(self) -> List[tuple]:
        """
        Get wallet count by cohort for Hyperdash wallets.

        Returns:
            List of (cohort, count, normal_count, infrequent_count) tuples
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT
                    cohort,
                    COUNT(*) as total,
                    SUM(CASE WHEN scan_frequency = 'normal' THEN 1 ELSE 0 END) as normal,
                    SUM(CASE WHEN scan_frequency = 'infrequent' THEN 1 ELSE 0 END) as infreq
                FROM wallets
                WHERE source = 'hyperdash' AND cohort IS NOT NULL
                GROUP BY cohort
                ORDER BY total DESC
            """).fetchall()
            return rows

    def get_tier_breakdown(self) -> List[tuple]:
        """
        Get wallet count by position value tier.

        Returns:
            List of (tier_name, count, total_value) tuples
        """
        tiers = [
            ("$10M+", 10_000_000, float('inf')),
            ("$1M-$10M", 1_000_000, 10_000_000),
            ("$100K-$1M", 100_000, 1_000_000),
            ("$60K-$100K", 60_000, 100_000),
            ("Below $60K", 0, 60_000),
        ]

        results = []
        with sqlite3.connect(self.db_path) as conn:
            for name, low, high in tiers:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(position_value), 0) FROM wallets WHERE position_value >= ? AND position_value < ?",
                    (low, high)
                ).fetchone()
                results.append((name, row[0], row[1]))

        return results

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    def _row_to_wallet(self, row: sqlite3.Row) -> Wallet:
        """Convert a database row to a Wallet object."""
        return Wallet(
            address=row["address"],
            source=row["source"],
            cohort=row["cohort"],
            position_value=row["position_value"],
            last_scanned=row["last_scanned"],
            scan_frequency=row["scan_frequency"],
        )


# =============================================================================
# Testing
# =============================================================================

def test_wallet_db():
    """Quick test of the wallet database."""
    import tempfile

    # Use temp file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        db = WalletDB(db_path)

        # Add some wallets
        added = db.add_wallet("0x123abc", "hyperdash", cohort="kraken")
        print(f"Added wallet: {added}")

        added = db.add_wallet("0x456def", "liq_history")
        print(f"Added wallet: {added}")

        # Try to add duplicate
        added = db.add_wallet("0x123abc", "hyperdash", cohort="large_whale")
        print(f"Added duplicate: {added} (should be False)")

        # Update scan result
        db.update_scan_result("0x123abc", position_value=1_000_000, position_count=3)

        # Get stats
        stats = db.get_stats()
        print(f"\nStats: {stats}")

        # Get wallets for scan
        wallets = db.get_wallets_for_scan()
        print(f"\nWallets for scan: {len(wallets)}")
        for w in wallets:
            print(f"  {w.address[:10]}... source={w.source} freq={w.scan_frequency}")

    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_wallet_db()
