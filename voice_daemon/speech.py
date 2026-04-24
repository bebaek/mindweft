from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO

from voice_daemon.audio import AudioDependencyError, load_recorded_audio_from_wav
from voice_daemon.service import SpeechOutput


@dataclass
class ConsoleSpeechOutput(SpeechOutput):
    output_stream: TextIO
    _speaking: bool = False

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self._speaking = True
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        self._speaking = False

    def stop(self) -> None:
        self._speaking = False

    def is_speaking(self) -> bool:
        return self._speaking

    def wait(self) -> None:
        self._speaking = False


@dataclass
class MacOsSaySpeechOutput(SpeechOutput):
    output_stream: TextIO
    voice: str | None = None
    _process: subprocess.Popen[str] | None = field(default=None, init=False)
    _interrupted: bool = field(default=False, init=False)

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        spoken_text = _sanitize_text_for_tts(text)
        command = ["say"]
        if self.voice:
            command.extend(["-v", self.voice])
        command.append(spoken_text)
        try:
            self._interrupted = False
            self._process = subprocess.Popen(command, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("`say` is not available on this system") from exc

    def stop(self) -> None:
        if not self.is_speaking():
            return
        self._interrupted = True
        assert self._process is not None
        self._process.terminate()

    def is_speaking(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def wait(self) -> None:
        if self._process is None:
            return
        return_code = self._process.wait()
        interrupted = self._interrupted
        self._process = None
        self._interrupted = False
        if interrupted:
            return
        if return_code != 0:
            raise RuntimeError(f"`say` failed with exit code {return_code}")


@dataclass
class PiperSpeechOutput(SpeechOutput):
    output_stream: TextIO
    model: str
    model_dir: str | None = None
    speaker: int | None = None
    length_scale: float | None = None
    sentence_silence: float | None = None
    _playback_thread: threading.Thread | None = field(default=None, init=False)
    _interrupted: bool = field(default=False, init=False)
    _playback_error: BaseException | None = field(default=None, init=False)

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        audio = self._synthesize(_sanitize_text_for_tts(text))
        sounddevice = _load_sounddevice_for_output()
        samples = _recorded_audio_to_numpy(audio)
        try:
            sounddevice.play(samples, samplerate=audio.sample_rate, blocking=False)
        except Exception as exc:
            raise RuntimeError(f"Piper playback failed: {exc}") from exc
        self._interrupted = False
        self._playback_error = None
        self._playback_thread = threading.Thread(
            target=self._wait_for_playback,
            args=(sounddevice,),
            daemon=True,
        )
        self._playback_thread.start()

    def stop(self) -> None:
        if not self.is_speaking():
            return
        self._interrupted = True
        sounddevice = _load_sounddevice_for_output()
        sounddevice.stop()

    def is_speaking(self) -> bool:
        return self._playback_thread is not None and self._playback_thread.is_alive()

    def wait(self) -> None:
        if self._playback_thread is None:
            return
        self._playback_thread.join()
        self._playback_thread = None
        interrupted = self._interrupted
        self._interrupted = False
        playback_error = self._playback_error
        self._playback_error = None
        if interrupted:
            return
        if playback_error is not None:
            raise RuntimeError(f"Piper playback failed: {playback_error}") from playback_error

    def _synthesize(self, text: str):
        with NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            output_path = Path(temp_file.name)
        try:
            piper_executable = _resolve_piper_executable()
            model_path = _resolve_piper_model_path(self.model, self.model_dir)
            command = [piper_executable, "--model", str(model_path), "--output_file", str(output_path)]
            if self.speaker is not None:
                command.extend(["--speaker", str(self.speaker)])
            if self.length_scale is not None:
                command.extend(["--length-scale", str(self.length_scale)])
            if self.sentence_silence is not None:
                command.extend(["--sentence-silence", str(self.sentence_silence)])
            result = subprocess.run(
                command,
                input=text,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                if detail:
                    raise RuntimeError(f"Piper synthesis failed: {detail}")
                raise RuntimeError(f"Piper synthesis failed with exit code {result.returncode}")
            return load_recorded_audio_from_wav(output_path)
        except FileNotFoundError as exc:
            raise RuntimeError("`piper` is not available on this system") from exc
        finally:
            output_path.unlink(missing_ok=True)

    def _wait_for_playback(self, sounddevice) -> None:
        try:
            sounddevice.wait()
        except Exception as exc:
            self._playback_error = exc


def _load_sounddevice_for_output():
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioDependencyError(
            "sounddevice is required for Piper audio playback. Install with `uv sync --extra voice`."
        ) from exc
    return sounddevice


def _sanitize_text_for_tts(text: str) -> str:
    sanitized = text.replace("\r\n", "\n").replace("\r", "\n")

    parts: list[str] = []
    for raw_line in sanitized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^>\s?", "", line)
        if match := re.match(r"#{1,6}\s+(.+)", line):
            parts.append(_ensure_tts_sentence(_strip_inline_markdown(match.group(1)), capitalize=True))
            continue
        if match := re.match(r"[-+*]\s+(?:\[[ xX]\]\s+)?(.+)", line):
            parts.append(_ensure_tts_sentence(_strip_inline_markdown(match.group(1)), capitalize=True))
            continue
        if match := re.match(r"\d+[.)]\s+(?:\[[ xX]\]\s+)?(.+)", line):
            parts.append(_ensure_tts_sentence(_strip_inline_markdown(match.group(1)), capitalize=True))
            continue
        parts.append(_strip_inline_markdown(line))

    sanitized = " ".join(parts)
    sanitized = re.sub(r"[*_~#>|]", "", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized.strip()


def _ensure_tts_sentence(text: str, *, capitalize: bool = False) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if capitalize:
        stripped = _capitalize_tts_sentence(stripped)
    if stripped[-1] in ".!?;:":
        return stripped
    return f"{stripped}."


def _capitalize_tts_sentence(text: str) -> str:
    for index, char in enumerate(text):
        if char.isalpha():
            return f"{text[:index]}{char.upper()}{text[index + 1:]}"
    return text


def _strip_inline_markdown(text: str) -> str:
    stripped = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
    stripped = re.sub(r"`([^`]*)`", r"\1", stripped)
    stripped = re.sub(r"(^|\s)[*_]{1,3}([^*_]+?)[*_]{1,3}(?=\s|$)", r"\1\2", stripped)
    return stripped.strip()


def _recorded_audio_to_numpy(audio):
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioDependencyError(
            "numpy is required for Piper audio playback. Install with `uv sync --extra voice`."
        ) from exc
    samples = np.frombuffer(audio.pcm_bytes, dtype=np.int16)
    if audio.channels > 1:
        return samples.reshape(-1, audio.channels)
    return samples


def _resolve_piper_executable() -> str:
    resolved = shutil.which("piper")
    if resolved:
        return resolved

    interpreter_dir = Path(sys.executable).absolute().parent
    local_candidates = [interpreter_dir / "piper"]
    if os.name == "nt":
        local_candidates.append(interpreter_dir / "piper.exe")

    for candidate in local_candidates:
        if candidate.is_file():
            if os.access(candidate, os.X_OK):
                return str(candidate)
            raise RuntimeError(
                f"`piper` exists at {str(candidate)!r} but is not executable. "
                "Fix its permissions or remove it from PATH."
            )

    for directory in os.environ.get("PATH", "").split(":"):
        if not directory:
            continue
        candidate = os.path.join(directory, "piper")
        if os.path.exists(candidate):
            raise RuntimeError(
                f"`piper` exists at {candidate!r} but is not executable. "
                "Fix its permissions or remove it from PATH."
            )

    raise RuntimeError(
        "`piper` is not available on PATH. Install voice deps with `uv sync --dev --extra voice` "
        "and confirm the Piper CLI is installed."
    )


def _resolve_piper_model_path(model: str, model_dir: str | None) -> Path:
    candidate = Path(model).expanduser()
    if candidate.exists():
        return candidate
    if candidate.suffix == ".onnx":
        raise RuntimeError(f"Piper model path does not exist: {candidate}")

    download_dir = _default_piper_model_dir(model_dir)
    model_path = download_dir / f"{model}.onnx"
    if model_path.exists():
        return model_path

    try:
        from piper.download_voices import download_voice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Piper voice download support is unavailable. Install `piper-tts` with "
            "`uv sync --dev --extra voice`."
        ) from exc

    download_dir.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(model, download_dir)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to resolve Piper voice '{model}'. Provide a .onnx path or download failed: {exc}"
        ) from exc

    if not model_path.exists():
        raise RuntimeError(
            f"Piper voice '{model}' did not produce the expected model file at {model_path}"
        )
    return model_path


def _default_piper_model_dir(model_dir: str | None) -> Path:
    if model_dir:
        return Path(model_dir).expanduser()
    return Path.home() / ".cache" / "minigent" / "piper"
