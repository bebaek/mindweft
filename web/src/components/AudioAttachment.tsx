import { useEffect, useState } from "react";
import type { AudioPart } from "../api/client";
import { useAuth } from "../auth/auth-context";

interface AudioAttachmentProps {
  audio: AudioPart;
  threadId: string;
}

export function AudioAttachment({ audio, threadId }: AudioAttachmentProps) {
  const { api } = useAuth();
  const [source, setSource] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void api.getAttachmentBlob(threadId, audio.attachment_id, controller.signal).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      setSource(objectUrl);
    }).catch((caught: unknown) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setSource("");
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, audio.attachment_id, threadId]);

  function download() {
    if (!source) return;
    const anchor = window.document.createElement("a");
    anchor.href = source;
    anchor.download = audio.filename;
    anchor.rel = "noreferrer";
    anchor.click();
  }

  return (
    <div className="message-audio">
      <div><span className="audio-badge" aria-hidden="true">WAV</span><strong title={audio.filename}>{audio.filename}</strong></div>
      {source === null && <span role="status">Loading audio…</span>}
      {source === "" && <span role="alert">Audio unavailable</span>}
      {source && <audio controls preload="metadata" src={source}><track kind="captions" /></audio>}
      <button type="button" disabled={!source} onClick={download}>Download</button>
    </div>
  );
}
