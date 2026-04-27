from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minigent_client.audio import pcm16le_to_ints


class WakeWordDependencyError(RuntimeError):
    """Raised when optional wake-word dependencies are unavailable."""


class WakeWordDetector(Protocol):
    @property
    def frame_length(self) -> int: ...

    @property
    def sample_rate(self) -> int: ...

    @property
    def label(self) -> str: ...

    def reset(self) -> None: ...

    def process_chunk(self, chunk: bytes) -> bool: ...


@dataclass
class PorcupineWakeWordDetector:
    access_key: str
    keyword_path: str

    def __post_init__(self) -> None:
        try:
            import pvporcupine  # type: ignore[import-not-found]
        except ImportError as exc:
            raise WakeWordDependencyError(
                "pvporcupine is required for passive wake-word mode. Install with `uv sync --extra voice`."
            ) from exc
        self._engine = pvporcupine.create(
            access_key=self.access_key,
            keyword_paths=[self.keyword_path],
        )

    @property
    def frame_length(self) -> int:
        return int(self._engine.frame_length)

    @property
    def sample_rate(self) -> int:
        return int(self._engine.sample_rate)

    @property
    def label(self) -> str:
        return f"porcupine:{self.keyword_path}"

    def reset(self) -> None:
        return None

    def process_chunk(self, chunk: bytes) -> bool:
        samples = pcm16le_to_ints(chunk)
        return int(self._engine.process(samples)) >= 0


@dataclass
class OpenWakeWordDetector:
    model_name: str = "okay_nabu"
    threshold: float = 0.5

    def __post_init__(self) -> None:
        try:
            from pyopen_wakeword import (  # type: ignore[import-not-found]
                Model,
                OpenWakeWord,
                OpenWakeWordFeatures,
            )
            from pyopen_wakeword.openwakeword import (
                SAMPLES_PER_CHUNK,  # type: ignore[import-not-found]
            )
        except ImportError as exc:
            raise WakeWordDependencyError(
                "pyopen-wakeword is required for the openwakeword provider. Install with `uv sync --extra voice`."
            ) from exc
        try:
            model_enum = getattr(Model, self.model_name.strip().upper())
        except AttributeError as exc:
            raise WakeWordDependencyError(
                f"Unknown openWakeWord builtin model '{self.model_name}'."
            ) from exc
        self._frame_length = int(SAMPLES_PER_CHUNK)
        self._features = OpenWakeWordFeatures.from_builtin()
        self._detector = OpenWakeWord.from_builtin(model_enum)

    @property
    def frame_length(self) -> int:
        return self._frame_length

    @property
    def sample_rate(self) -> int:
        return 16_000

    @property
    def label(self) -> str:
        return f"openwakeword:{self.model_name}"

    def reset(self) -> None:
        self._features.reset()
        self._detector.reset()

    def process_chunk(self, chunk: bytes) -> bool:
        for features in self._features.process_streaming(chunk):
            for probability in self._detector.process_streaming(features):
                if float(probability) >= self.threshold:
                    return True
        return False
