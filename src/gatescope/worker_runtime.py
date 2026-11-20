"""Small process lifecycle helpers shared by dedicated research workers."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from types import FrameType
from typing import Callable


def database_path(root: Path) -> Path:
    configured = Path(os.environ.get("GATESCOPE_DB_PATH", "data/gate_patterns.db"))
    return configured if configured.is_absolute() else root / configured


def install_stop_handlers(stop: Callable[[], None]) -> None:
    def handle(signum: int, frame: FrameType | None) -> None:
        stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
