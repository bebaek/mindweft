from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast


class _SpeechProbability(Protocol):
    def item(self) -> float: ...


class _StreamingModel(Protocol):
    def __call__(self, samples: object, sample_rate: int) -> _SpeechProbability: ...


class VoiceActivityDependencyError(RuntimeError):
    """Raised when the optional VAD dependency is unavailable."""


@dataclass
class SileroVoiceActivityDetector:
    threshold: float = 0.5

    def __post_init__(self) -> None:
        try:
            import torch  # type: ignore[import-not-found]
            from silero_vad import load_silero_vad  # type: ignore[import-not-found]
        except ImportError as exc:
            raise VoiceActivityDependencyError(
                "silero-vad and torch are required for VAD. Install with `uv sync --extra voice`."
            ) from exc
        self._torch = torch
        model = load_silero_vad()
        # Silero also supports non-callable sequence models; this adapter needs
        # the callable streaming interface returned by the default loader.
        if not callable(model):
            raise VoiceActivityDependencyError(
                "Silero VAD must provide a callable streaming model."
            )
        self._model = cast(_StreamingModel, model)

    def reset(self) -> None:
        reset_states = getattr(self._model, "reset_states", None)
        if callable(reset_states):
            reset_states()

    def is_speech(self, samples: list[float], sample_rate: int) -> bool:
        if not samples:
            return False
        tensor = self._torch.tensor(samples, dtype=self._torch.float32)
        probability = float(self._model(tensor, sample_rate).item())
        return probability >= self.threshold
