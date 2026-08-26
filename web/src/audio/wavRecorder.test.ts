import { afterEach, describe, expect, it, vi } from "vitest";
import { encodePcm16Wav, startPcmWavRecording } from "./wavRecorder";

function blobArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Could not read blob"));
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(blob);
  });
}

function ascii(view: DataView, offset: number, length: number): string {
  return Array.from({ length }, (_, index) => String.fromCharCode(view.getUint8(offset + index))).join("");
}

const originalAudioContext = Object.getOwnPropertyDescriptor(window, "AudioContext");
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");

afterEach(() => {
  if (originalAudioContext) Object.defineProperty(window, "AudioContext", originalAudioContext);
  else Reflect.deleteProperty(window, "AudioContext");
  if (originalMediaDevices) Object.defineProperty(navigator, "mediaDevices", originalMediaDevices);
  else Reflect.deleteProperty(navigator, "mediaDevices");
});

describe("PCM WAV encoding", () => {
  it("writes a canonical mono 16-bit WAV header and sample data", async () => {
    const blob = encodePcm16Wav(new Float32Array([-1, 0, 1]), 16_000);
    const view = new DataView(await blobArrayBuffer(blob));

    expect(blob.type).toBe("audio/wav");
    expect(blob.size).toBe(50);
    expect(ascii(view, 0, 4)).toBe("RIFF");
    expect(view.getUint32(4, true)).toBe(42);
    expect(ascii(view, 8, 4)).toBe("WAVE");
    expect(ascii(view, 12, 4)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(ascii(view, 36, 4)).toBe("data");
    expect(view.getUint32(40, true)).toBe(6);
    expect(view.getInt16(44, true)).toBe(-32_768);
    expect(view.getInt16(46, true)).toBe(0);
    expect(view.getInt16(48, true)).toBe(32_767);
  });

  it("clips samples outside the PCM range", async () => {
    const blob = encodePcm16Wav(new Float32Array([-2, 2]), 8_000);
    const view = new DataView(await blobArrayBuffer(blob));

    expect(view.getInt16(44, true)).toBe(-32_768);
    expect(view.getInt16(46, true)).toBe(32_767);
  });

  it("captures bounded PCM samples and releases microphone resources", async () => {
    const stopTrack = vi.fn();
    const closeContext = vi.fn(() => Promise.resolve());
    const audioInput: { emit?: (samples: Float32Array) => void } = {};

    class FakeAudioContext {
      readonly sampleRate = 4;
      readonly state = "running";
      readonly destination = {} as AudioDestinationNode;

      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() } as unknown as MediaStreamAudioSourceNode;
      }

      createScriptProcessor() {
        let handler: ((event: AudioProcessingEvent) => void) | null = null;
        const node = {
          connect: vi.fn(),
          disconnect: vi.fn(),
          get onaudioprocess() { return handler; },
          set onaudioprocess(value) { handler = value; },
        };
        audioInput.emit = (samples) => handler?.({
          inputBuffer: { getChannelData: () => samples },
        } as unknown as AudioProcessingEvent);
        return node as unknown as ScriptProcessorNode;
      }

      createGain() {
        return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() } as unknown as GainNode;
      }

      close = closeContext;
      resume = vi.fn(() => Promise.resolve());
    }

    const getUserMedia = vi.fn(() => Promise.resolve({
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream));
    Object.defineProperty(window, "AudioContext", { configurable: true, value: FakeAudioContext });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const onDurationChange = vi.fn();
    const onLimitReached = vi.fn();

    const session = await startPcmWavRecording({
      maxBytes: 1_000,
      maxDurationSeconds: 1,
      onDurationChange,
      onLimitReached,
    });
    if (!audioInput.emit) throw new Error("Recorder did not install an audio callback");
    audioInput.emit(new Float32Array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]));
    const file = await session.stop();

    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    expect(onDurationChange).toHaveBeenLastCalledWith(1);
    expect(onLimitReached).toHaveBeenCalledOnce();
    expect(file.type).toBe("audio/wav");
    expect(file.size).toBe(52);
    expect(stopTrack).toHaveBeenCalledOnce();
    expect(closeContext).toHaveBeenCalledOnce();
  });
});
