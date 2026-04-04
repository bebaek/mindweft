import asyncio
from datetime import datetime

from app.tools import build_local_tool_registry


def test_local_registry_exposes_current_time_tool() -> None:
    registry = build_local_tool_registry()

    specs = {spec.name: spec for spec in registry.specs()}

    assert "current_time" in specs
    assert specs["current_time"].description == "Return the current UTC time in ISO 8601 format."


def test_current_time_tool_returns_iso8601_timestamp() -> None:
    registry = build_local_tool_registry()

    result = asyncio.run(registry.execute("current_time", {}))

    assert set(result) == {"current_time"}
    datetime.fromisoformat(result["current_time"])
