# Audio input smoke testing

Mindweft accepts validated uncompressed PCM WAV audio when server audio input is enabled and the
effective native LLM profile explicitly declares the `audio` input modality. Browser microphone
recording additionally requires a secure context—HTTPS or localhost—and user-granted microphone
permission.

## Browser microphone checklist

Use a non-production thread and a short recording without sensitive content.

1. Open `/console/` over HTTPS or localhost and select an audio-capable native profile.
2. Confirm **Record audio** is enabled, then select it and grant microphone permission.
3. Speak briefly and confirm the elapsed timer advances.
4. Select **Cancel** and confirm no queued attachment remains and the browser stops showing active
   microphone use.
5. Record again, select **Stop**, play the queued preview, and confirm it contains the expected audio.
6. Send the recording and confirm it appears in thread history with playback and download controls.
7. Start another recording, then choose a profile without audio capability. Confirm capture ends,
   no recording is queued, and the recording control becomes unavailable.
8. Temporarily configure a short `audio_input.max_duration_seconds`, record without selecting
   **Stop**, and confirm capture stops automatically at the configured limit.
9. Deny microphone permission and confirm the console reports an actionable permission error without
   creating an attachment.
10. Reload or navigate away during capture and confirm the browser stops showing active microphone
    use.

The browser encodes mono 16-bit PCM WAV locally. The resulting file still passes through the normal
attachment byte/count limits and server-side RIFF/WAVE validation before it reaches a provider.

## Provider smoke testing

Live provider checks are opt-in because they require credentials and may incur cost. For OpenAI or
OpenRouter Chat Completions and Gemini, send a short known phrase and verify that the response refers
to its audible content. Responses, Anthropic, and peer-agent backends should reject audio before
upload. Do not use production credentials or sensitive recordings in routine smoke tests.
