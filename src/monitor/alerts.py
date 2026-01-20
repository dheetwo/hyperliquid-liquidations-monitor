"""
Telegram Alerts
===============

Telegram notification system for the liquidation monitor service.

Alert types:
- New position alerts: Sent when new high-priority positions are detected during scan phase
- Proximity alerts: Sent when watched positions approach liquidation threshold
"""

import logging
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, TYPE_CHECKING
from dataclasses import dataclass, field

# Timezone for alert timestamps
EASTERN_TZ = ZoneInfo("America/New_York")

if TYPE_CHECKING:
    from .service import WatchedPosition

logger = logging.getLogger(__name__)

# Rate limiting constants
MIN_ALERT_INTERVAL_SECONDS = 300  # 5 minutes between alerts for same position
MIN_MESSAGE_INTERVAL_SECONDS = 1  # 1 second between any messages (Telegram limit: 30/sec)
MAX_ALERTS_PER_MINUTE = 20  # Global rate limit


@dataclass
class AlertConfig:
    """Configuration for alert sending."""
    bot_token: str
    chat_id: str
    dry_run: bool = False
    max_message_length: int = 4000
    # Rate limiting settings
    min_alert_interval: int = MIN_ALERT_INTERVAL_SECONDS
    min_message_interval: float = MIN_MESSAGE_INTERVAL_SECONDS


class TelegramAlerts:
    """
    Telegram alert sender for liquidation monitoring.

    Handles formatting and sending alerts for:
    - New positions detected during scan phase
    - Positions approaching liquidation during monitor phase

    Includes rate limiting to prevent Telegram API abuse.
    """

    def __init__(self, config: AlertConfig):
        """
        Initialize Telegram alerts.

        Args:
            config: AlertConfig with bot token, chat ID, and settings
        """
        self.config = config
        self._validate()

        # Rate limiting state
        self._last_message_time: float = 0
        self._position_alert_times: Dict[str, float] = {}  # position_key -> last alert time
        self._alerts_this_minute: List[float] = []  # timestamps of recent alerts

    def _validate(self):
        """Validate configuration."""
        if not self.config.dry_run:
            if not self.config.bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN is required (or use --dry-run)")
            if not self.config.chat_id:
                raise ValueError("TELEGRAM_CHAT_ID is required (or use --dry-run)")

    def _check_rate_limit(self) -> bool:
        """
        Check if we're within rate limits.

        Returns:
            True if we can send, False if rate limited
        """
        now = time.time()

        # Clean up old timestamps (older than 1 minute)
        self._alerts_this_minute = [t for t in self._alerts_this_minute if now - t < 60]

        # Check global rate limit
        if len(self._alerts_this_minute) >= MAX_ALERTS_PER_MINUTE:
            logger.warning(f"Rate limited: {len(self._alerts_this_minute)} alerts in last minute")
            return False

        return True

    def _enforce_message_interval(self):
        """Enforce minimum interval between messages."""
        now = time.time()
        elapsed = now - self._last_message_time

        if elapsed < self.config.min_message_interval:
            sleep_time = self.config.min_message_interval - elapsed
            time.sleep(sleep_time)

    def _can_alert_position(self, position_key: str) -> bool:
        """
        Check if we can send an alert for a specific position.

        Args:
            position_key: Unique position identifier

        Returns:
            True if alert allowed, False if in cooldown
        """
        now = time.time()
        last_alert = self._position_alert_times.get(position_key, 0)

        if now - last_alert < self.config.min_alert_interval:
            remaining = int(self.config.min_alert_interval - (now - last_alert))
            logger.debug(f"Position {position_key[:20]} in cooldown ({remaining}s remaining)")
            return False

        return True

    def _record_position_alert(self, position_key: str):
        """Record that we sent an alert for a position."""
        self._position_alert_times[position_key] = time.time()

    def _send_message(
        self,
        text: str,
        skip_rate_limit: bool = False,
        reply_to_message_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Send a message via Telegram Bot API.

        Args:
            text: Message text (HTML formatted)
            skip_rate_limit: If True, skip rate limit check (for critical alerts)
            reply_to_message_id: If set, reply to this message ID

        Returns:
            message_id if successful, None otherwise
        """
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would send Telegram message:\n{text}")
            print(f"\n{'='*60}")
            print("[DRY RUN] Telegram Alert:")
            if reply_to_message_id:
                print(f"(Reply to message {reply_to_message_id})")
            print("="*60)
            print(text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
            print("="*60 + "\n")
            # Return fake message_id for dry run
            return 999999

        # Check rate limits
        if not skip_rate_limit and not self._check_rate_limit():
            logger.warning("Message dropped due to rate limiting")
            return None

        # Enforce minimum interval between messages
        self._enforce_message_interval()

        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"

        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            response = requests.post(url, json=payload, timeout=10)

            response.raise_for_status()

            # Record successful send for rate limiting
            now = time.time()
            self._last_message_time = now
            self._alerts_this_minute.append(now)

            # Extract message_id from response
            result = response.json()
            message_id = result.get("result", {}).get("message_id")

            logger.info(f"Telegram alert sent successfully (message_id: {message_id})")
            return message_id

        except requests.exceptions.Timeout:
            logger.error("Telegram request timed out")
            return None
        except requests.exceptions.HTTPError as e:
            # Log status code without exposing token in URL
            status_code = e.response.status_code if e.response is not None else "unknown"
            logger.error(f"Telegram HTTP error: {status_code}")
            if status_code == 429:
                logger.warning("Telegram rate limit hit (429) - backing off")
            return None
        except requests.exceptions.ConnectionError:
            logger.error("Telegram connection error - network issue")
            return None
        except requests.exceptions.RequestException:
            # Generic request error - don't log exception details which may contain URL/token
            logger.error("Telegram request failed")
            return None
        except Exception:
            # Catch-all - don't log exception details to avoid token exposure
            logger.error("Unexpected error sending Telegram message")
            return None

    def _truncate_message(self, text: str) -> str:
        """Truncate message to Telegram's character limit."""
        if len(text) > self.config.max_message_length:
            return text[:self.config.max_message_length - 20] + "\n... (truncated)"
        return text

    def send_scan_summary_alert(
        self,
        watchlist: dict,
        scan_mode: str,
        is_baseline: bool,
        scan_time: datetime = None
    ) -> Optional[int]:
        """
        Send summary of scan results showing top positions in watchlist.
        Uses same format as send_new_positions_alert for consistency.

        Args:
            watchlist: Dict of position_key -> WatchedPosition
            scan_mode: Scan mode used ("comprehensive", "normal", "high-priority")
            is_baseline: Whether this was a baseline scan
            scan_time: Timestamp of the scan (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if scan_time is None:
            scan_time = datetime.now(timezone.utc)

        scan_time_et = scan_time.astimezone(EASTERN_TZ)

        # Sort positions by distance (closest to liquidation first)
        positions = sorted(watchlist.values(), key=lambda p: p.last_distance_pct)

        # Build header
        lines = [
            f"SCAN COMPLETE - {scan_mode.upper()}" + (" (BASELINE)" if is_baseline else ""),
            "",
            f"Watching {len(positions)} positions",
            ""
        ]

        if not positions:
            lines.append("No positions in watchlist")
            lines.append("")
            lines.append(f"{scan_time_et.strftime('%H:%M:%S %Z')}")
            return self._send_message("\n".join(lines))

        # Show positions in same format as NEW LIQUIDATION TARGETS
        # Limit to 15 positions to fit in message
        display_positions = positions[:15]

        # Pre-compute formatted values to find max widths
        formatted = []
        for pos in display_positions:
            side_str = "L" if pos.side == "Long" else "S"
            margin_type = "Iso" if pos.is_isolated else "Cross"
            if pos.position_value >= 1_000_000:
                value_str = f"${pos.position_value / 1_000_000:.1f}M"
            else:
                value_str = f"${pos.position_value / 1_000:.0f}K"
            dist_str = f"{pos.last_distance_pct:.2f}%"
            formatted.append((pos, pos.token, side_str, value_str, margin_type, dist_str))

        # Find max width for each column
        max_token = max(len(f[1]) for f in formatted)
        max_side = max(len(f[2]) for f in formatted)
        max_value = max(len(f[3]) for f in formatted)
        max_margin = max(len(f[4]) for f in formatted)
        max_dist = max(len(f[5]) for f in formatted)

        for pos, token, side_str, value_str, margin_type, dist_str in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{pos.address}"

            # Truncate address with equal chars on each side and ellipsis in middle
            addr = pos.address
            addr_display = f"{addr[:18]}...{addr[-18:]}"

            row = (
                f"{token:<{max_token}} | "
                f"{side_str:<{max_side}} | "
                f"{value_str:>{max_value}} | "
                f"{margin_type:<{max_margin}} | "
                f"{dist_str:<{max_dist}}"
            )
            lines.append(f"<code>{row}</code>")
            lines.append(f"<a href=\"{hypurrscan_url}\">{addr_display}</a>")
            lines.append("")

        if len(positions) > 15:
            lines.append(f"... and {len(positions) - 15} more positions")
            lines.append("")

        lines.append(f"{scan_time_et.strftime('%H:%M:%S %Z')}")

        message = "\n".join(lines)
        message = self._truncate_message(message)

        return self._send_message(message)

    def send_new_positions_alert(
        self,
        positions: List["WatchedPosition"],
        scan_time: datetime = None
    ) -> Optional[int]:
        """
        Send alert for newly detected high-priority positions.

        Args:
            positions: List of new WatchedPosition objects
            scan_time: Timestamp of the scan (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if not positions:
            logger.debug("No new positions to alert")
            return None

        if scan_time is None:
            scan_time = datetime.now(timezone.utc)

        # Convert to Eastern Time
        scan_time_et = scan_time.astimezone(EASTERN_TZ)

        # Build message
        lines = [
            "NEW LIQUIDATION TARGETS DETECTED",
            ""
        ]

        # Pre-compute formatted values to find max widths
        display_positions = positions[:10]
        formatted = []
        for pos in display_positions:
            side_str = "L" if pos.side == "Long" else "S"
            margin_type = "Iso" if pos.is_isolated else "Cross"
            if pos.position_value >= 1_000_000:
                value_str = f"${pos.position_value / 1_000_000:.1f}M"
            else:
                value_str = f"${pos.position_value / 1_000:.0f}K"
            dist_str = f"{pos.last_distance_pct:.1f}%"
            formatted.append((pos, pos.token, side_str, value_str, margin_type, dist_str))

        # Find max width for each column
        max_token = max(len(f[1]) for f in formatted)
        max_side = max(len(f[2]) for f in formatted)
        max_value = max(len(f[3]) for f in formatted)
        max_margin = max(len(f[4]) for f in formatted)
        max_dist = max(len(f[5]) for f in formatted)

        for pos, token, side_str, value_str, margin_type, dist_str in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{pos.address}"

            # Truncate address with equal chars on each side and ellipsis in middle
            addr = pos.address
            addr_display = f"{addr[:18]}...{addr[-18:]}"

            row = (
                f"{token:<{max_token}} | "
                f"{side_str:<{max_side}} | "
                f"{value_str:>{max_value}} | "
                f"{margin_type:<{max_margin}} | "
                f"{dist_str:<{max_dist}}"
            )
            lines.append(f"<code>{row}</code>")
            lines.append(f"<a href=\"{hypurrscan_url}\">{addr_display}</a>")
            lines.append("")

        if len(positions) > 10:
            lines.append(f"... and {len(positions) - 10} more positions")
            lines.append("")

        lines.append(f"{scan_time_et.strftime('%H:%M:%S %Z')}")

        message = "\n".join(lines)
        message = self._truncate_message(message)

        return self._send_message(message)

    def send_proximity_alert(
        self,
        position: "WatchedPosition",
        previous_distance: float,
        current_price: float,
        alert_time: datetime = None
    ) -> bool:
        """
        Send alert when a position approaches liquidation threshold.

        Rate limited per position to avoid spam for volatile markets.
        If the position has an alert_message_id, replies to that message.

        Args:
            position: WatchedPosition that triggered the alert
            previous_distance: Previous distance percentage (for comparison)
            current_price: Current mark price
            alert_time: Timestamp of the alert (default: now)

        Returns:
            True if sent successfully, False if rate limited or failed
        """
        # Check position-specific rate limit
        position_key = position.position_key
        if not self._can_alert_position(position_key):
            logger.debug(f"Proximity alert for {position.token} skipped (cooldown)")
            return False

        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        # Convert to Eastern Time
        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if position.side == "Long" else "S"
        margin_type = "Iso" if position.is_isolated else "Cross"

        # Format position value
        if position.position_value >= 1_000_000:
            value_str = f"${position.position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position.position_value / 1_000:.0f}K"

        # Format prices
        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        # Hypurrscan link
        hypurrscan_url = f"https://hypurrscan.io/address/{position.address}"
        addr = position.address
        addr_display = f"{addr[:18]}...{addr[-18:]}"

        lines = [
            "APPROACHING LIQUIDATION",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: {previous_distance:.2f}% -> <b>{position.last_distance_pct:.2f}%</b>",
            f"Liq. Price: {format_price(position.liq_price)} | Current Price: {format_price(current_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)
        message_id = self._send_message(
            message,
            reply_to_message_id=position.alert_message_id
        )

        # Record alert time if sent successfully
        if message_id is not None:
            self._record_position_alert(position_key)
            return message_id

        return None

    def send_critical_alert(
        self,
        position: "WatchedPosition",
        previous_distance: float,
        current_price: float,
        reply_to_message_id: int = None,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send CRITICAL alert when position crosses below 0.1% threshold.

        Args:
            position: WatchedPosition that triggered the alert
            previous_distance: Previous distance percentage
            current_price: Current mark price
            reply_to_message_id: Message to reply to (for threading)
            alert_time: Timestamp of the alert (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if position.side == "Long" else "S"
        margin_type = "Iso" if position.is_isolated else "Cross"

        if position.position_value >= 1_000_000:
            value_str = f"${position.position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position.position_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{position.address}"
        addr = position.address
        addr_display = f"{addr[:18]}...{addr[-18:]}"

        lines = [
            "🚨 IMMINENT LIQUIDATION",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: {previous_distance:.3f}% -> <b>{position.last_distance_pct:.3f}%</b>",
            f"Liq. Price: {format_price(position.liq_price)} | Current Price: {format_price(current_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)

        # Reply to provided message or fall back to original alert
        reply_to = reply_to_message_id or position.alert_message_id
        return self._send_message(message, reply_to_message_id=reply_to)

    def send_recovery_alert(
        self,
        position: "WatchedPosition",
        previous_distance: float,
        current_price: float,
        reply_to_message_id: int = None,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send RECOVERY alert when position goes from <0.2% to >0.5%.

        Args:
            position: WatchedPosition that recovered
            previous_distance: Previous distance percentage (was <0.2%)
            current_price: Current mark price
            reply_to_message_id: Message to reply to (for threading)
            alert_time: Timestamp of the alert (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if position.side == "Long" else "S"
        margin_type = "Iso" if position.is_isolated else "Cross"

        if position.position_value >= 1_000_000:
            value_str = f"${position.position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position.position_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{position.address}"
        addr = position.address
        addr_display = f"{addr[:18]}...{addr[-18:]}"

        lines = [
            "✅ POSITION RECOVERED",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: {previous_distance:.3f}% -> <b>{position.last_distance_pct:.2f}%</b>",
            f"Liq. Price: {format_price(position.liq_price)} | Current Price: {format_price(current_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)

        # Reply to provided message or fall back to original alert
        reply_to = reply_to_message_id or position.alert_message_id
        return self._send_message(message, reply_to_message_id=reply_to)

    def send_startup_phase_alert(
        self,
        phase: int,
        total_phases: int,
        phase_name: str,
        description: str,
        timestamp: datetime = None
    ) -> Optional[int]:
        """
        Send startup phase progress notification.

        Args:
            phase: Current phase number (1-indexed)
            total_phases: Total number of phases
            phase_name: Short name of the phase (e.g., "High-priority")
            description: What this phase covers
            timestamp: Timestamp (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        timestamp_et = timestamp.astimezone(EASTERN_TZ)

        # Progress bar visualization
        filled = "█" * phase
        empty = "░" * (total_phases - phase)
        progress_bar = f"[{filled}{empty}]"

        lines = [
            f"<b>STARTUP SCAN {phase}/{total_phases}</b>",
            "",
            f"{progress_bar} {phase_name}",
            "",
            description,
            "",
            f"{timestamp_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_cohort_start(
        self,
        cohort: str,
        phase_name: str = None
    ) -> Optional[int]:
        """
        Send notification when starting to scan a new cohort.

        Args:
            cohort: Cohort name being scanned
            phase_name: Optional phase name for context

        Returns:
            message_id if sent successfully, None otherwise
        """
        header = f"SCANNING: {phase_name}" if phase_name else "SCANNING"

        lines = [
            f"{header}",
            f"Cohort: {cohort}",
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_phase_start_alert(
        self,
        phase_name: str,
        address_count: int,
        exchange_count: int,
        estimated_seconds: float,
        cohort_breakdown: dict = None,
        timestamp: datetime = None
    ) -> Optional[int]:
        """
        Send notification when a pipeline phase is starting.
        Only includes estimated time if > 60 seconds.

        Args:
            phase_name: Name of the phase (e.g., "Position Scan")
            address_count: Number of addresses to scan
            exchange_count: Number of exchanges per address
            estimated_seconds: Estimated runtime in seconds
            cohort_breakdown: Optional dict of cohort -> count
            timestamp: Timestamp (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        timestamp_et = timestamp.astimezone(EASTERN_TZ)
        total_requests = address_count * exchange_count

        lines = [
            f"<b>🔄 {phase_name} Starting</b>",
            "",
            f"📊 Addresses: {address_count:,}",
            f"🔄 Exchanges: {exchange_count}",
            f"📡 API calls: {total_requests:,}",
        ]

        # Only show estimated time if > 60 seconds
        if estimated_seconds > 60:
            minutes = estimated_seconds / 60
            lines.append("")
            lines.append(f"<b>⏱ Estimated wait: {minutes:.1f} min</b>")

        # Add cohort breakdown if provided
        if cohort_breakdown:
            lines.append("")
            lines.append("Cohorts:")
            for cohort, count in cohort_breakdown.items():
                if count > 0:
                    lines.append(f"  • {cohort}: {count:,}")

        lines.append("")
        lines.append(f"<i>{timestamp_et.strftime('%H:%M:%S %Z')}</i>")

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_phase_complete_alert(
        self,
        phase_name: str,
        results_count: int,
        elapsed_seconds: float,
        extra_info: str = None,
        timestamp: datetime = None
    ) -> Optional[int]:
        """
        Send notification when a pipeline phase completes.

        Args:
            phase_name: Name of the phase
            results_count: Number of results (positions, etc.)
            elapsed_seconds: Actual runtime in seconds
            extra_info: Optional additional info line
            timestamp: Timestamp (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        timestamp_et = timestamp.astimezone(EASTERN_TZ)

        lines = [
            f"<b>✅ {phase_name} Complete</b>",
            "",
            f"📊 Results: {results_count:,}",
            f"⏱ Time: {elapsed_seconds:.1f}s",
        ]

        if extra_info:
            lines.append(extra_info)

        lines.append("")
        lines.append(f"<i>{timestamp_et.strftime('%H:%M:%S %Z')}</i>")

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_pipeline_complete_alert(
        self,
        filter_stats: dict,
        watchlist_size: int,
        watchlist_isolated: int,
        watchlist_cross: int,
        new_positions: list,
        timestamp: datetime = None
    ) -> Optional[int]:
        """
        Send combined Phase 3+4 pipeline completion alert.

        Args:
            filter_stats: Stats from Phase 3 filter (filtered_count, within_3pct, within_10pct)
            watchlist_size: Total positions in watchlist
            watchlist_isolated: Number of isolated positions in watchlist
            watchlist_cross: Number of cross positions in watchlist
            new_positions: List of WatchedPosition objects that pass alert thresholds
            timestamp: Timestamp (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        timestamp_et = timestamp.astimezone(EASTERN_TZ)

        lines = [
            "<b>Scan Pipeline Complete</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "<b>Phase 3: Liquidation Filter</b>",
            f"{filter_stats.get('filtered_count', 0):,} positions with liq prices",
            f"≤3%: {filter_stats.get('within_3pct', 0)} | ≤10%: {filter_stats.get('within_10pct', 0)}",
            "",
            "<b>Phase 4: Watchlist</b>",
        ]

        # Add position details for those passing alert thresholds
        if new_positions:
            lines.append("")
            for pos in new_positions:
                # Format: Token Side | $Value | iso/cross | distance%
                margin_type = "iso" if pos.is_isolated else "cross"
                value_str = self._format_value(pos.position_value)
                lines.append(f"{pos.token} {pos.side} | {value_str} | {margin_type} | {pos.last_distance_pct:.2f}%")

                # Truncated address with hyperlink
                addr_short = f"{pos.address[:6]}...{pos.address[-4:]}"
                hypurrscan_url = f"https://hypurrscan.io/address/{pos.address}"
                lines.append(f'<a href="{hypurrscan_url}">{addr_short}</a>')
                lines.append("")

        # Show remaining watchlist count
        remaining = watchlist_size - len(new_positions)
        if remaining > 0:
            lines.append(f"+{remaining} more on watchlist")
        elif watchlist_size == 0:
            lines.append("No positions on watchlist")

        lines.append("")
        lines.append(f"{timestamp_et.strftime('%Y-%m-%d %H:%M:%S')}")

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def _format_value(self, value: float) -> str:
        """Format a dollar value for display (e.g., $16.3M, $1.27M, $500K)."""
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.1f}B"
        elif value >= 1_000_000:
            return f"${value / 1_000_000:.1f}M"
        elif value >= 1_000:
            return f"${value / 1_000:.0f}K"
        else:
            return f"${value:.0f}"

    def send_service_status(
        self,
        status: str,
        details: str = "",
        timestamp: datetime = None
    ) -> bool:
        """
        Send service status notification.

        These are critical operational alerts and skip rate limiting.

        Args:
            status: Status type ("started", "stopped", "error", "scan_complete")
            details: Additional details
            timestamp: Timestamp (default: now)

        Returns:
            True if sent successfully
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        # Convert to Eastern Time
        timestamp_et = timestamp.astimezone(EASTERN_TZ)

        status_emoji = {
            "started": "Monitor service started",
            "stopped": "Monitor service stopped",
            "error": "Monitor service error",
            "scan_complete": "Scan phase complete",
        }.get(status, f"Status: {status}")

        lines = [
            f"<b>{status_emoji}</b>",
        ]

        if details:
            lines.append("")
            lines.append(details)

        lines.append("")
        lines.append(f"Time: {timestamp_et.strftime('%H:%M:%S %Z')}")

        message = "\n".join(lines)
        # Service status alerts skip rate limiting (critical operational info)
        return self._send_message(message, skip_rate_limit=True) is not None


def send_test_alert(
    bot_token: str = None,
    chat_id: str = None,
    dry_run: bool = False
) -> bool:
    """
    Send a test alert to verify Telegram configuration.

    Args:
        bot_token: Telegram bot token (default: from env)
        chat_id: Telegram chat ID (default: from env)
        dry_run: If True, print message instead of sending

    Returns:
        True if successful
    """
    import os

    if bot_token is None:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if chat_id is None:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    config = AlertConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        dry_run=dry_run
    )

    alerts = TelegramAlerts(config)
    return alerts.send_service_status(
        "started",
        "Test alert - Liquidation monitor configuration verified."
    )
