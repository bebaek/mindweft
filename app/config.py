from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    load_dotenv(dotenv_path=Path(".env"), override=False)
