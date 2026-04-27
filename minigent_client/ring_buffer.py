from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class AudioRingBuffer:
    max_bytes: int
    _chunks: deque[bytes] = field(default_factory=deque)
    _size_bytes: int = 0

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size_bytes += len(chunk)
        while self._size_bytes > self.max_bytes and self._chunks:
            removed = self._chunks.popleft()
            self._size_bytes -= len(removed)

    def snapshot(self) -> list[bytes]:
        return list(self._chunks)
