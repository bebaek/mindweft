from __future__ import annotations

from app.unified_config import apply_startup_config


def load_environment(*, discover_default_files: bool | None = None) -> None:
    apply_startup_config(discover_default_files=discover_default_files)
