export interface AudioRecordingSession {
  cancel(): Promise<void>;
  stop(): Promise<File>;
}

export interface StartAudioRecordingOptions {
  maxBytes: number;
  maxDurationSeconds: number;
  onDurationChange?: (durationSeconds: number) => void;
  onLimitReached?: () => void;
}

interface AudioContextConstructor {
  new (): AudioContext;
}

function audioContextConstructor(): AudioContextConstructor | undefined {
  return window.AudioContext
    ?? (window as typeof window & { webkitAudioContext?: AudioContextConstructor }).webkitAudioContext;
}

export function audioRecordingSupported(): boolean {
  return Boolean(navigator.mediaDevices && audioContextConstructor());
}

export function encodePcm16Wav(samples: Float32Array, sampleRate: number): Blob {
  const bytesPerSample = 2;
  const dataBytes = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  function writeAscii(offset: number, value: string) {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  }

  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeAscii(36, "data");
  view.setUint32(40, dataBytes, true);

  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * bytesPerSample, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function recordingFilename(now = new Date()): string {
  return `recording-${now.toISOString().replace(/[:.]/g, "-")}.wav`;
}

export async function startPcmWavRecording(
  options: StartAudioRecordingOptions,
): Promise<AudioRecordingSession> {
  const AudioContextClass = audioContextConstructor();
  if (!navigator.mediaDevices || !AudioContextClass) {
    throw new Error("Audio recording is not supported by this browser.");
  }
  if (options.maxBytes <= 44 || options.maxDurationSeconds <= 0) {
    throw new Error("Audio recording limits are invalid.");
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  let context: AudioContext | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let processor: ScriptProcessorNode | null = null;
  let silentGain: GainNode | null = null;
  let finished = false;
  let limitSignaled = false;
  const chunks: Float32Array[] = [];
  let frameCount = 0;

  const cleanup = async () => {
    processor?.disconnect();
    source?.disconnect();
    silentGain?.disconnect();
    processor = null;
    source = null;
    silentGain = null;
    for (const track of stream.getTracks()) track.stop();
    if (context && context.state !== "closed") await context.close();
    context = null;
  };

  try {
    context = new AudioContextClass();
    const sampleRate = context.sampleRate;
    const durationFrames = Math.max(1, Math.floor(options.maxDurationSeconds * sampleRate));
    const byteFrames = Math.max(1, Math.floor((options.maxBytes - 44) / 2));
    const maxFrames = Math.min(durationFrames, byteFrames);
    source = context.createMediaStreamSource(stream);
    processor = context.createScriptProcessor(4096, 1, 1);
    silentGain = context.createGain();
    silentGain.gain.value = 0;

    processor.onaudioprocess = (event) => {
      if (finished || limitSignaled) return;
      const input = event.inputBuffer.getChannelData(0);
      const remaining = maxFrames - frameCount;
      const chunk = new Float32Array(input.subarray(0, Math.min(input.length, remaining)));
      chunks.push(chunk);
      frameCount += chunk.length;
      options.onDurationChange?.(frameCount / sampleRate);
      if (frameCount >= maxFrames && !limitSignaled) {
        limitSignaled = true;
        options.onLimitReached?.();
      }
    };

    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(context.destination);
    if (context.state === "suspended") await context.resume();

    return {
      async cancel() {
        if (finished) return;
        finished = true;
        await cleanup();
      },
      async stop() {
        if (finished) throw new Error("This audio recording has already ended.");
        finished = true;
        const samples = new Float32Array(frameCount);
        let offset = 0;
        for (const chunk of chunks) {
          samples.set(chunk, offset);
          offset += chunk.length;
        }
        const blob = encodePcm16Wav(samples, sampleRate);
        await cleanup();
        if (samples.length === 0) throw new Error("No audio was captured. Please try recording again.");
        return new File([blob], recordingFilename(), { type: "audio/wav" });
      },
    };
  } catch (error) {
    await cleanup();
    throw error;
  }
}
