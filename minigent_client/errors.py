from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MinigentAPIError(RuntimeError):
    """User-facing API/client error with optional technical detail."""

    message: str
    category: str = "request_failed"
    detail: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_dict(self, *, include_detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": self.message,
            "category": self.category,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if include_detail and self.detail:
            payload["detail"] = self.detail
        return payload
