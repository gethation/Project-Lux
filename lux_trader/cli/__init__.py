"""Command-line interface for Project Lux.

The public surface is consolidated into seven top-level commands with explicit
nested actions for status, recovery, and gated administration.
"""

from .dispatch import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
