"""Telegram alert functionality."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class AlertStats:
    """Statistics for alert tracking."""
    total_sent: int = 0
    total_errors: int = 0
    last_send_time: Optional[datetime] = None
    last_error_time: Optional[datetime] = None
    last_error_message: Optional[str] = None


class TelegramAlerts:
    """
    Telegram alert sender with error handling and metrics.

    Usage:
        alerts = TelegramAlerts(bot_token="...", chat_id="...")
        success = alerts.send("Position approaching liquidation", "proximity")
    """

    PRIORITY_PREFIXES = {
        "critical": "IMMINENT LIQUIDATION",
        "proximity": "APPROACHING LIQUIDATION",
    }

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0):
        """
        Initialize Telegram alerts.

        Args:
            bot_token: Telegram bot token
            chat_id: Target chat/channel ID
            timeout: Request timeout in seconds
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._stats = AlertStats()

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

        return cls(bot_token=bot_token, chat_id=chat_id)

    def send(self, message: str, priority: str = "") -> bool:
        """
        Send an alert message to Telegram.

        Args:
            message: Alert message text
            priority: Alert priority ("critical", "proximity", or empty)

        Returns:
            True if sent successfully, False otherwise
        """
        prefix = self.PRIORITY_PREFIXES.get(priority, "")
        text = f"{prefix}\n{message}" if prefix else message

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

            self._stats.total_sent += 1
            self._stats.last_send_time = datetime.now(timezone.utc)
            return True

        except requests.Timeout:
            error_msg = "Request timed out"
            logger.error(f"Failed to send Telegram alert: {error_msg}")
            self._record_error(error_msg)
            return False

        except requests.HTTPError as e:
            error_msg = f"HTTP error: {e.response.status_code}"
            logger.error(f"Failed to send Telegram alert: {error_msg}")
            self._record_error(error_msg)
            return False

        except requests.RequestException as e:
            error_msg = str(e)
            logger.error(f"Failed to send Telegram alert: {error_msg}")
            self._record_error(error_msg)
            return False

    def _record_error(self, message: str) -> None:
        """Record an error in stats."""
        self._stats.total_errors += 1
        self._stats.last_error_time = datetime.now(timezone.utc)
        self._stats.last_error_message = message

    def get_stats(self) -> AlertStats:
        """Get alert statistics."""
        return self._stats

    def as_callback(self):
        """
        Return a callback function compatible with Monitor.alert_callback.

        Returns:
            Callable[[str, str], None]
        """
        def callback(message: str, priority: str) -> None:
            self.send(message, priority)
        return callback
