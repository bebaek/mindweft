import { useEffect, useId, useRef, useState } from "react";
import type { DocumentPart } from "../api/client";
import { useAuth } from "../auth/auth-context";

interface DocumentAttachmentProps {
  document: DocumentPart;
  threadId: string;
}

type RetrievalStatus = "idle" | "loading" | "ready" | "error";

export function DocumentAttachment({ document, threadId }: DocumentAttachmentProps) {
  const { api } = useAuth();
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const previewButtonRef = useRef<HTMLButtonElement>(null);
  const objectUrlRef = useRef<string | null>(null);
  const requestRef = useRef<Promise<string> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const isTextDocument = document.mime_type === "text/plain";
  const documentLabel = isTextDocument ? "text document" : "PDF";
  const [previewOpen, setPreviewOpen] = useState(false);
  const [source, setSource] = useState<string | null>(null);
  const [textContent, setTextContent] = useState<string | null>(null);
  const [status, setStatus] = useState<RetrievalStatus>("idle");

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (previewOpen && !dialog.open) dialog.showModal();
    if (!previewOpen && dialog.open) dialog.close();
  }, [previewOpen]);

  function loadDocument(): Promise<string> {
    if (objectUrlRef.current) return Promise.resolve(objectUrlRef.current);
    if (requestRef.current) return requestRef.current;

    const controller = new AbortController();
    controllerRef.current = controller;
    setStatus("loading");
    const request = api.getAttachmentBlob(
      threadId,
      document.attachment_id,
      controller.signal,
    ).then(async (blob) => {
      const objectUrl = URL.createObjectURL(blob);
      const text = isTextDocument ? await blob.text() : null;
      if (!mountedRef.current) {
        URL.revokeObjectURL(objectUrl);
        throw new DOMException("Document component was removed", "AbortError");
      }
      objectUrlRef.current = objectUrl;
      setSource(objectUrl);
      setTextContent(text);
      setStatus("ready");
      return objectUrl;
    }).catch((caught: unknown) => {
      if (
        mountedRef.current &&
        !(caught instanceof DOMException && caught.name === "AbortError")
      ) {
        setStatus("error");
      }
      throw caught;
    }).finally(() => {
      requestRef.current = null;
      controllerRef.current = null;
    });
    requestRef.current = request;
    return request;
  }

  function openPreview() {
    setPreviewOpen(true);
    void loadDocument().catch(() => undefined);
  }

  function closePreview() {
    setPreviewOpen(false);
    window.setTimeout(() => previewButtonRef.current?.focus(), 0);
  }

  async function downloadDocument() {
    try {
      const objectUrl = await loadDocument();
      const anchor = window.document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = document.filename;
      anchor.rel = "noreferrer";
      anchor.style.display = "none";
      window.document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch {
      // The shared retrieval state exposes the error and a retry action.
    }
  }

  return (
    <div className="message-document">
      <span className={`document-badge${isTextDocument ? " text" : ""}`} aria-hidden="true">{isTextDocument ? "TXT" : "PDF"}</span>
      <span className="document-filename" title={document.filename}>{document.filename}</span>
      <div className="document-actions">
        <button ref={previewButtonRef} type="button" onClick={openPreview}>Preview</button>
        <button type="button" onClick={() => void downloadDocument()}>Download</button>
      </div>
      {status === "loading" && <span className="document-status" role="status">Loading {documentLabel}…</span>}
      {status === "error" && (
        <span className="document-status document-error" role="alert">
          Could not load document.
          <button type="button" onClick={() => void loadDocument().catch(() => undefined)}>Retry</button>
        </span>
      )}

      <dialog
        ref={dialogRef}
        className="document-preview-dialog"
        aria-labelledby={titleId}
        onCancel={(event) => { event.preventDefault(); closePreview(); }}
        onClose={() => {
          if (previewOpen) closePreview();
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget) closePreview();
        }}
      >
        <div className="document-preview-header">
          <div>
            <p className="eyebrow">{isTextDocument ? "Text preview" : "PDF preview"}</p>
            <h2 id={titleId}>{document.filename}</h2>
          </div>
          <div className="document-preview-actions">
            <button type="button" onClick={() => void downloadDocument()}>Download</button>
            <button type="button" onClick={closePreview} aria-label={`Close preview of ${document.filename}`}>×</button>
          </div>
        </div>
        <div className="document-preview-body">
          {status === "loading" && <p role="status">Loading document preview…</p>}
          {status === "error" && (
            <div className="document-preview-error" role="alert">
              <p>The document could not be loaded.</p>
              <button type="button" onClick={() => void loadDocument().catch(() => undefined)}>Retry preview</button>
            </div>
          )}
          {status === "ready" && isTextDocument && textContent !== null && (
            <pre className="document-text-preview">{textContent}</pre>
          )}
          {status === "ready" && !isTextDocument && source && (
            <iframe
              src={source}
              title={`Preview of ${document.filename}`}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
            />
          )}
        </div>
      </dialog>
    </div>
  );
}
