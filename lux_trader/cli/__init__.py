"""Command-line interface for Project Lux.

M1 exposes only the replay-strategy surface: ``replay``, ``summary``, ``doctor``.
Live and execution commands are added by later milestones.
"""

from .dispatch import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
