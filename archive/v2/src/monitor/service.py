"""
Monitor Service (DEPRECATED)
============================

DEPRECATED: This module has been split into modular components.
Please update imports to use:
    from src.monitor import MonitorService  # Main service class
    from src.monitor.orchestrator import MonitorService  # Alternative
    from src.models import WatchedPosition  # Position tracking

Components:
- orchestrator.py: Main MonitorService class with scheduling
- scan_phase.py: Scan phase logic (cohort -> position -> filter -> alert)
- monitor_phase.py: Monitor phase logic (price polling, proximity alerts)
- watchlist.py: Watchlist building and management

This module provides backward compatibility by re-exporting from the new location.
"""

import warnings

warnings.warn(
    "src.monitor.service is deprecated. "
    "Use 'from src.monitor import MonitorService' or "
    "'from src.monitor.orchestrator import MonitorService' instead.",
    DeprecationWarning,
    stacklevel=2
)

# Re-export from new locations for backward compatibility
from .orchestrator import MonitorService
from src.models import WatchedPosition

__all__ = [
    "MonitorService",
    "WatchedPosition",
]
