"""
Debug Logger
============

Structured debug logging for scan phases.
Writes detailed JSON logs for each scan, organized by date.

Log structure:
    logs/
    ├── monitor_YYYY-MM-DD.log    # Main service log (text)
    └── scans/
        └── YYYY-MM-DD/
            ├── scan_HHMMSS_mode.json   # Full scan debug data
            └── ...
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Project root for log paths
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_SCANS_LOG_DIR = _PROJECT_ROOT / "logs" / "scans"


def ensure_scan_log_dir() -> Path:
    """
    Ensure the scan log directory exists for today's date.

    Returns:
        Path to today's scan log directory.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = _SCANS_LOG_DIR / today
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_scan_log_path(scan_mode: str, timestamp: Optional[datetime] = None) -> Path:
    """
    Get the path for a scan debug log file.

    Args:
        scan_mode: Scan mode (e.g., "normal", "comprehensive")
        timestamp: Scan timestamp (defaults to now)

    Returns:
        Path to the scan log file.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    log_dir = ensure_scan_log_dir()
    time_str = timestamp.strftime("%H%M%S")
    return log_dir / f"scan_{time_str}_{scan_mode}.json"


class ScanDebugLog:
    """
    Accumulates debug data for a single scan and writes to JSON.

    Usage:
        debug_log = ScanDebugLog(scan_mode="normal", is_baseline=False)
        debug_log.log_cohort_phase(traders=traders)
        debug_log.log_position_phase(positions=positions)
        debug_log.log_filter_phase(stats=stats)
        debug_log.log_watchlist_phase(watchlist=watchlist, new_positions=new_positions)
        debug_log.save()
    """

    def __init__(
        self,
        scan_mode: str,
        is_baseline: bool = False,
        timestamp: Optional[datetime] = None
    ):
        """
        Initialize a scan debug log.

        Args:
            scan_mode: Scan mode name
            is_baseline: Whether this is a baseline scan
            timestamp: Scan start timestamp
        """
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.scan_mode = scan_mode
        self.is_baseline = is_baseline
        self.log_path = get_scan_log_path(scan_mode, self.timestamp)

        self.data: Dict[str, Any] = {
            "scan_mode": scan_mode,
            "is_baseline": is_baseline,
            "timestamp": self.timestamp.isoformat(),
            "phases": {}
        }

    def log_cohort_phase(
        self,
        traders: List[Any],
        cohorts_requested: List[str],
        elapsed_seconds: Optional[float] = None
    ):
        """Log Phase 1: Cohort scraper results."""
        # Count by cohort
        cohort_counts = {}
        for trader in traders:
            cohort = getattr(trader, 'cohort', 'unknown')
            cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1

        self.data["phases"]["cohort"] = {
            "total_traders": len(traders),
            "cohorts_requested": cohorts_requested,
            "by_cohort": cohort_counts,
            "elapsed_seconds": elapsed_seconds,
        }

        logger.debug(f"[DEBUG] Cohort phase: {len(traders)} traders across {len(cohort_counts)} cohorts")

    def log_position_phase(
        self,
        positions: List[Any],
        addresses_scanned: int,
        exchanges_scanned: List[str],
        elapsed_seconds: Optional[float] = None,
        resumed: bool = False
    ):
        """Log Phase 2: Position scraper results."""
        # Count by exchange
        exchange_counts = {}
        for pos in positions:
            exchange = getattr(pos, 'exchange', 'unknown')
            exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1

        # Count by token
        token_counts = {}
        for pos in positions:
            token = getattr(pos, 'token', 'unknown')
            token_counts[token] = token_counts.get(token, 0) + 1

        # Count isolated vs cross
        isolated_count = sum(1 for pos in positions if getattr(pos, 'is_isolated', False))

        # Top tokens by count
        top_tokens = sorted(token_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        self.data["phases"]["position"] = {
            "total_positions": len(positions),
            "addresses_scanned": addresses_scanned,
            "exchanges_scanned": exchanges_scanned,
            "by_exchange": exchange_counts,
            "isolated_count": isolated_count,
            "cross_count": len(positions) - isolated_count,
            "unique_tokens": len(token_counts),
            "top_tokens": dict(top_tokens),
            "elapsed_seconds": elapsed_seconds,
            "resumed_from_progress": resumed,
        }

        logger.debug(
            f"[DEBUG] Position phase: {len(positions)} positions, "
            f"{isolated_count} isolated, {len(token_counts)} tokens"
        )

    def log_filter_phase(
        self,
        stats: Dict[str, Any],
        elapsed_seconds: Optional[float] = None
    ):
        """Log Phase 3: Liquidation filter results."""
        self.data["phases"]["filter"] = {
            **stats,
            "elapsed_seconds": elapsed_seconds,
        }

        logger.debug(
            f"[DEBUG] Filter phase: {stats.get('filtered_count', 0)} with liq prices, "
            f"{stats.get('within_3pct', 0)} within 3%"
        )

    def log_watchlist_phase(
        self,
        watchlist: Dict[str, Any],
        new_positions: List[Any],
        baseline_size: int,
        previous_size: int,
        elapsed_seconds: Optional[float] = None
    ):
        """Log Phase 4: Watchlist building results."""
        # Count by exchange
        exchange_counts = {}
        for pos in watchlist.values():
            exchange = getattr(pos, 'exchange', 'unknown')
            exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1

        # Count isolated vs cross
        isolated_count = sum(1 for pos in watchlist.values() if getattr(pos, 'is_isolated', False))

        # New positions details
        new_position_details = []
        for pos in new_positions:
            new_position_details.append({
                "address": pos.address[:10] + "...",
                "token": pos.token,
                "exchange": pos.exchange,
                "side": pos.side,
                "position_value": pos.position_value,
                "distance_pct": pos.last_distance_pct,
                "is_isolated": pos.is_isolated,
            })

        # Distance distribution
        distance_buckets = {"<1%": 0, "1-2%": 0, "2-3%": 0, "3-5%": 0}
        for pos in watchlist.values():
            dist = getattr(pos, 'last_distance_pct', 100)
            if dist < 1:
                distance_buckets["<1%"] += 1
            elif dist < 2:
                distance_buckets["1-2%"] += 1
            elif dist < 3:
                distance_buckets["2-3%"] += 1
            elif dist < 5:
                distance_buckets["3-5%"] += 1

        self.data["phases"]["watchlist"] = {
            "watchlist_size": len(watchlist),
            "isolated_count": isolated_count,
            "cross_count": len(watchlist) - isolated_count,
            "by_exchange": exchange_counts,
            "distance_distribution": distance_buckets,
            "new_positions_count": len(new_positions),
            "new_positions": new_position_details,
            "baseline_size": baseline_size,
            "previous_size": previous_size,
            "elapsed_seconds": elapsed_seconds,
        }

        logger.debug(
            f"[DEBUG] Watchlist phase: {len(watchlist)} positions, "
            f"{len(new_positions)} new"
        )

    def log_error(self, phase: str, error: str, details: Optional[Dict] = None):
        """Log an error during a phase."""
        if "errors" not in self.data:
            self.data["errors"] = []

        self.data["errors"].append({
            "phase": phase,
            "error": error,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.error(f"[DEBUG] Error in {phase}: {error}")

    def save(self) -> Path:
        """
        Save the debug log to disk.

        Returns:
            Path to the saved log file.
        """
        self.data["saved_at"] = datetime.now(timezone.utc).isoformat()

        try:
            with open(self.log_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, default=str)

            logger.info(f"Scan debug log saved: {self.log_path}")
            return self.log_path

        except OSError as e:
            logger.error(f"Failed to save debug log: {e}")
            return self.log_path


def cleanup_old_logs(days_to_keep: int = 7):
    """
    Remove scan logs older than specified days.

    Args:
        days_to_keep: Number of days of logs to retain.
    """
    if not _SCANS_LOG_DIR.exists():
        return

    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days_to_keep)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    removed_count = 0
    for date_dir in _SCANS_LOG_DIR.iterdir():
        if date_dir.is_dir() and date_dir.name < cutoff_str:
            # Remove all files in the directory
            for log_file in date_dir.iterdir():
                log_file.unlink()
                removed_count += 1
            # Remove the directory
            date_dir.rmdir()

    if removed_count > 0:
        logger.info(f"Cleaned up {removed_count} old scan logs")
