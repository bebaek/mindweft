import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

interface ContextDialogProps {
  threadId: string | null;
  open: boolean;
  onClose: () => void;
  onThreadCompacted: (threadId: string) => void;
}

export function ContextDialog({ threadId, open, onClose, onThreadCompacted }: ContextDialogProps) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [confirmCompact, setConfirmCompact] = useState(false);
  const context = useQuery({
    queryKey: ["thread-context", threadId, authentication],
    queryFn: ({ signal }) => api.getThreadContext(threadId!, signal),
    enabled: open && threadId !== null,
    retry: false,
  });
  const compact = useMutation({
    mutationFn: () => api.compactThread(threadId!),
    onSuccess: (result) => {
      setConfirmCompact(false);
      void queryClient.invalidateQueries({ queryKey: ["thread-context", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      if (result.thread_id !== threadId) {
        onThreadCompacted(result.thread_id);
        onClose();
      }
    },
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const usage = context.data?.usage;

  return (
    <dialog
      className="context-dialog"
      ref={dialogRef}
      aria-labelledby="context-dialog-title"
      onCancel={onClose}
      onClose={onClose}
    >
      <div className="context-dialog-header">
        <div><p className="eyebrow">Conversation</p><h2 id="context-dialog-title">Thread context</h2></div>
        <button type="button" onClick={onClose} aria-label="Close context">×</button>
      </div>

      {context.isPending && <div className="context-loading">Inspecting thread context…</div>}
      {context.isError && <div className="context-error" role="alert">Could not load thread context.</div>}
      {context.data && usage && (
        <div className="context-dialog-body">
          <dl className="context-stats">
            <div><dt>Estimated tokens</dt><dd>{usage.total_tokens.toLocaleString()}</dd></div>
            <div><dt>Messages</dt><dd>{usage.message_count.toLocaleString()}</dd></div>
            <div><dt>Summarized</dt><dd>{usage.summarized_message_count.toLocaleString()}</dd></div>
            <div><dt>Unsummarized</dt><dd>{usage.unsummarized_message_count.toLocaleString()}</dd></div>
          </dl>

          <section className="context-section">
            <div><p className="eyebrow">Memory</p><h3>Conversation summary</h3></div>
            <p>{context.data.summary || "This thread has not been summarized yet."}</p>
          </section>

          <details className="raw-context">
            <summary>Inspect rendered model context</summary>
            <pre>{context.data.rendered || "No rendered context is available."}</pre>
          </details>

          {compact.data && (
            <p className="compact-result" role="status">
              Compacted {compact.data.compacted_message_count.toLocaleString()} message{compact.data.compacted_message_count === 1 ? "" : "s"}.
            </p>
          )}
          {compact.isError && <p className="context-error" role="alert">Context compaction failed. The original messages were preserved.</p>}

          <div className="context-actions">
            {confirmCompact ? (
              <div className="compact-confirm">
                <p>Older messages will be summarized into a new child thread. The source thread will be preserved. Continue?</p>
                <button type="button" onClick={() => setConfirmCompact(false)}>Cancel</button>
                <button type="button" className="compact-primary" disabled={compact.isPending} onClick={() => compact.mutate()}>
                  {compact.isPending ? "Compacting…" : "Confirm compaction"}
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="compact-trigger"
                disabled={usage.unsummarized_message_count === 0}
                onClick={() => setConfirmCompact(true)}
              >
                Compact thread context
              </button>
            )}
          </div>
        </div>
      )}
    </dialog>
  );
}
