#!/usr/bin/env python3
"""
CLI entry point for cohort scanning.

Usage:
    python scripts/scan_cohorts.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.step1_cohort import main

if __name__ == "__main__":
    main()
