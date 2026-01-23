"""
Path Utilities
==============

Shared path validation and project directory constants.
"""

from pathlib import Path
from typing import List

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()

# Standard data directories
DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DATA_PROCESSED_DIR = DATA_DIR / "processed"
LOGS_DIR = PROJECT_ROOT / "logs"

# Allowed base directories for file operations (security: prevent path traversal)
ALLOWED_DATA_DIRS = [
    DATA_RAW_DIR,
    DATA_PROCESSED_DIR,
]


def validate_file_path(filepath: str, must_exist: bool = False) -> Path:
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


def ensure_directories() -> None:
    """Ensure all required data directories exist."""
    dirs = [
        DATA_RAW_DIR,
        DATA_PROCESSED_DIR,
        LOGS_DIR,
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def get_data_path(relative_path: str, raw: bool = True) -> Path:
    """
    Get a path within the data directory.

    Args:
        relative_path: Path relative to data directory
        raw: If True, use data/raw; if False, use data/processed

    Returns:
        Full path
    """
    base = DATA_RAW_DIR if raw else DATA_PROCESSED_DIR
    return base / relative_path
