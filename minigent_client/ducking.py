from __future__ import annotations

import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TextIO

from minigent_client.runtime import ClientState


def should_duck_for_state(state: ClientState) -> bool:
    return state in {ClientState.LISTENING, ClientState.FOLLOW_UP_LISTENING}


@dataclass
class MacOsAmbientVolumeDucker:
    ducked_output_volume: int
    output_stream: TextIO
    _saved_output_volume: int | None = field(default=None, init=False)
    _ducked: bool = field(default=False, init=False)
    _disabled: bool = field(default=False, init=False)
    _warned: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def sync_state(self, state: ClientState) -> None:
        with self._lock:
            if self._disabled:
                return
            try:
                if should_duck_for_state(state):
                    self._duck()
                    return
                self._restore()
            except Exception as exc:
                self._disable(exc)

    def close(self) -> None:
        with self._lock:
            if self._disabled:
                return
            try:
                self._restore()
            except Exception as exc:
                self._disable(exc)

    def temporarily_restore(
        self,
        callback: Callable[[], None],
        *,
        reduck_delay_seconds: float = 0.0,
    ) -> None:
        should_reduck = False
        with self._lock:
            if self._disabled or not self._ducked:
                pass
            else:
                try:
                    self._restore()
                    should_reduck = True
                except Exception as exc:
                    self._disable(exc)
        callback()
        if should_reduck:
            threading.Thread(
                target=self._duck_in_background,
                args=(max(reduck_delay_seconds, 0.0),),
                daemon=True,
            ).start()

    def _duck_in_background(self, delay_seconds: float = 0.0) -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        with self._lock:
            if self._disabled:
                return
            try:
                self._duck()
            except Exception as exc:
                self._disable(exc)

    def _duck(self) -> None:
        if self._ducked:
            return
        if self._saved_output_volume is None:
            self._saved_output_volume = self._read_output_volume()
        self._set_output_volume(self.ducked_output_volume)
        self._ducked = True

    def _restore(self) -> None:
        if not self._ducked or self._saved_output_volume is None:
            return
        self._set_output_volume(self._saved_output_volume)
        self._ducked = False
        self._saved_output_volume = None

    def _disable(self, exc: Exception) -> None:
        self._disabled = True
        self._ducked = False
        self._saved_output_volume = None
        if self._warned:
            return
        self.output_stream.write(f"[warning] ambient audio ducking disabled: {exc}\n")
        self.output_stream.flush()
        self._warned = True

    @staticmethod
    def validate_platform() -> None:
        if platform.system() != "Darwin":
            raise RuntimeError("ambient audio ducking is currently supported only on macOS")

    def _read_output_volume(self) -> int:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or f"osascript failed with exit code {result.returncode}")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(
                f"unexpected output volume response: {result.stdout.strip()!r}"
            ) from exc

    def _set_output_volume(self, volume: int) -> None:
        result = subprocess.run(
            ["osascript", "-e", f"set volume output volume {volume}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or f"osascript failed with exit code {result.returncode}")
