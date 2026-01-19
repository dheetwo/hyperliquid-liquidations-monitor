#!/usr/bin/env python3
"""
CLI entry point for position filtering.

Usage:
    python scripts/filter_positions.py                          # Default: priority positions
    python scripts/filter_positions.py data/raw/position_data.csv   # Custom input
    python scripts/filter_positions.py -o data/processed/out.csv    # Custom output
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.step3_filter import main

if __name__ == "__main__":
    main()
