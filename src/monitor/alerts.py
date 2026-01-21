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
            cohort_str = pos.cohort_display if hasattr(pos, 'cohort_display') else ""
            formatted.append((pos, pos.token, side_str, value_str, margin_type, dist_str, cohort_str))

        # Find max width for each column
        max_token = max(len(f[1]) for f in formatted)
        max_side = max(len(f[2]) for f in formatted)
        max_value = max(len(f[3]) for f in formatted)
        max_margin = max(len(f[4]) for f in formatted)
        max_dist = max(len(f[5]) for f in formatted)

        for pos, token, side_str, value_str, margin_type, dist_str, cohort_str in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{pos.address}"

            # Truncate address with equal chars on each side and ellipsis in middle
            addr = pos.address
            addr_display = f"{addr[:6]}...{addr[-4:]}"

            row = (
                f"{token:<{max_token}} | "
                f"{side_str:<{max_side}} | "
                f"{value_str:>{max_value}} | "
                f"{margin_type:<{max_margin}} | "
                f"{dist_str:<{max_dist}}"
            )
            lines.append(f"<code>{row}</code>")
            # Show address link with cohort tags after
            if cohort_str:
                lines.append(f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({cohort_str})")
            else:
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
            cohort_str = pos.cohort_display if hasattr(pos, 'cohort_display') else ""
            formatted.append((pos, pos.token, side_str, value_str, margin_type, dist_str, cohort_str))

        # Find max width for each column
        max_token = max(len(f[1]) for f in formatted)
        max_side = max(len(f[2]) for f in formatted)
        max_value = max(len(f[3]) for f in formatted)
        max_margin = max(len(f[4]) for f in formatted)
        max_dist = max(len(f[5]) for f in formatted)

        for pos, token, side_str, value_str, margin_type, dist_str, cohort_str in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{pos.address}"

            # Truncate address with equal chars on each side and ellipsis in middle
            addr = pos.address
            addr_display = f"{addr[:6]}...{addr[-4:]}"

            row = (
                f"{token:<{max_token}} | "
                f"{side_str:<{max_side}} | "
                f"{value_str:>{max_value}} | "
                f"{margin_type:<{max_margin}} | "
                f"{dist_str:<{max_dist}}"
            )
            lines.append(f"<code>{row}</code>")
            # Show address link with cohort tags after
            if cohort_str:
                lines.append(f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({cohort_str})")
            else:
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
        cohort_str = position.cohort_display if hasattr(position, 'cohort_display') else ""

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
        addr_display = f"{addr[:6]}...{addr[-4:]}"

        # Build address line with cohort tags after
        if cohort_str:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({cohort_str})"
        else:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a>"

        lines = [
            "APPROACHING LIQUIDATION",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            addr_line,
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
        cohort_str = position.cohort_display if hasattr(position, 'cohort_display') else ""

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
        addr_display = f"{addr[:6]}...{addr[-4:]}"

        # Build address line with cohort tags after
        if cohort_str:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({cohort_str})"
        else:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a>"

        lines = [
            "🚨 IMMINENT LIQUIDATION",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            addr_line,
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
        cohort_str = position.cohort_display if hasattr(position, 'cohort_display') else ""

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
        addr_display = f"{addr[:6]}...{addr[-4:]}"

        # Build address line with cohort tags after
        if cohort_str:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({cohort_str})"
        else:
            addr_line = f"<a href=\"{hypurrscan_url}\">{addr_display}</a>"

        lines = [
            "✅ POSITION RECOVERED",
            "",
            f"{position.token} | {side_str} | {value_str} | {margin_type}",
            addr_line,
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

    def send_collateral_added_alert(
        self,
        position: "WatchedPosition",
        old_liq_price: float,
        new_liq_price: float,
        old_distance: float,
        new_distance: float,
        reply_to_message_id: int = None,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send alert when user adds collateral to a position.

        Detected by liquidation price moving away from current price
        while position size remains unchanged.

        Args:
            position: WatchedPosition that had collateral added
            old_liq_price: Previous liquidation price
            new_liq_price: New liquidation price (farther from current)
            old_distance: Previous distance percentage
            new_distance: New distance percentage (should be larger)
            reply_to_message_id: Message to reply to (for threading)
            alert_time: Timestamp of the alert (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        # Build token display with exchange prefix if not main
        exchange = getattr(position, 'exchange', 'main')
        if exchange and exchange != 'main':
            token_display = f"{exchange}:{position.token}"
        else:
            token_display = position.token

        side_str = "L" if position.side == "Long" else "S"
        margin_type = "Iso" if position.is_isolated else "Cross"

        if position.position_value >= 1_000_000:
            value_str = f"${position.position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position.position_value / 1_000:.0f}K"

        addr = position.address
        hypurrscan_url = f"https://hypurrscan.io/address/{addr}"

        lines = [
            f"💰 COLLATERAL ADDED on {token_display}: Liq Distance: {old_distance:.2f}% → {new_distance:.2f}%",
            f"{token_display} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr[:6]}...{addr[-4:]}</a>",
        ]

        message = "\n".join(lines)

        reply_to = reply_to_message_id or position.alert_message_id
        return self._send_message(message, reply_to_message_id=reply_to)

    def send_liquidation_alert(
        self,
        position: "WatchedPosition",
        liquidation_type: str,
        old_value: float,
        new_value: float = 0,
        last_distance: float = None,
        current_price: float = None,
        reply_to_message_id: int = None,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send alert when a position is partially or fully liquidated.

        Args:
            position: WatchedPosition that was liquidated
            liquidation_type: "full" or "partial"
            old_value: Previous position value
            new_value: New position value (0 for full liquidation)
            last_distance: Last known distance percentage before liquidation
            current_price: Current mark price at time of liquidation
            reply_to_message_id: Message to reply to (for threading)
            alert_time: Timestamp of the alert (default: now)

        Returns:
            message_id if sent successfully, None otherwise
        """
        def format_value(v: float) -> str:
            # Round down to nearest 10k
            v = (v // 10_000) * 10_000
            if v >= 1_000_000:
                return f"${v / 1_000_000:.1f}M"
            else:
                return f"${v / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{position.address}"
        addr_display = f"{position.address[:6]}...{position.address[-4:]}"

        # Build token display with exchange prefix if not main
        exchange = getattr(position, 'exchange', 'main')
        if exchange and exchange != 'main':
            token_display = f"{exchange}:{position.token}"
        else:
            token_display = position.token

        liq_str = format_price(position.liq_price) if position.liq_price else ""
        side_str = "L" if position.side == "Long" else "S"
        margin_type = "Iso" if position.is_isolated else "Cross"

        if liquidation_type == "full":
            lines = [
                f"🔴 {format_value(old_value)} FULL LIQUIDATION on {token_display} at {liq_str}",
                f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            ]
        else:
            liquidated_amount = old_value - new_value
            lines = [
                f"⚠️ {format_value(liquidated_amount)} PARTIAL LIQUIDATION on {token_display} at {liq_str}",
                f"{token_display} | {side_str} | → {format_value(new_value)} | {margin_type}",
                f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            ]

        message = "\n".join(lines)

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

    def send_proximity_alert_simple(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send proximity alert with simple parameters (for cache-based monitoring).

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: Current distance to liquidation
            liq_price: Liquidation price
            mark_price: Current mark price
            position_value: Position value in USD
            is_isolated: Whether isolated margin
            alert_time: Alert timestamp

        Returns:
            message_id if sent successfully, None otherwise
        """
        position_key = f"{address}:{token}:main:{side}"
        if not self._can_alert_position(position_key):
            logger.debug(f"Proximity alert for {token} skipped (cooldown)")
            return None

        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        if position_value >= 1_000_000:
            value_str = f"${position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{address}"
        addr_display = f"{address[:6]}...{address[-4:]}"

        lines = [
            "APPROACHING LIQUIDATION",
            "",
            f"{token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: <b>{distance_pct:.2f}%</b>",
            f"Liq. Price: {format_price(liq_price)} | Current: {format_price(mark_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)
        message_id = self._send_message(message)

        if message_id is not None:
            self._record_position_alert(position_key)

        return message_id

    def send_critical_alert_simple(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send critical alert with simple parameters (for cache-based monitoring).

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: Current distance to liquidation
            liq_price: Liquidation price
            mark_price: Current mark price
            position_value: Position value in USD
            is_isolated: Whether isolated margin
            alert_time: Alert timestamp

        Returns:
            message_id if sent successfully, None otherwise
        """
        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        if position_value >= 1_000_000:
            value_str = f"${position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{address}"
        addr_display = f"{address[:6]}...{address[-4:]}"

        lines = [
            "🚨 IMMINENT LIQUIDATION",
            "",
            f"{token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: <b>{distance_pct:.3f}%</b>",
            f"Liq. Price: {format_price(liq_price)} | Current: {format_price(mark_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_recovery_alert_simple(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send recovery alert with simple parameters (for cache-based monitoring).

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: Current distance to liquidation
            liq_price: Liquidation price
            mark_price: Current mark price
            position_value: Position value in USD
            is_isolated: Whether isolated margin
            alert_time: Alert timestamp

        Returns:
            message_id if sent successfully, None otherwise
        """
        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        if position_value >= 1_000_000:
            value_str = f"${position_value / 1_000_000:.1f}M"
        else:
            value_str = f"${position_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{address}"
        addr_display = f"{address[:6]}...{address[-4:]}"

        lines = [
            "✅ POSITION RECOVERED",
            "",
            f"{token} | {side_str} | {value_str} | {margin_type}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
            "",
            f"Liquidation Distance: <b>{distance_pct:.2f}%</b>",
            f"Liq. Price: {format_price(liq_price)} | Current: {format_price(mark_price)}",
            "",
            f"{alert_time_et.strftime('%H:%M:%S %Z')}",
        ]

        message = "\n".join(lines)
        return self._send_message(message)

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

        status_text = {
            "started": "Monitor service started",
            "stopped": "Monitor service stopped",
            "error": "Monitor service error",
            "scan_complete": "Scan phase complete",
        }.get(status, f"Status: {status}")

        time_str = timestamp_et.strftime('%H:%M:%S %Z')
        lines = [
            f"<b>{status_text} at {time_str}</b>",
        ]

        if details:
            lines.append("")
            lines.append(details)

        message = "\n".join(lines)
        # Service status alerts skip rate limiting (critical operational info)
        return self._send_message(message, skip_rate_limit=True) is not None


def send_daily_summary(
    position_cache: "PositionCache",
    alerts: TelegramAlerts,
    discovery_scheduler: "DiscoveryScheduler"
) -> Optional[int]:
    """
    Send daily watchlist summary to Telegram.

    Groups positions by refresh tier and shows overview of monitored positions.

    Args:
        position_cache: PositionCache with current positions
        alerts: TelegramAlerts instance
        discovery_scheduler: DiscoveryScheduler for discovery interval info

    Returns:
        message_id if sent successfully, None otherwise
    """
    from collections import defaultdict
    from .cache import PositionCache, DiscoveryScheduler

    now = datetime.now(EASTERN_TZ)

    # Group positions by tier
    positions_by_tier = {
        'critical': [],
        'high': [],
        'normal': [],
    }

    for pos in position_cache.positions.values():
        positions_by_tier[pos.refresh_tier].append(pos)

    # Sort each tier by distance
    for tier in positions_by_tier:
        positions_by_tier[tier].sort(key=lambda p: p.distance_pct or float('inf'))

    critical = positions_by_tier['critical']
    high = positions_by_tier['high']
    normal = positions_by_tier['normal']

    def format_token_with_exchange(token: str, exchange: str) -> str:
        """Add exchange prefix for sub-exchanges."""
        if exchange != "main":
            return f"{exchange}:{token}"
        return token

    # Build message - header without timestamp (moved to end)
    lines = [
        "LIQUIDATION WATCHLIST SUMMARY",
        "",
    ]

    # Critical section - single line per position
    if critical:
        lines.append("🔴 CRITICAL ZONE (≤0.125%)")
        display_critical = critical[:10]

        # Pre-compute formatted values for column alignment
        formatted = []
        for pos in display_critical:
            token_display = format_token_with_exchange(pos.token, pos.exchange)
            value_str = f"${pos.position_value / 1_000_000:.1f}M" if pos.position_value >= 1_000_000 else f"${pos.position_value / 1_000:.0f}K"
            side_char = "L" if pos.side == "Long" else "S"
            margin_type = "Iso" if pos.leverage_type == "Isolated" else "Cross"
            dist_str = f"{pos.distance_pct:.3f}%"
            addr_short = f"{pos.address[:6]}...{pos.address[-4:]}"
            formatted.append((token_display, side_char, value_str, margin_type, dist_str, addr_short, pos.address))

        # Find max widths for each column
        max_token = max(len(f[0]) for f in formatted)
        max_value = max(len(f[2]) for f in formatted)
        max_margin = max(len(f[3]) for f in formatted)
        max_dist = max(len(f[4]) for f in formatted)

        for token_display, side_char, value_str, margin_type, dist_str, addr_short, address in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{address}"
            row = f"{token_display:<{max_token}} | {side_char} | {value_str:>{max_value}} | {margin_type:<{max_margin}} | {dist_str:>{max_dist}}"
            lines.append(f"<a href=\"{hypurrscan_url}\">{addr_short}</a> <code>{row}</code>")
        if len(critical) > 10:
            lines.append(f"... and {len(critical) - 10} more")

    # High section - single line per position
    if high:
        lines.append("🟠 HIGH PRIORITY (≤0.25%)")
        display_high = high[:10]

        # Pre-compute formatted values for column alignment
        formatted = []
        for pos in display_high:
            token_display = format_token_with_exchange(pos.token, pos.exchange)
            value_str = f"${pos.position_value / 1_000_000:.1f}M" if pos.position_value >= 1_000_000 else f"${pos.position_value / 1_000:.0f}K"
            side_char = "L" if pos.side == "Long" else "S"
            margin_type = "Iso" if pos.leverage_type == "Isolated" else "Cross"
            dist_str = f"{pos.distance_pct:.3f}%"
            addr_short = f"{pos.address[:6]}...{pos.address[-4:]}"
            formatted.append((token_display, side_char, value_str, margin_type, dist_str, addr_short, pos.address))

        # Find max widths for each column
        max_token = max(len(f[0]) for f in formatted)
        max_value = max(len(f[2]) for f in formatted)
        max_margin = max(len(f[3]) for f in formatted)
        max_dist = max(len(f[4]) for f in formatted)

        for token_display, side_char, value_str, margin_type, dist_str, addr_short, address in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{address}"
            row = f"{token_display:<{max_token}} | {side_char} | {value_str:>{max_value}} | {margin_type:<{max_margin}} | {dist_str:>{max_dist}}"
            lines.append(f"<a href=\"{hypurrscan_url}\">{addr_short}</a> <code>{row}</code>")
        if len(high) > 10:
            lines.append(f"... and {len(high) - 10} more")

    # Normal section - show positions ≤3.5% with single line format
    # Filter out positions >3.5% as they're not worth actively monitoring
    normal_filtered = [p for p in normal if p.distance_pct is not None and p.distance_pct <= 3.5]

    if normal_filtered:
        lines.append("🟢 MONITORING")

        # Pre-compute formatted values for column alignment
        formatted = []
        for pos in normal_filtered:
            token_display = format_token_with_exchange(pos.token, pos.exchange)
            value_str = f"${pos.position_value / 1_000_000:.1f}M" if pos.position_value >= 1_000_000 else f"${pos.position_value / 1_000:.0f}K"
            side_char = "L" if pos.side == "Long" else "S"
            margin_type = "Iso" if pos.leverage_type == "Isolated" else "Cross"
            dist_str = f"{pos.distance_pct:.2f}%"
            addr_short = f"{pos.address[:6]}...{pos.address[-4:]}"
            formatted.append((token_display, side_char, value_str, margin_type, dist_str, addr_short, pos.address))

        # Find max widths for each column
        max_token = max(len(f[0]) for f in formatted)
        max_value = max(len(f[2]) for f in formatted)
        max_margin = max(len(f[3]) for f in formatted)
        max_dist = max(len(f[4]) for f in formatted)

        for token_display, side_char, value_str, margin_type, dist_str, addr_short, address in formatted:
            hypurrscan_url = f"https://hypurrscan.io/address/{address}"
            row = f"{token_display:<{max_token}} | {side_char} | {value_str:>{max_value}} | {margin_type:<{max_margin}} | {dist_str:>{max_dist}}"
            lines.append(f"<a href=\"{hypurrscan_url}\">{addr_short}</a> <code>{row}</code>")
        lines.append("")

    # Timestamp at end
    lines.append(f"{now.strftime('%Y-%m-%d %I:%M:%S %p')} EST")

    message = "\n".join(lines)
    return alerts._send_message(message)


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
