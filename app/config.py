from __future__ import annotations

from app.unified_config import apply_startup_config


def load_environment() -> None:
    apply_startup_config()
