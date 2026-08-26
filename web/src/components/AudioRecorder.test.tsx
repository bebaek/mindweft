import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AudioRecorder } from "./AudioRecorder";

const recorderMocks = vi.hoisted(() => ({
  supported: vi.fn(() => true),
  start: vi.fn(),
}));

vi.mock("../audio/wavRecorder", () => ({
  audioRecordingSupported: recorderMocks.supported,
  startPcmWavRecording: recorderMocks.start,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  recorderMocks.supported.mockReturnValue(true);
});

function renderRecorder(overrides: Partial<React.ComponentProps<typeof AudioRecorder>> = {}) {
  const props: React.ComponentProps<typeof AudioRecorder> = {
    disabled: false,
    maxBytes: 1_000_000,
    maxDurationSeconds: 60,
    onError: vi.fn(),
    onRecorded: vi.fn(),
    ...overrides,
  };
  render(<AudioRecorder {...props} />);
  return props;
}

describe("AudioRecorder", () => {
  it("requests the microphone and returns a WAV file when stopped", async () => {
    const recording = new File(["wav"], "recording.wav", { type: "audio/wav" });
    const session = { cancel: vi.fn(), stop: vi.fn(() => Promise.resolve(recording)) };
    recorderMocks.start.mockResolvedValue(session);
    const props = renderRecorder();

    fireEvent.click(screen.getByRole("button", { name: "Record audio" }));
    await screen.findByRole("group", { name: "Audio recording" });

    expect(recorderMocks.start).toHaveBeenCalledWith(expect.objectContaining({
      maxBytes: 1_000_000,
      maxDurationSeconds: 60,
    }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(props.onRecorded).toHaveBeenCalledWith(recording));
    expect(session.stop).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Record audio" })).toBeEnabled();
  });

  it("stops automatically when the recording limit is reached", async () => {
    const recording = new File(["wav"], "recording.wav", { type: "audio/wav" });
    const session = { cancel: vi.fn(), stop: vi.fn(() => Promise.resolve(recording)) };
    recorderMocks.start.mockResolvedValue(session);
    const props = renderRecorder();

    fireEvent.click(screen.getByRole("button", { name: "Record audio" }));
    await screen.findByRole("group", { name: "Audio recording" });
    const options = recorderMocks.start.mock.calls[0][0] as { onLimitReached: () => void };
    options.onLimitReached();

    await waitFor(() => expect(props.onRecorded).toHaveBeenCalledWith(recording));
    expect(session.stop).toHaveBeenCalledOnce();
  });

  it("cancels without producing an attachment", async () => {
    const session = { cancel: vi.fn(() => Promise.resolve()), stop: vi.fn() };
    recorderMocks.start.mockResolvedValue(session);
    const props = renderRecorder();

    fireEvent.click(screen.getByRole("button", { name: "Record audio" }));
    await screen.findByRole("group", { name: "Audio recording" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(session.cancel).toHaveBeenCalledOnce());
    expect(props.onRecorded).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Record audio" })).toBeEnabled();
  });

  it("cancels an active recording when audio input becomes unavailable", async () => {
    const session = { cancel: vi.fn(() => Promise.resolve()), stop: vi.fn() };
    recorderMocks.start.mockResolvedValue(session);
    const onError = vi.fn();
    const onRecorded = vi.fn();
    const { rerender } = render(
      <AudioRecorder
        disabled={false}
        maxBytes={1_000_000}
        maxDurationSeconds={60}
        onError={onError}
        onRecorded={onRecorded}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Record audio" }));
    await screen.findByRole("group", { name: "Audio recording" });

    rerender(
      <AudioRecorder
        disabled
        maxBytes={1_000_000}
        maxDurationSeconds={60}
        unavailableReason="The selected model profile does not accept audio."
        onError={onError}
        onRecorded={onRecorded}
      />,
    );

    await waitFor(() => expect(session.cancel).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Record audio" })).toBeDisabled();
    expect(onRecorded).not.toHaveBeenCalled();
  });

  it("reports denied microphone permission", async () => {
    recorderMocks.start.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    const props = renderRecorder();

    fireEvent.click(screen.getByRole("button", { name: "Record audio" }));

    await waitFor(() => expect(props.onError).toHaveBeenLastCalledWith(
      "Microphone access was denied. Allow microphone access in your browser and try again.",
    ));
  });

  it("disables recording when audio input is unavailable", () => {
    renderRecorder({ disabled: true, unavailableReason: "Audio input is disabled on this server." });

    expect(screen.getByRole("button", { name: "Record audio" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Record audio" })).toHaveAttribute(
      "title",
      "Audio input is disabled on this server.",
    );
  });
});
