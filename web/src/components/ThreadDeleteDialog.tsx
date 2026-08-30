import { useEffect, useRef } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/auth-context";

interface ThreadDeleteDialogProps {
  open: boolean;
  threadId: string;
  threadTitle: string;
  onClose: () => void;
  onDeleted: (result: { deletedCount: number; deletedThreadIds: string[]; lineage: boolean }) => void;
}

export function ThreadDeleteDialog({
  open,
  threadId,
  threadTitle,
  onClose,
  onDeleted,
}: ThreadDeleteDialogProps) {
  const { api, authentication } = useAuth();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const lineage = useQuery({
    queryKey: ["imported-thread-lineage", threadId, authentication],
    queryFn: ({ signal }) => api.getImportedThreadLineage(threadId, signal),
    enabled: open,
    retry: false,
  });
  const importedLineage = lineage.data && (lineage.data.thread_count ?? 0) > 1
    ? lineage.data
    : null;
  const scopeError = lineage.error instanceof ApiError && lineage.error.status === 404
    ? null
    : lineage.error;

  const remove = useMutation({
    mutationFn: async () => {
      if (importedLineage) {
        const result = await api.deleteImportedThreadLineage(threadId);
        return {
          deletedCount: result.deleted_count,
          deletedThreadIds: result.deleted_thread_ids,
          lineage: true,
        };
      }
      await api.deleteThread(threadId);
      return { deletedCount: 1, deletedThreadIds: [threadId], lineage: false };
    },
    onSuccess: (result) => onDeleted(result),
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const pending = lineage.isPending || remove.isPending;
  const count = importedLineage?.thread_count ?? 1;
  const title = importedLineage ? "Delete imported lineage?" : "Delete conversation?";

  return (
    <dialog
      ref={dialogRef}
      className="thread-delete-dialog"
      aria-labelledby="thread-delete-title"
      onCancel={(event) => {
        if (pending) event.preventDefault();
        else onClose();
      }}
      onClose={onClose}
    >
      <header className="thread-delete-heading">
        <div><p className="eyebrow">Permanent deletion</p><h2 id="thread-delete-title">{title}</h2></div>
        <button type="button" aria-label="Close thread deletion" disabled={pending} onClick={onClose}>×</button>
      </header>

      <div className="thread-delete-body">
        {lineage.isPending ? (
          <p className="thread-delete-checking" role="status">Checking whether this conversation belongs to an imported lineage…</p>
        ) : scopeError ? (
          <p className="thread-delete-error" role="alert">
            Could not determine the deletion scope. Nothing has been deleted.
          </p>
        ) : (
          <>
            <p>
              {importedLineage
                ? <>The conversation <strong>“{threadTitle}”</strong> belongs to an imported lineage containing <strong>{count} conversations</strong>. The server requires deleting the complete imported lineage together.</>
                : <>Delete <strong>“{threadTitle}”</strong> permanently?</>}
            </p>
            <p className="thread-delete-warning">
              {importedLineage
                ? "Every conversation in this imported lineage, including retained messages, context, and attachments, will be removed."
                : "Its retained messages, context, and attachments will be removed."}
              {" "}This action cannot be undone.
            </p>
          </>
        )}

        {remove.isError && (
          <p className="thread-delete-error" role="alert">
            {remove.error instanceof Error ? remove.error.message : "Could not delete the conversation."}
          </p>
        )}
      </div>

      <footer className="thread-delete-actions">
        <button type="button" disabled={pending} onClick={onClose}>Cancel</button>
        <button
          type="button"
          className="thread-delete-confirm"
          disabled={pending || Boolean(scopeError)}
          onClick={() => remove.mutate()}
        >
          {remove.isPending
            ? "Deleting…"
            : importedLineage
              ? `Delete all ${count} conversations`
              : "Delete conversation"}
        </button>
      </footer>
    </dialog>
  );
}
