"""Parallel speech-to-text: base types, driver-backed engines, and the runner.

Concrete engine adapters now live in drop-in drivers under
``ws_collab/drivers/stt``. This package exposes the transport-neutral base types
and the assembly/runner helpers.
"""

from __future__ import annotations

from .base import Hypothesis, SttAdapter, normalize_text
from .engines import build_engines, run_stt

__all__ = [
    "Hypothesis",
    "SttAdapter",
    "normalize_text",
    "build_engines",
    "run_stt",
]
