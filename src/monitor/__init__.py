"""
Monitor Package
===============

Continuous monitoring service for liquidation hunting opportunities.

Components:
- orchestrator.py: Main MonitorService class with scheduling
- scan_phase.py: Scan phase logic (cohort -> position -> filter -> alert)
- monitor_phase.py: Monitor phase logic (price polling, proximity alerts)
- watchlist.py: Watchlist building and management
- alerts.py: Telegram alert system
- database.py: SQLite persistence for state
"""

from .orchestrator import MonitorService
from .alerts import TelegramAlerts, send_test_alert
from src.models import WatchedPosition

__all__ = [
    "MonitorService",
    "WatchedPosition",
    "TelegramAlerts",
    "send_test_alert",
]
