"""Locations and logger shared by console modules; contains no runtime state."""

from pathlib import Path
import logging

CONSOLE_FILE = Path(__file__).resolve().parent / "app.py"
logger = logging.getLogger("console")
