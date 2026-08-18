from __future__ import annotations

from dataclasses import dataclass


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
        self._model = load_silero_vad()

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
