"""
Monitor Service Orchestrator
============================

Main monitoring service for liquidation hunting opportunities.

Alternates between:
- Scan phase: Full pipeline (cohort -> position -> filter -> detect new)
- Monitor phase: Poll prices and alert on proximity threshold breach

Two operating modes:
- Scheduled mode (default): Time-based scan scheduling
- Manual mode: Fixed interval between scans
"""

import csv
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import pytz
import requests

from src.models import WatchedPosition
from .alerts import TelegramAlerts, AlertConfig
from .database import MonitorDatabase
from .scan_phase import run_scan_phase
from .monitor_phase import run_monitor_phase
from config.monitor_settings import (
    SCAN_INTERVAL_MINUTES,
    POLL_INTERVAL_SECONDS,
    POSITION_CACHE_MAX_AGE_MINUTES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DEFAULT_SCAN_MODE,
    COMPREHENSIVE_SCAN_HOUR,
    COMPREHENSIVE_SCAN_MINUTE,
)

# Timezone for scheduling
EST = pytz.timezone('America/New_York')

logger = logging.getLogger(__name__)


class MonitorService:
    """
    Continuous monitoring service for liquidation hunting.

    Alternates between:
    - Scan phase: Full pipeline (cohort -> position -> filter -> detect new)
    - Monitor phase: Poll prices and alert on proximity threshold breach
    """

    def __init__(
        self,
        scan_interval_minutes: int = SCAN_INTERVAL_MINUTES,
        poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
        scan_mode: str = DEFAULT_SCAN_MODE,
        dry_run: bool = False,
        manual_mode: bool = False,
    ):
        """
        Initialize the monitor service.

        Args:
            scan_interval_minutes: Time between full scans (manual mode only)
            poll_interval_seconds: Time between price polls during monitor phase
            scan_mode: Position scan mode for manual mode ("high-priority", "normal", "comprehensive")
            dry_run: If True, print alerts instead of sending to Telegram
            manual_mode: If True, use fixed interval; if False, use time-based scheduling
        """
        self.scan_interval = scan_interval_minutes * 60  # Convert to seconds
        self.poll_interval = poll_interval_seconds
        self.scan_mode = scan_mode  # Used in manual mode or as default
        self.dry_run = dry_run
        self.manual_mode = manual_mode

        # Initialize alert system
        self.alerts = TelegramAlerts(AlertConfig(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
            dry_run=dry_run,
        ))

        # Initialize database
        self.db = MonitorDatabase()

        # State tracking
        self.watchlist: Dict[str, WatchedPosition] = {}  # position_key -> WatchedPosition
        self.previous_position_keys: Set[str] = set()  # Keys from previous scan
        self.baseline_position_keys: Set[str] = set()  # Keys from baseline (comprehensive) scan
        self.running = False
        self.last_scan_time: Optional[datetime] = None
        self._last_snapshot_time: float = 0  # For throttling position snapshots
        self._last_prune_time: float = 0  # For periodic data pruning

        # Signal handling for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Shutdown signal received, stopping monitor...")
        self.running = False
        # Save state before shutdown
        self._save_state()

    def _restore_state(self) -> bool:
        """
        Restore watchlist and baseline from database.

        Returns:
            True if state was restored (had saved data), False otherwise
        """
        try:
            # Load baseline
            self.baseline_position_keys = self.db.load_baseline()

            # Load watchlist
            saved_positions = self.db.load_watchlist()
            if not saved_positions:
                logger.info("No saved watchlist found, starting fresh")
                return False

            # Reconstruct WatchedPosition objects
            for pos_data in saved_positions:
                watched = WatchedPosition(
                    address=pos_data['address'],
                    token=pos_data['token'],
                    exchange=pos_data['exchange'],
                    side=pos_data['side'],
                    liq_price=pos_data['liq_price'],
                    position_value=pos_data['position_value'],
                    is_isolated=pos_data['is_isolated'],
                    hunting_score=pos_data['hunting_score'],
                    last_distance_pct=pos_data['last_distance_pct'],
                    last_mark_price=pos_data['last_mark_price'],
                    threshold_pct=pos_data['threshold_pct'],
                    alerted_proximity=pos_data['alerted_proximity'],
                    alerted_critical=pos_data['alerted_critical'],
                    in_critical_zone=pos_data['in_critical_zone'],
                    first_seen_scan=pos_data['first_seen_scan'],
                    alert_message_id=pos_data['alert_message_id'],
                    last_proximity_message_id=pos_data['last_proximity_message_id'],
                )
                self.watchlist[watched.position_key] = watched

            self.previous_position_keys = set(self.watchlist.keys())

            logger.info(
                f"Restored state: {len(self.watchlist)} watchlist, "
                f"{len(self.baseline_position_keys)} baseline"
            )
            return True

        except Exception as e:
            logger.warning(f"Failed to restore state: {e}, starting fresh")
            return False

    def _save_state(self):
        """Save watchlist and baseline to database."""
        try:
            self.db.save_watchlist(self.watchlist)
            self.db.save_baseline(self.baseline_position_keys)
            logger.debug("State saved to database")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _record_position_snapshots(self):
        """Record position snapshots for history tracking (throttled)."""
        now = time.time()

        # Only snapshot every 5 minutes to avoid excessive writes
        if now - self._last_snapshot_time < 300:
            return

        try:
            snapshots = [
                {
                    'position_key': pos.position_key,
                    'liq_price': pos.liq_price,
                    'position_value': pos.position_value,
                    'distance_pct': pos.last_distance_pct,
                    'mark_price': pos.last_mark_price,
                }
                for pos in self.watchlist.values()
                if pos.last_distance_pct is not None and pos.last_mark_price is not None
            ]

            if snapshots:
                self.db.record_position_snapshots_batch(snapshots)

            self._last_snapshot_time = now

        except Exception as e:
            logger.warning(f"Failed to record position snapshots: {e}")

    def _maybe_prune_data(self):
        """Periodically prune old data (once per day)."""
        now = time.time()

        # Prune once per day (86400 seconds)
        if now - self._last_prune_time < 86400:
            return

        try:
            self.db.prune_old_data()
            self._last_prune_time = now
        except Exception as e:
            logger.warning(f"Failed to prune old data: {e}")

    def _get_scan_mode_for_time(self, dt: datetime) -> str:
        """
        Determine the scan mode for a given time.

        Schedule (all times in EST):
        - 6:30 AM -> "comprehensive" (baseline scan)
        - :00 (on the hour) -> "normal"
        - :30 (half hour) -> "high-priority"

        Args:
            dt: Datetime to check (will be converted to EST)

        Returns:
            Scan mode string: "comprehensive", "normal", or "high-priority"
        """
        # Convert to EST
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        est_time = dt.astimezone(EST)

        hour = est_time.hour
        minute = est_time.minute

        # Check for comprehensive scan time (6:30 AM EST)
        if hour == COMPREHENSIVE_SCAN_HOUR and minute == COMPREHENSIVE_SCAN_MINUTE:
            return "comprehensive"

        # On the hour (:00) -> normal scan
        if minute < 15:
            return "normal"

        # Half hour (:30) -> priority scan
        return "high-priority"

    def _get_next_scan_time(self) -> datetime:
        """
        Get the next scheduled scan time.

        Returns the next :00 or :30 time slot in UTC.

        Returns:
            datetime in UTC for the next scan
        """
        now_utc = datetime.now(timezone.utc)
        now_est = now_utc.astimezone(EST)

        # Current minute determines next slot
        current_minute = now_est.minute

        if current_minute < 30:
            # Next slot is :30
            next_minute = 30
            next_hour = now_est.hour
        else:
            # Next slot is :00 of next hour
            next_minute = 0
            next_hour = now_est.hour + 1

        # Create next scan time in EST
        next_scan_est = now_est.replace(
            hour=next_hour % 24,
            minute=next_minute,
            second=0,
            microsecond=0
        )

        # Handle day rollover
        if next_hour >= 24:
            next_scan_est = next_scan_est + timedelta(days=1)
            next_scan_est = next_scan_est.replace(hour=next_hour % 24)

        # Convert back to UTC
        return next_scan_est.astimezone(timezone.utc)

    def _get_cached_position_file_age(self, filepath: str) -> Optional[float]:
        """
        Get the age of a cached position file in minutes.

        Args:
            filepath: Path to the position file

        Returns:
            Age in minutes, or None if file doesn't exist
        """
        try:
            path = Path(filepath)
            if not path.exists():
                return None
            mtime = path.stat().st_mtime
            age_seconds = time.time() - mtime
            return age_seconds / 60
        except OSError:
            return None

    def _can_use_cached_positions(self, filepath: str) -> bool:
        """
        Check if cached position data is fresh enough to use as fallback.

        Args:
            filepath: Path to the position file

        Returns:
            True if cache is valid and fresh enough
        """
        age_minutes = self._get_cached_position_file_age(filepath)
        if age_minutes is None:
            return False
        return age_minutes <= POSITION_CACHE_MAX_AGE_MINUTES

    def run_scan_phase(
        self,
        mode: Optional[str] = None,
        is_baseline: bool = False,
        notify_cohorts: bool = False,
        send_summary: bool = False
    ) -> Tuple[int, int]:
        """
        Execute the scan phase of the monitor loop.

        Delegates to scan_phase module.
        """
        return run_scan_phase(self, mode, is_baseline, notify_cohorts, send_summary)

    def run_monitor_phase(self, until: Optional[datetime] = None):
        """
        Execute the monitor phase - poll prices until specified time or next scan.

        Delegates to monitor_phase module.
        """
        run_monitor_phase(self, until)

    def _run_manual_mode(self):
        """
        Run in manual mode with fixed interval between scans.

        Original behavior: scan, monitor for interval, repeat.
        """
        logger.info(f"Mode: MANUAL (fixed {self.scan_interval // 60}min interval)")
        logger.info(f"Scan mode: {self.scan_mode}")

        # Send startup notification
        self.alerts.send_service_status(
            "started",
            f"Manual mode | Interval: {self.scan_interval // 60}min | Mode: {self.scan_mode}"
        )

        while self.running:
            # Scan phase (always baseline in manual mode)
            total, new_count = self.run_scan_phase(is_baseline=True)

            if not self.running:
                break

            if total == 0:
                logger.warning("No positions to watch, waiting 60s before retry...")
                time.sleep(60)
                continue

            # Monitor phase
            self.run_monitor_phase()

    def _run_scheduled_mode(self):
        """
        Run in scheduled mode with time-based scan scheduling.

        Schedule (all times EST):
        - 6:30 AM: Comprehensive scan (baseline reset)
        - Every hour (:00): Normal scan
        - Every 30 min (:30): Priority scan

        On startup: always runs progressive scan with rate limit protection.
        """
        now = datetime.now(timezone.utc)
        now_est = now.astimezone(EST)
        logger.info(f"Mode: SCHEDULED (time-based)")
        logger.info(f"Current time: {now_est.strftime('%H:%M EST')}")
        logger.info(f"Comprehensive scan at: {COMPREHENSIVE_SCAN_HOUR:02d}:{COMPREHENSIVE_SCAN_MINUTE:02d} EST")

        # Rate limit protection: check if last scan was too recent
        SCAN_COOLDOWN_MINUTES = 5
        last_scan = self.db.get_last_scan_time()
        if last_scan:
            elapsed = (now - last_scan).total_seconds()
            cooldown_seconds = SCAN_COOLDOWN_MINUTES * 60
            if elapsed < cooldown_seconds:
                wait_seconds = int(cooldown_seconds - elapsed)
                logger.info(
                    f"Rate limit protection: last scan was {elapsed:.0f}s ago, "
                    f"waiting {wait_seconds}s before starting"
                )
                restart_time = now + timedelta(seconds=wait_seconds)
                restart_time_et = restart_time.astimezone(pytz.timezone("America/New_York"))
                self.alerts.send_service_status(
                    "started",
                    f"Not monitoring positions (rate limit cooldown)\n"
                    f"Estimated restart: {restart_time_et.strftime('%H:%M:%S %Z')}"
                )
                time.sleep(wait_seconds)

        # Always do progressive startup scan
        logger.info("Starting progressive scan")

        # Send startup notification
        self.alerts.send_service_status(
            "started",
            "Scheduled mode | Progressive startup scan"
        )

        # Progressive startup scan: incremental additions only (no re-scanning)
        # Phase 1: kraken + large_whale (main, xyz)
        # Phase 2: whale only (main, xyz) - incremental
        # Phase 3: shark only + remaining exchanges - incremental
        logger.info("=" * 60)
        logger.info("PROGRESSIVE STARTUP SCAN (incremental)")
        logger.info("=" * 60)

        # Phase 1: High-priority (kraken + large_whale, main + xyz)
        logger.info("Phase 1/3: High-priority scan (largest traders, main exchanges)")
        self.alerts.send_startup_phase_alert(
            phase=1,
            total_phases=3,
            phase_name="High-priority",
            description="Scanning kraken + large_whale cohorts\nExchanges: main, xyz"
        )
        total, new_count = self.run_scan_phase(mode="high-priority", is_baseline=True, notify_cohorts=True, send_summary=True)

        if not self.running:
            return

        # Phase 2: Whale only (incremental - not re-scanning kraken/large_whale)
        logger.info("Phase 2/3: Whale scan (incremental)")
        self.alerts.send_startup_phase_alert(
            phase=2,
            total_phases=3,
            phase_name="Whale",
            description="Adding whale cohort only\nExchanges: main, xyz"
        )
        total, new_count = self.run_scan_phase(mode="whale-only", is_baseline=False, notify_cohorts=True, send_summary=True)

        if not self.running:
            return

        # Phase 3: Shark + remaining exchanges (incremental)
        logger.info("Phase 3/3: Shark + additional exchanges (incremental)")
        self.alerts.send_startup_phase_alert(
            phase=3,
            total_phases=3,
            phase_name="Shark + Extra Exchanges",
            description="Adding shark cohort\nAdding exchanges: flx, vntl, hyna, km"
        )
        total, new_count = self.run_scan_phase(mode="shark-incremental", is_baseline=False, notify_cohorts=True, send_summary=True)

        if not self.running:
            return

        # Set final baseline from comprehensive scan
        self.baseline_position_keys = set(self.watchlist.keys())
        logger.info(f"Progressive scan complete. Final baseline: {len(self.baseline_position_keys)} positions")

        if total == 0:
            logger.warning("No positions from startup scan, waiting 60s before retry...")
            time.sleep(60)

        # Enter main scheduled loop
        self._run_scheduled_main_loop()

    def _run_scheduled_main_loop(self):
        """
        Main scheduled loop - runs after startup (fresh or restored).

        Alternates between monitor phase and scheduled scans.
        """
        while self.running:
            # Get next scan time and mode
            next_scan_time = self._get_next_scan_time()
            next_scan_mode = self._get_scan_mode_for_time(next_scan_time)
            is_baseline = (next_scan_mode == "comprehensive")

            next_est = next_scan_time.astimezone(EST)
            logger.info(
                f"Next scan at {next_est.strftime('%H:%M EST')}: "
                f"mode={next_scan_mode}" + (" (baseline)" if is_baseline else "")
            )

            # Monitor phase until next scan time
            self.run_monitor_phase(until=next_scan_time)

            if not self.running:
                break

            # Run the scheduled scan
            total, new_count = self.run_scan_phase(mode=next_scan_mode, is_baseline=is_baseline)

            if total == 0:
                logger.warning("No positions to watch, waiting 60s before retry...")
                time.sleep(60)

    def run(self):
        """
        Main entry point - run the continuous monitor loop.

        In scheduled mode (default):
        - 6:30 AM EST: Comprehensive scan (baseline)
        - Every hour (:00): Normal scan
        - Every 30 min (:30): Priority scan

        In manual mode (--manual flag):
        - Fixed interval between scans (original behavior)
        """
        self.running = True
        logger.info("=" * 60)
        logger.info("LIQUIDATION MONITOR SERVICE STARTING")
        logger.info("=" * 60)
        logger.info(f"Poll interval: {self.poll_interval} seconds")
        logger.info(f"Dry run: {self.dry_run}")

        try:
            if self.manual_mode:
                self._run_manual_mode()
            else:
                self._run_scheduled_mode()

        except KeyboardInterrupt:
            logger.info("Service interrupted by user")
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error: {type(e).__name__}"
            logger.error(f"Service error: {error_msg}")
            self.alerts.send_service_status("error", error_msg)
            raise
        except (OSError, csv.Error) as e:
            error_msg = f"File error: {type(e).__name__}"
            logger.error(f"Service error: {error_msg}")
            self.alerts.send_service_status("error", error_msg)
            raise
        except Exception as e:
            # Catch-all for unexpected errors - log type only to avoid exposing sensitive data
            error_msg = f"Unexpected error: {type(e).__name__}"
            logger.error(f"Service error: {error_msg}")
            self.alerts.send_service_status("error", error_msg)
            raise

        finally:
            logger.info("LIQUIDATION MONITOR SERVICE STOPPED")
            self.alerts.send_service_status("stopped")

    def stop(self):
        """Stop the monitor service gracefully."""
        self.running = False
