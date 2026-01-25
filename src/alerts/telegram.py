"""
Telegram Alerts
===============

Telegram notification system for the liquidation monitor service.

Alert types:
- Proximity alerts: Sent when watched positions approach liquidation threshold
- Critical alerts: Sent when positions are imminent to liquidation
"""

import logging
import time
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
from dataclasses import dataclass

# Timezone for alert timestamps
EASTERN_TZ = ZoneInfo("America/New_York")

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
    - Positions approaching liquidation during monitor phase
    - Critical/imminent liquidation alerts

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

    @classmethod
    def from_env(cls) -> Optional["TelegramAlerts"]:
        """
        Create TelegramAlerts from environment variables.

        Returns:
            TelegramAlerts instance if configured, None otherwise
        """
        import os

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

        if not bot_token or not chat_id:
            logger.warning(
                "Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"
            )
            return None

        config = AlertConfig(bot_token=bot_token, chat_id=chat_id)
        return cls(config)

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

    def _send_message_async(self, text: str, skip_rate_limit: bool = False):
        """
        Send message without blocking. Fire and forget.

        Spawns a background thread to send the message so the caller
        can continue immediately without waiting for Telegram response.

        Args:
            text: Message text (HTML formatted)
            skip_rate_limit: If True, skip rate limit check
        """
        def _send():
            try:
                self._send_message(text, skip_rate_limit=skip_rate_limit)
            except Exception as e:
                logger.error(f"Async send failed: {e}")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

    def _truncate_message(self, text: str) -> str:
        """Truncate message to Telegram's character limit."""
        if len(text) > self.config.max_message_length:
            return text[:self.config.max_message_length - 20] + "\n... (truncated)"
        return text

    def send_proximity_alert(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        exchange: str = "main",
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send proximity alert (APPROACHING LIQUIDATION).

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: Current distance to liquidation
            liq_price: Liquidation price
            mark_price: Current mark price
            position_value: Position value in USD
            is_isolated: Whether isolated margin
            exchange: Exchange name (default: "main")
            alert_time: Alert timestamp

        Returns:
            message_id if sent successfully, None otherwise
        """
        position_key = f"{address}:{token}:{exchange}:{side}"
        if not self._can_alert_position(position_key):
            logger.debug(f"Proximity alert for {token} skipped (cooldown)")
            return None

        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        # Build token display with exchange prefix if not main
        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"⚠️ {token_display} | {value_str} {margin_type}",
            f"<b>{distance_pct:.2f}%</b> away @ {format_price(liq_price)} | <a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        message_id = self._send_message(message)

        if message_id is not None:
            self._record_position_alert(position_key)

        return message_id

    def send_proximity_alert_async(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        exchange: str = "main",
    ):
        """
        Send proximity alert without blocking.

        Same as send_proximity_alert but doesn't wait for Telegram response.
        Use this for time-critical processing where you need to continue
        immediately to the next position.
        """
        position_key = f"{address}:{token}:{exchange}:{side}"
        if not self._can_alert_position(position_key):
            logger.debug(f"Proximity alert for {token} skipped (cooldown)")
            return

        # Record alert immediately (before async send)
        self._record_position_alert(position_key)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"⚠️ {token_display} | {value_str} {margin_type}",
            f"<b>{distance_pct:.2f}%</b> away @ {format_price(liq_price)} | <a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        self._send_message_async(message)

    def send_critical_alert(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        exchange: str = "main",
        alert_time: datetime = None
    ) -> Optional[int]:
        """
        Send critical alert (IMMINENT LIQUIDATION).

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: Current distance to liquidation
            liq_price: Liquidation price
            mark_price: Current mark price
            position_value: Position value in USD
            is_isolated: Whether isolated margin
            exchange: Exchange name (default: "main")
            alert_time: Alert timestamp

        Returns:
            message_id if sent successfully, None otherwise
        """
        if alert_time is None:
            alert_time = datetime.now(timezone.utc)

        alert_time_et = alert_time.astimezone(EASTERN_TZ)

        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        # Build token display with exchange prefix if not main
        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"🚨 {token_display} | {value_str} {margin_type}",
            f"<b>{distance_pct:.2f}%</b> away @ {format_price(liq_price)} | <a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_critical_alert_async(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        mark_price: float,
        position_value: float,
        is_isolated: bool = False,
        exchange: str = "main",
    ):
        """
        Send critical alert without blocking.

        Same as send_critical_alert but doesn't wait for Telegram response.
        Use this for time-critical processing where you need to continue
        immediately to the next position.
        """
        side_str = "L" if side == "Long" else "S"
        margin_type = "Iso" if is_isolated else "Cross"

        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"🚨 {token_display} | {value_str} {margin_type}",
            f"<b>{distance_pct:.2f}%</b> away @ {format_price(liq_price)} | <a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        self._send_message_async(message, skip_rate_limit=True)

    def send_collateral_added_alert_async(
        self,
        token: str,
        side: str,
        address: str,
        distance_pct: float,
        liq_price: float,
        position_value: float,
        previous_bucket: str,
        is_isolated: bool = False,
        exchange: str = "main",
    ):
        """
        Send collateral added alert without blocking.

        Sent when a position recovers from HIGH/CRITICAL due to collateral addition.

        Args:
            token: Token symbol
            side: "Long" or "Short"
            address: Wallet address
            distance_pct: New distance to liquidation after recovery
            liq_price: New liquidation price
            position_value: Position value in USD
            previous_bucket: Previous bucket name ("HIGH" or "CRITICAL")
            is_isolated: Whether isolated margin
            exchange: Exchange name (default: "main")
        """
        margin_type = "Iso" if is_isolated else "Cross"

        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"🟢 {token_display} | {value_str} {margin_type} +COLLATERAL",
            f"<b>{distance_pct:.2f}%</b> away @ {format_price(liq_price)} | <a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        self._send_message_async(message)

    def send_full_liquidation_alert(
        self,
        token: str,
        address: str,
        position_value: float,
        liq_price: float,
        exchange: str = "main",
    ) -> Optional[int]:
        """
        Send full liquidation alert.

        Args:
            token: Token symbol
            address: Wallet address
            position_value: Position value that was liquidated
            liq_price: Liquidation price
            exchange: Exchange name (default: "main")

        Returns:
            message_id if sent successfully, None otherwise
        """
        # Build token display with exchange prefix if not main
        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

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
            f"🔴 {token_display} | {value_str} FULL LIQ @ {format_price(liq_price)}",
            f"<a href=\"{hypurrscan_url}\">{addr_display}</a>",
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

    def send_partial_liquidation_alert(
        self,
        token: str,
        address: str,
        liquidated_value: float,
        remaining_value: float,
        liq_price: float,
        new_liq_price: float = None,
        exchange: str = "main",
    ) -> Optional[int]:
        """
        Send partial liquidation alert.

        Args:
            token: Token symbol
            address: Wallet address
            liquidated_value: Value that was liquidated
            remaining_value: Remaining position value
            liq_price: Liquidation price where partial liq occurred
            new_liq_price: New liquidation price after partial (optional)
            exchange: Exchange name (default: "main")

        Returns:
            message_id if sent successfully, None otherwise
        """
        # Build token display with exchange prefix if not main
        if exchange and exchange != "main":
            token_display = f"{exchange}:{token}"
        else:
            token_display = token

        if liquidated_value >= 1_000_000:
            liq_value_str = f"${liquidated_value / 1_000_000:.1f}M"
        else:
            liq_value_str = f"${liquidated_value / 1_000:.0f}K"

        if remaining_value >= 1_000_000:
            remaining_str = f"${remaining_value / 1_000_000:.1f}M"
        else:
            remaining_str = f"${remaining_value / 1_000:.0f}K"

        def format_price(p: float) -> str:
            if p >= 1000:
                return f"${p:,.0f}"
            elif p >= 1:
                return f"${p:.2f}"
            else:
                return f"${p:.6f}"

        hypurrscan_url = f"https://hypurrscan.io/address/{address}"
        addr_display = f"{address[:6]}...{address[-4:]}"

        # Build second line with optional new liq price
        if new_liq_price:
            line2 = f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({remaining_str} LEFT @ {format_price(new_liq_price)})"
        else:
            line2 = f"<a href=\"{hypurrscan_url}\">{addr_display}</a> ({remaining_str} LEFT)"

        lines = [
            f"🟠 {token_display} | {liq_value_str} PARTIAL LIQ @ {format_price(liq_price)}",
            line2,
        ]

        message = "\n".join(lines)
        return self._send_message(message, skip_rate_limit=True)

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

    def as_callback(self):
        """
        Return a callback function compatible with Monitor.alert_callback.

        Returns:
            Callable[[str, str], None]
        """
        def callback(message: str, priority: str) -> None:
            # This is a legacy callback - just send the raw message
            self._send_message(message)
        return callback
