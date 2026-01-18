"""
Monitor Service
===============

Main monitoring service for liquidation hunting opportunities.

Two phases:
1. Scan Phase (every T minutes): cohort -> position -> filter -> alert on NEW positions
2. Monitor Phase (between scans): poll mark prices -> alert when proximity drops below threshold
"""

import csv
import logging
import os
import time
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytz
import requests

from .alerts import TelegramAlerts, AlertConfig

# Allowed base directories for file operations (security: prevent path traversal)
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
ALLOWED_DATA_DIRS = [
    PROJECT_ROOT / "data" / "raw",
    PROJECT_ROOT / "data" / "processed",
]

# Import from project modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.scrapers.cohort import fetch_cohorts, save_to_csv as save_cohort_csv, PRIORITY_COHORTS
from src.scrapers.position import (
    load_cohort_addresses,
    fetch_all_mark_prices,
    fetch_all_positions,
    fetch_all_positions_for_address,
    save_to_csv as save_position_csv,
    SCAN_MODES,
)
from src.filters.liquidation import (
    filter_positions,
    calculate_distance_to_liquidation,
    get_current_price,
)
from config.monitor_settings import (
    SCAN_INTERVAL_MINUTES,
    POLL_INTERVAL_SECONDS,
    MAX_WATCH_DISTANCE_PCT,
    MIN_HUNTING_SCORE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DEFAULT_SCAN_MODE,
    COHORT_DATA_PATH,
    get_proximity_alert_threshold,
    passes_new_position_threshold,
    CRITICAL_ZONE_PCT,
    CRITICAL_ALERT_PCT,
    RECOVERY_PCT,
    CRITICAL_REFRESH_MIN_INTERVAL,
    CRITICAL_REFRESH_MAX_INTERVAL,
    CRITICAL_REFRESH_SCALE_FACTOR,
    MAX_CRITICAL_POSITIONS,
    COMPREHENSIVE_SCAN_HOUR,
    COMPREHENSIVE_SCAN_MINUTE,
)

# Timezone for scheduling
EST = pytz.timezone('America/New_York')

logger = logging.getLogger(__name__)


def _validate_file_path(filepath: str, must_exist: bool = False) -> Path:
    """
    Validate that a file path is within allowed directories.

    Security: Prevents path traversal attacks by ensuring all file operations
    are within expected data directories.

    Args:
        filepath: Path to validate
        must_exist: If True, raise error if file doesn't exist

    Returns:
        Resolved Path object

    Raises:
        ValueError: If path is outside allowed directories
        FileNotFoundError: If must_exist=True and file doesn't exist
    """
    path = Path(filepath).resolve()

    # Check if path is within any allowed directory
    is_allowed = any(
        path.is_relative_to(allowed_dir) or path.parent.resolve() == allowed_dir
        for allowed_dir in ALLOWED_DATA_DIRS
    )

    if not is_allowed:
        raise ValueError(
            f"File path '{filepath}' is outside allowed directories. "
            f"Allowed: {[str(d) for d in ALLOWED_DATA_DIRS]}"
        )

    if must_exist and not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    # Ensure parent directory exists for write operations
    path.parent.mkdir(parents=True, exist_ok=True)

    return path


def _sanitize_csv_value(value: str) -> str:
    """
    Sanitize a CSV value to prevent CSV injection attacks.

    CSV injection occurs when values starting with =, +, -, @, tab, or carriage
    return are interpreted as formulas by spreadsheet applications.

    Args:
        value: Raw string value from CSV

    Returns:
        Sanitized string safe for spreadsheet use
    """
    if not isinstance(value, str):
        return value

    # Characters that trigger formula interpretation in Excel/Sheets
    dangerous_prefixes = ('=', '+', '-', '@', '\t', '\r', '\n')

    if value.startswith(dangerous_prefixes):
        # Prefix with single quote to force text interpretation
        return "'" + value

    return value


@dataclass
class WatchedPosition:
    """
    A position being monitored for liquidation proximity.

    Tracks the position details and monitoring state.
    """
    # Position identification
    address: str
    token: str
    exchange: str
    side: str

    # Position details
    liq_price: float
    position_value: float
    is_isolated: bool
    hunting_score: float

    # Tracking state
    last_distance_pct: float = None
    last_mark_price: float = None
    threshold_pct: float = None  # Computed from multi-factorial rules
    alerted_proximity: bool = False  # Already sent proximity alert (0.5%)
    alerted_critical: bool = False  # Already sent critical alert (0.1%)
    in_critical_zone: bool = False  # Currently in critical zone (<0.2%)
    first_seen_scan: str = None  # Timestamp of first detection
    alert_message_id: int = None  # Telegram message_id for reply threading
    last_proximity_message_id: int = None  # Last proximity/critical alert message_id

    @property
    def position_key(self) -> str:
        """Unique key for this position (address + token + exchange + side)."""
        return f"{self.address}:{self.token}:{self.exchange}:{self.side}"

    def __hash__(self):
        return hash(self.position_key)

    def __eq__(self, other):
        if isinstance(other, WatchedPosition):
            return self.position_key == other.position_key
        return False


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

        # State tracking
        self.watchlist: Dict[str, WatchedPosition] = {}  # position_key -> WatchedPosition
        self.previous_position_keys: Set[str] = set()  # Keys from previous scan
        self.baseline_position_keys: Set[str] = set()  # Keys from baseline (comprehensive) scan
        self.running = False
        self.last_scan_time: Optional[datetime] = None

        # Signal handling for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Shutdown signal received, stopping monitor...")
        self.running = False

    def _get_scan_mode_for_time(self, dt: datetime) -> str:
        """
        Determine the scan mode for a given time.

        Schedule (all times in EST):
        - 6:30 AM → "comprehensive" (baseline scan)
        - :00 (on the hour) → "normal"
        - :30 (half hour) → "high-priority"

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

        # On the hour (:00) → normal scan
        if minute < 15:
            return "normal"

        # Half hour (:30) → priority scan
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

    def _load_filtered_positions(self, filepath: str) -> List[Dict]:
        """
        Load filtered position data from CSV.

        Includes CSV injection protection by sanitizing values.

        Args:
            filepath: Path to filtered positions CSV

        Returns:
            List of position dictionaries (sanitized)
        """
        positions = []
        try:
            # Validate path is within allowed directories
            validated_path = _validate_file_path(filepath, must_exist=True)

            with open(validated_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Sanitize all string values to prevent CSV injection
                    sanitized_row = {
                        key: _sanitize_csv_value(value) if isinstance(value, str) else value
                        for key, value in row.items()
                    }
                    positions.append(sanitized_row)

            logger.info(f"Loaded {len(positions)} filtered positions from {filepath}")

        except FileNotFoundError:
            logger.error(f"Filtered positions file not found: {filepath}")
        except ValueError as e:
            logger.error(f"Invalid file path: {e}")
        except csv.Error as e:
            logger.error(f"CSV parsing error: {e}")
        except OSError as e:
            logger.error(f"File read error: {e}")

        return positions

    def _build_watchlist(self, filtered_positions: List[Dict]) -> Dict[str, WatchedPosition]:
        """
        Build watchlist from filtered position data.

        Filters by:
        - Distance within MAX_WATCH_DISTANCE_PCT
        - Hunting score above MIN_HUNTING_SCORE

        Args:
            filtered_positions: List of position dicts from filtered CSV

        Returns:
            Dict mapping position_key to WatchedPosition
        """
        watchlist = {}
        scan_time = datetime.now(timezone.utc).isoformat()

        for row in filtered_positions:
            try:
                # Parse required fields
                address = row['Address']
                token = row['Token']
                exchange = row['Exchange']
                side = row['Side']
                liq_price = float(row['Liquidation Price'])
                position_value = float(row['Position Value'])
                is_isolated = row['Isolated'].lower() == 'true'
                hunting_score = float(row['Hunting Score'])
                distance_pct = float(row['Distance to Liq (%)'])
                current_price = float(row['Current Price'])

                # Apply filters
                if distance_pct > MAX_WATCH_DISTANCE_PCT:
                    continue
                if hunting_score < MIN_HUNTING_SCORE:
                    continue

                # Create watched position
                watched = WatchedPosition(
                    address=address,
                    token=token,
                    exchange=exchange,
                    side=side,
                    liq_price=liq_price,
                    position_value=position_value,
                    is_isolated=is_isolated,
                    hunting_score=hunting_score,
                    last_distance_pct=distance_pct,
                    last_mark_price=current_price,
                    threshold_pct=get_proximity_alert_threshold(),
                    first_seen_scan=scan_time,
                )

                watchlist[watched.position_key] = watched

            except (KeyError, ValueError) as e:
                logger.debug(f"Skipping position due to parsing error: {e}")
                continue

        logger.info(f"Built watchlist with {len(watchlist)} positions")
        return watchlist

    def _detect_new_positions(
        self,
        current_watchlist: Dict[str, WatchedPosition],
        is_baseline: bool = False
    ) -> List[WatchedPosition]:
        """
        Detect positions that are new since the baseline scan AND pass alert thresholds.

        In scheduled mode:
        - Baseline (comprehensive) scan: compare against previous baseline
        - Normal/priority scans: compare against current day's baseline

        In manual mode:
        - Always compare against previous scan

        Filters by asset-tier thresholds:
        - At ≤3% distance: BTC $100M, majors $50M, alts $5M
        - At ≤1% distance: BTC $50M, majors $25M, alts $2M
        - Isolated positions get 5x multiplier to notional

        Args:
            current_watchlist: Current watchlist after filtering
            is_baseline: If True, this is a baseline scan (compare against previous baseline)

        Returns:
            List of new WatchedPositions that pass thresholds
        """
        current_keys = set(current_watchlist.keys())

        # In manual mode or baseline scan, compare against previous_position_keys
        # In scheduled mode non-baseline scan, compare against baseline_position_keys
        if self.manual_mode or is_baseline:
            compare_keys = self.previous_position_keys
        else:
            compare_keys = self.baseline_position_keys

        new_keys = current_keys - compare_keys

        # Filter new positions by alert thresholds
        new_positions = []
        for key in new_keys:
            pos = current_watchlist[key]
            if passes_new_position_threshold(
                token=pos.token,
                position_value=pos.position_value,
                distance_pct=pos.last_distance_pct,
                is_isolated=pos.is_isolated
            ):
                new_positions.append(pos)

        # Sort by hunting score descending
        new_positions.sort(key=lambda p: p.hunting_score, reverse=True)

        comparison_type = "previous scan" if (self.manual_mode or is_baseline) else "baseline"
        logger.info(
            f"Detected {len(new_keys)} new positions (vs {comparison_type}), "
            f"{len(new_positions)} pass alert thresholds"
        )
        return new_positions

    def run_scan_phase(
        self,
        mode: Optional[str] = None,
        is_baseline: bool = False
    ) -> Tuple[int, int]:
        """
        Execute the scan phase of the monitor loop.

        Steps:
        1. Run cohort scraper
        2. Run position scraper
        3. Run liquidation filter
        4. Detect new positions
        5. Send alerts for new positions
        6. Update watchlist

        Args:
            mode: Scan mode override ("high-priority", "normal", "comprehensive").
                  If None, uses self.scan_mode.
            is_baseline: If True, this is a baseline scan:
                         - Reset baseline_position_keys
                         - Full watchlist replacement
                         - Alerts compare against previous baseline

        Returns:
            Tuple of (total_positions, new_positions_count)
        """
        # Use provided mode or fall back to instance default
        scan_mode = mode or self.scan_mode

        logger.info("=" * 60)
        logger.info(f"SCAN PHASE STARTING - Mode: {scan_mode}" +
                   (" (BASELINE)" if is_baseline else ""))
        logger.info("=" * 60)

        scan_start = datetime.now(timezone.utc)

        # Step 1: Fetch cohort data
        logger.info("Step 1: Fetching cohort data...")
        try:
            traders = fetch_cohorts(PRIORITY_COHORTS, delay=1.0)
            save_cohort_csv(traders, COHORT_DATA_PATH)
            logger.info(f"Saved {len(traders)} traders to {COHORT_DATA_PATH}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Cohort fetch network error: {type(e).__name__}")
            return 0, 0
        except (OSError, csv.Error) as e:
            logger.error(f"Cohort data save error: {type(e).__name__}: {e}")
            return 0, 0

        # Step 2: Fetch position data
        logger.info("Step 2: Fetching position data...")
        position_scan_time = datetime.now(timezone.utc)
        try:
            # Get scan mode config
            mode_config = SCAN_MODES.get(scan_mode, SCAN_MODES["normal"])
            cohorts = mode_config["cohorts"]
            dexes = mode_config["dexes"]

            # Load addresses
            addresses = load_cohort_addresses(COHORT_DATA_PATH, cohorts=cohorts)
            if not addresses:
                logger.error("No addresses loaded from cohort data")
                return 0, 0

            # Fetch mark prices
            mark_prices = fetch_all_mark_prices()
            if not mark_prices:
                logger.error("Failed to fetch mark prices")
                return 0, 0

            # Fetch positions
            positions = fetch_all_positions(addresses, mark_prices, dexes)

            # Save to temporary file for filter (with path validation)
            position_file = "data/raw/position_data_monitor.csv"
            validated_position_path = _validate_file_path(position_file)
            save_position_csv(positions, str(validated_position_path))
            logger.info(f"Saved {len(positions)} positions to {position_file}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Position fetch network error: {type(e).__name__}")
            return 0, 0
        except (OSError, csv.Error) as e:
            logger.error(f"Position data save error: {type(e).__name__}: {e}")
            return 0, 0
        except ValueError as e:
            logger.error(f"Invalid file path: {e}")
            return 0, 0

        # Step 3: Run liquidation filter
        logger.info("Step 3: Running liquidation filter...")
        try:
            filtered_file = "data/processed/filtered_position_data_monitor.csv"
            # Validate output path
            validated_filtered_path = _validate_file_path(filtered_file)
            stats = filter_positions(str(validated_position_path), str(validated_filtered_path))
            if not stats:
                logger.error("Filter returned no results")
                return 0, 0
            logger.info(f"Filter complete: {stats.get('filtered_count', 0)} positions with liq prices")
        except requests.exceptions.RequestException as e:
            logger.error(f"Filter network error (order book fetch): {type(e).__name__}")
            return 0, 0
        except (OSError, csv.Error) as e:
            logger.error(f"Filter file error: {type(e).__name__}: {e}")
            return 0, 0
        except ValueError as e:
            logger.error(f"Filter error: {e}")
            return 0, 0

        # Step 4: Build new watchlist and detect new positions
        logger.info("Step 4: Building watchlist and detecting new positions...")
        filtered_positions = self._load_filtered_positions(str(validated_filtered_path))
        new_watchlist = self._build_watchlist(filtered_positions)

        # Detect new positions (pass is_baseline flag)
        new_positions = self._detect_new_positions(new_watchlist, is_baseline=is_baseline)

        # Step 5: Send alerts for new positions
        if new_positions:
            logger.info(f"Step 5: Alerting {len(new_positions)} new positions...")
            message_id = self.alerts.send_new_positions_alert(new_positions, position_scan_time)
            # Store message_id in each alerted position for reply threading
            if message_id:
                for pos in new_positions:
                    pos.alert_message_id = message_id
        else:
            logger.info("Step 5: No new positions to alert")

        # Step 6: Update state
        if is_baseline:
            # Baseline scan: full watchlist replacement, reset baseline keys
            self.watchlist = new_watchlist
            self.baseline_position_keys = set(new_watchlist.keys())
            logger.info(f"Baseline reset: {len(self.baseline_position_keys)} positions in baseline")
        else:
            # Non-baseline scan: merge into existing watchlist
            # Preserve alert flags and message IDs for positions that still exist
            for key, new_pos in new_watchlist.items():
                if key in self.watchlist:
                    old_pos = self.watchlist[key]
                    new_pos.alerted_proximity = old_pos.alerted_proximity
                    new_pos.alerted_critical = old_pos.alerted_critical
                    new_pos.in_critical_zone = old_pos.in_critical_zone
                    new_pos.alert_message_id = old_pos.alert_message_id
                    new_pos.last_proximity_message_id = old_pos.last_proximity_message_id
                # Add or update position in watchlist
                self.watchlist[key] = new_pos

        self.previous_position_keys = set(new_watchlist.keys())
        self.last_scan_time = scan_start

        logger.info("=" * 60)
        logger.info(f"SCAN PHASE COMPLETE - {len(self.watchlist)} positions in watchlist")
        logger.info("=" * 60)

        return len(self.watchlist), len(new_positions)

    def _refresh_critical_positions(self, mark_prices: Dict) -> int:
        """
        Refresh position data for positions in critical zone (<0.2%).

        Fetches fresh clearinghouseState for each critical position to get
        updated liquidation prices (in case of margin changes).

        Args:
            mark_prices: Current mark prices by exchange

        Returns:
            Number of positions refreshed
        """
        critical_positions = [
            pos for pos in self.watchlist.values()
            if pos.in_critical_zone
        ]

        if not critical_positions:
            return 0

        # Sort by distance (closest first) and truncate to max
        critical_positions.sort(key=lambda p: p.last_distance_pct)
        if len(critical_positions) > MAX_CRITICAL_POSITIONS:
            logger.info(
                f"Truncating critical positions from {len(critical_positions)} "
                f"to {MAX_CRITICAL_POSITIONS} (prioritizing closest)"
            )
            critical_positions = critical_positions[:MAX_CRITICAL_POSITIONS]

        refreshed = 0
        for position in critical_positions:
            try:
                # Fetch fresh position data for this address
                dexes = [""] if position.exchange == "main" else [position.exchange]
                positions = fetch_all_positions_for_address(
                    position.address,
                    mark_prices,
                    dexes=dexes
                )

                # Find the matching position
                for pos_data in positions:
                    if (
                        pos_data.get("Token") == position.token and
                        pos_data.get("Exchange") == position.exchange and
                        pos_data.get("Side") == position.side
                    ):
                        # Update liquidation price if changed
                        new_liq_price = pos_data.get("Liquidation Price")
                        if new_liq_price and new_liq_price != position.liq_price:
                            logger.info(
                                f"Critical position {position.token} liq price updated: "
                                f"{position.liq_price:.4f} -> {new_liq_price:.4f}"
                            )
                            position.liq_price = new_liq_price

                        # Update position value if changed
                        new_value = pos_data.get("Position Value")
                        if new_value:
                            position.position_value = new_value

                        refreshed += 1
                        break

            except Exception as e:
                logger.warning(f"Failed to refresh critical position {position.token}: {e}")

        if refreshed > 0:
            logger.info(f"Refreshed {refreshed} critical positions")

        return refreshed

    def _get_critical_refresh_interval(self, critical_count: int) -> float:
        """
        Calculate dynamic refresh interval based on number of critical positions.

        Scales linearly (capped at 30 positions):
        - 1 position: ~2.3s
        - 5 positions: ~3.5s
        - 10+ positions: 5s (max interval)

        Args:
            critical_count: Number of positions in critical zone

        Returns:
            Refresh interval in seconds
        """
        interval = CRITICAL_REFRESH_MIN_INTERVAL + (critical_count * CRITICAL_REFRESH_SCALE_FACTOR)
        return min(interval, CRITICAL_REFRESH_MAX_INTERVAL)

    def run_monitor_phase(self, until: Optional[datetime] = None):
        """
        Execute the monitor phase - poll prices until specified time or next scan.

        Priority monitoring for critical positions (<0.2%):
        - Refresh position data at dynamic interval (scales with position count)
        - Alert at 0.1% threshold (critical alert)
        - Alert when recovering from <0.2% to >0.5% (recovery alert)

        Args:
            until: datetime (UTC) to run until. If None, uses self.scan_interval from now.
        """
        if until is None:
            next_scan_time = time.time() + self.scan_interval
            next_scan_display = "in {} minutes".format(self.scan_interval // 60)
        else:
            next_scan_time = until.timestamp()
            next_scan_est = until.astimezone(EST)
            next_scan_display = next_scan_est.strftime("%H:%M EST")

        logger.info("MONITOR PHASE STARTING")
        logger.info(f"Watching {len(self.watchlist)} positions")
        logger.info(f"Poll interval: {self.poll_interval}s")
        logger.info(f"Next scan: {next_scan_display}")

        last_critical_refresh = 0

        while self.running and time.time() < next_scan_time:
            try:
                # Fetch current prices
                mark_prices = fetch_all_mark_prices()
                if not mark_prices:
                    logger.warning("Failed to fetch mark prices, retrying...")
                    time.sleep(self.poll_interval)
                    continue

                # Priority: Refresh critical positions if interval elapsed
                critical_count = sum(1 for p in self.watchlist.values() if p.in_critical_zone)
                if critical_count > 0:
                    now = time.time()
                    # Use capped count for interval calculation (matches truncation in refresh)
                    capped_count = min(critical_count, MAX_CRITICAL_POSITIONS)
                    refresh_interval = self._get_critical_refresh_interval(capped_count)
                    if now - last_critical_refresh >= refresh_interval:
                        logger.debug(
                            f"Critical refresh: {critical_count} positions, "
                            f"interval: {refresh_interval:.0f}s"
                        )
                        self._refresh_critical_positions(mark_prices)
                        last_critical_refresh = now

                # Check each watched position
                alerts_sent = 0
                for key, position in self.watchlist.items():
                    current_price = get_current_price(
                        position.token,
                        position.exchange,
                        mark_prices
                    )

                    if current_price == 0:
                        continue

                    # Calculate new distance
                    new_distance = calculate_distance_to_liquidation(
                        current_price,
                        position.liq_price,
                        position.side
                    )

                    previous_distance = position.last_distance_pct
                    was_in_critical_zone = position.in_critical_zone

                    # Update position state
                    position.last_mark_price = current_price
                    position.last_distance_pct = new_distance
                    position.in_critical_zone = new_distance < CRITICAL_ZONE_PCT

                    # Determine reply_to for threading
                    reply_to = position.last_proximity_message_id or position.alert_message_id

                    # Check for RECOVERY: was in critical zone (<0.2%), now recovered (>0.5%)
                    if (
                        was_in_critical_zone and
                        new_distance > RECOVERY_PCT
                    ):
                        logger.info(
                            f"RECOVERY: {position.token} {position.side} "
                            f"recovered from {previous_distance:.3f}% to {new_distance:.2f}%"
                        )
                        msg_id = self.alerts.send_recovery_alert(
                            position,
                            previous_distance,
                            current_price,
                            reply_to_message_id=reply_to
                        )
                        if msg_id:
                            position.last_proximity_message_id = msg_id
                            # Reset alert flags since position recovered
                            position.alerted_critical = False
                            position.in_critical_zone = False
                            alerts_sent += 1
                        continue  # Skip other checks for this position

                    # Check for CRITICAL alert (crossing below 0.1%)
                    if (
                        not position.alerted_critical and
                        new_distance <= CRITICAL_ALERT_PCT and
                        (previous_distance is None or previous_distance > CRITICAL_ALERT_PCT)
                    ):
                        logger.info(
                            f"CRITICAL ALERT: {position.token} {position.side} "
                            f"at {new_distance:.3f}% (threshold: {CRITICAL_ALERT_PCT}%)"
                        )
                        msg_id = self.alerts.send_critical_alert(
                            position,
                            previous_distance or new_distance,
                            current_price,
                            reply_to_message_id=reply_to
                        )
                        if msg_id:
                            position.alerted_critical = True
                            position.last_proximity_message_id = msg_id
                            alerts_sent += 1

                    # Check for PROXIMITY alert (crossing below 0.5%)
                    elif (
                        not position.alerted_proximity and
                        new_distance <= RECOVERY_PCT and
                        (previous_distance is None or previous_distance > RECOVERY_PCT)
                    ):
                        logger.info(
                            f"PROXIMITY ALERT: {position.token} {position.side} "
                            f"at {new_distance:.2f}% (threshold: {RECOVERY_PCT}%)"
                        )
                        msg_id = self.alerts.send_proximity_alert(
                            position,
                            previous_distance or new_distance,
                            current_price
                        )
                        if msg_id:
                            position.alerted_proximity = True
                            position.last_proximity_message_id = msg_id
                            alerts_sent += 1

                if alerts_sent > 0:
                    logger.info(f"Sent {alerts_sent} alerts")

                # Log periodic status
                remaining = int(next_scan_time - time.time())
                if remaining % 300 < self.poll_interval:  # Every ~5 minutes
                    logger.info(
                        f"Monitor phase: {remaining}s until next scan, "
                        f"{len(self.watchlist)} positions watched, "
                        f"{critical_count} in critical zone"
                    )

            except requests.exceptions.RequestException as e:
                logger.error(f"Monitor phase network error: {type(e).__name__}")
            except (KeyError, ValueError, TypeError) as e:
                logger.error(f"Monitor phase data error: {type(e).__name__}: {e}")

            time.sleep(self.poll_interval)

        logger.info("MONITOR PHASE COMPLETE")

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

        On startup: immediate scan based on current time.
        """
        now = datetime.now(timezone.utc)
        now_est = now.astimezone(EST)
        logger.info(f"Mode: SCHEDULED (time-based)")
        logger.info(f"Current time: {now_est.strftime('%H:%M EST')}")
        logger.info(f"Comprehensive scan at: {COMPREHENSIVE_SCAN_HOUR:02d}:{COMPREHENSIVE_SCAN_MINUTE:02d} EST")

        # Determine startup scan mode based on current time
        startup_mode = self._get_scan_mode_for_time(now)
        is_startup_baseline = (startup_mode == "comprehensive")

        # If no baseline exists yet, treat first scan as baseline
        if not self.baseline_position_keys:
            is_startup_baseline = True
            logger.info("No baseline exists, first scan will establish baseline")

        # Send startup notification
        self.alerts.send_service_status(
            "started",
            f"Scheduled mode | Startup: {startup_mode}" +
            (" (baseline)" if is_startup_baseline else "")
        )

        # Run immediate startup scan
        logger.info(f"Startup scan: mode={startup_mode}, baseline={is_startup_baseline}")
        total, new_count = self.run_scan_phase(mode=startup_mode, is_baseline=is_startup_baseline)

        if not self.running:
            return

        if total == 0:
            logger.warning("No positions from startup scan, waiting 60s before retry...")
            time.sleep(60)

        # Main scheduled loop
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
