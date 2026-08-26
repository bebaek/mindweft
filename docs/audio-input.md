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

Live provider checks are opt-in because they require credentials and may incur cost. The integration
harness generates a 250 ms mono 16-bit PCM WAV tone in memory, then exercises thread creation,
binary upload, audio message persistence, provider execution, and authenticated history retrieval.
It asserts provider acceptance and a non-empty response rather than a specific interpretation of the
tone.

Run one provider at a time with an explicitly selected audio-capable model:

```bash
MINDWEFT_RUN_LIVE_AUDIO_PROVIDER_TESTS=true \
OPENAI_API_KEY=... \
MINDWEFT_LIVE_AUDIO_OPENAI_MODEL=... \
uv run pytest tests/test_live_audio_providers.py -k openai -m integration -q

MINDWEFT_RUN_LIVE_AUDIO_PROVIDER_TESTS=true \
OPENROUTER_API_KEY=... \
MINDWEFT_LIVE_AUDIO_OPENROUTER_MODEL=... \
uv run pytest tests/test_live_audio_providers.py -k openrouter -m integration -q

MINDWEFT_RUN_LIVE_AUDIO_PROVIDER_TESTS=true \
GEMINI_API_KEY=... \
MINDWEFT_LIVE_AUDIO_GEMINI_MODEL=... \
uv run pytest tests/test_live_audio_providers.py -k gemini -m integration -q
```

The provider-standard `OPENAI_MODEL`, `OPENROUTER_MODEL`, and `GEMINI_MODEL` variables are accepted
when the corresponding test-specific model variable is omitted. Base URL and optional OpenRouter
attribution variables use the normal application names. Providers without credentials or a model
are skipped independently. Keep request/response debug logging disabled so credentials and encoded
audio are not written to logs.

Responses, Anthropic, and peer-agent backends should continue to reject audio before upload and are
covered without live requests. Do not use production credentials or sensitive recordings in routine
smoke tests.
