import { useEffect, useRef, useState } from "react";
import {
  audioRecordingSupported,
  startPcmWavRecording,
  type AudioRecordingSession,
} from "../audio/wavRecorder";

interface AudioRecorderProps {
  disabled: boolean;
  maxBytes: number;
  maxDurationSeconds: number;
  unavailableReason?: string | null;
  onError: (message: string) => void;
  onRecorded: (file: File) => void;
  onRecordingChange?: (recording: boolean) => void;
}

type RecordingState = "idle" | "requesting" | "recording" | "stopping";

function microphoneErrorMessage(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Microphone access was denied. Allow microphone access in your browser and try again.";
    }
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
      return "No microphone is available.";
    }
    if (error.name === "NotReadableError" || error.name === "TrackStartError") {
      return "The microphone is unavailable or already in use.";
    }
  }
  return error instanceof Error ? error.message : "Could not start audio recording.";
}

function formatRecordingDuration(seconds: number): string {
  const wholeSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(wholeSeconds / 60);
  return `${String(minutes)}:${String(wholeSeconds % 60).padStart(2, "0")}`;
}

export function AudioRecorder({
  disabled,
  maxBytes,
  maxDurationSeconds,
  unavailableReason,
  onError,
  onRecorded,
  onRecordingChange,
}: AudioRecorderProps) {
  const [state, setState] = useState<RecordingState>("idle");
  const [durationSeconds, setDurationSeconds] = useState(0);
  const sessionRef = useRef<AudioRecordingSession | null>(null);
  const mountedRef = useRef(true);
  const disabledRef = useRef(disabled);
  const supported = audioRecordingSupported();

  useEffect(() => {
    onRecordingChange?.(state !== "idle");
  }, [onRecordingChange, state]);

  useEffect(() => () => {
    mountedRef.current = false;
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) void session.cancel().catch(() => undefined);
  }, []);

  useEffect(() => {
    disabledRef.current = disabled;
    if (!disabled) return;
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) {
      void session.cancel()
        .catch(() => undefined)
        .finally(() => {
          if (mountedRef.current) {
            setState("idle");
            setDurationSeconds(0);
          }
        });
    }
  }, [disabled]);

  async function finishRecording() {
    const session = sessionRef.current;
    if (!session || state === "stopping") return;
    sessionRef.current = null;
    setState("stopping");
    try {
      const file = await session.stop();
      if (mountedRef.current) onRecorded(file);
    } catch (error) {
      if (mountedRef.current) onError(microphoneErrorMessage(error));
    } finally {
      if (mountedRef.current) {
        setState("idle");
        setDurationSeconds(0);
      }
    }
  }

  async function startRecording() {
    if (disabled || !supported || state !== "idle") return;
    setState("requesting");
    setDurationSeconds(0);
    onError("");
    try {
      const session = await startPcmWavRecording({
        maxBytes,
        maxDurationSeconds,
        onDurationChange: (duration) => {
          if (mountedRef.current) setDurationSeconds(duration);
        },
        onLimitReached: () => {
          if (mountedRef.current) void finishRecording();
        },
      });
      if (!mountedRef.current || disabledRef.current) {
        await session.cancel();
        if (mountedRef.current) setState("idle");
        return;
      }
      sessionRef.current = session;
      setState("recording");
    } catch (error) {
      if (mountedRef.current) {
        setState("idle");
        onError(microphoneErrorMessage(error));
      }
    }
  }

  async function cancelRecording() {
    const session = sessionRef.current;
    sessionRef.current = null;
    try {
      if (session) await session.cancel();
    } catch (error) {
      if (mountedRef.current) onError(microphoneErrorMessage(error));
    } finally {
      if (mountedRef.current) {
        setState("idle");
        setDurationSeconds(0);
      }
    }
  }

  if (state === "recording" || state === "stopping") {
    return (
      <div className="audio-recorder-active" role="group" aria-label="Audio recording">
        <span className="recording-indicator" aria-hidden="true" />
        <output aria-live="off">Recording {formatRecordingDuration(durationSeconds)}</output>
        <button
          type="button"
          className="audio-recording-stop"
          disabled={state === "stopping"}
          onClick={() => void finishRecording()}
        >
          {state === "stopping" ? "Saving…" : "Stop"}
        </button>
        <button
          type="button"
          className="audio-recording-cancel"
          disabled={state === "stopping"}
          onClick={() => void cancelRecording()}
        >
          Cancel
        </button>
      </div>
    );
  }

  const title = !supported
    ? "Audio recording is not supported by this browser"
    : disabled
      ? (unavailableReason ?? "Audio input is unavailable")
      : "Record audio";
  return (
    <button
      type="button"
      className="record-audio"
      aria-label={state === "requesting" ? "Requesting microphone access" : "Record audio"}
      title={title}
      disabled={disabled || !supported || state === "requesting"}
      onClick={() => void startRecording()}
    >
      <span aria-hidden="true">●</span>
      {state === "requesting" && <span className="sr-only">Requesting microphone access</span>}
    </button>
  );
}
