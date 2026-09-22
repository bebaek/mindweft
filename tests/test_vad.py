import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from mindweft_client.vad import SileroVoiceActivityDetector, VoiceActivityDependencyError


def install_fake_dependencies(monkeypatch, model):
    torch = ModuleType("torch")
    torch.float32 = "float32"
    torch.tensor = Mock(return_value=object())
    silero = ModuleType("silero_vad")
    silero.load_silero_vad = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "silero_vad", silero)
    return torch, silero


@pytest.mark.parametrize("probability, expected", [(0.49, False), (0.5, True), (0.9, True)])
def test_streaming_model_inference(monkeypatch, probability, expected):
    model = Mock()
    model.return_value.item.return_value = probability
    torch, silero = install_fake_dependencies(monkeypatch, model)

    detector = SileroVoiceActivityDetector(threshold=0.5)
    assert detector.is_speech([0.1, -0.1], 16000) is expected
    silero.load_silero_vad.assert_called_once_with()
    torch.tensor.assert_called_once_with([0.1, -0.1], dtype=torch.float32)
    model.assert_called_once_with(torch.tensor.return_value, 16000)
    detector.reset()
    model.reset_states.assert_called_once_with()


def test_empty_samples_skip_inference(monkeypatch):
    model = Mock()
    torch, _ = install_fake_dependencies(monkeypatch, model)
    detector = SileroVoiceActivityDetector()

    assert detector.is_speech([], 16000) is False
    torch.tensor.assert_not_called()
    model.assert_not_called()


def test_non_callable_model_is_rejected_at_load(monkeypatch):
    install_fake_dependencies(monkeypatch, object())

    with pytest.raises(VoiceActivityDependencyError, match="callable streaming model"):
        SileroVoiceActivityDetector()


def test_reset_without_optional_reset_states(monkeypatch):
    install_fake_dependencies(monkeypatch, lambda tensor, rate: None)
    SileroVoiceActivityDetector().reset()


def test_missing_dependency_has_install_guidance(monkeypatch):
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    monkeypatch.setitem(sys.modules, "torch", ModuleType("torch"))

    with pytest.raises(VoiceActivityDependencyError, match="uv sync --extra voice"):
        SileroVoiceActivityDetector()
