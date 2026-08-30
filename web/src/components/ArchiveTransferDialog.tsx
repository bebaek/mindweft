import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  type ArchiveImportOptions,
  type ArchiveImportResponse,
  type PortableThreadArchive,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

const THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive";
const LINEAGE_ARCHIVE_SCHEMA = "mindweft.thread-lineage-archive";

type ExportKind = "thread" | "lineage";

interface ArchiveTransferDialogProps {
  open: boolean;
  threadId: string | null;
  threadTitle?: string | null;
  onClose: () => void;
  onImported: (threadId: string, result: ArchiveImportResponse) => void;
}

function archiveFileName(
  threadTitle: string | null | undefined,
  threadId: string,
  kind: ExportKind,
): string {
  const base = (threadTitle || threadId)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "thread";
  return `${base}.${kind === "lineage" ? "lineage." : ""}mindweft.json`;
}

function downloadArchiveJson(payload: PortableThreadArchive, filename: string): void {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function readArchiveFile(file: File): Promise<{
  payload: PortableThreadArchive;
  lineage: boolean;
}> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await file.text());
  } catch (error) {
    if (error instanceof SyntaxError) throw new Error("The selected file is not valid JSON.");
    throw error;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("The selected archive must contain a JSON object.");
  }
  const payload = parsed as PortableThreadArchive;
  if (payload.schema === THREAD_ARCHIVE_SCHEMA) return { payload, lineage: false };
  if (payload.schema === LINEAGE_ARCHIVE_SCHEMA) return { payload, lineage: true };
  const schema = typeof payload.schema === "string" ? payload.schema : "missing";
  throw new Error(`Unsupported archive schema: ${schema}.`);
}

export function ArchiveTransferDialog({
  open,
  threadId,
  threadTitle,
  onClose,
  onImported,
}: ArchiveTransferDialogProps) {
  const { api } = useAuth();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [profilePolicy, setProfilePolicy] = useState<ArchiveImportOptions["profilePolicy"]>("available");
  const [organizationPolicy, setOrganizationPolicy] = useState<ArchiveImportOptions["organizationPolicy"]>("reset");
  const [timestampPolicy, setTimestampPolicy] = useState<ArchiveImportOptions["timestampPolicy"]>("reset");
  const [dryRun, setDryRun] = useState(false);
  const [result, setResult] = useState<ArchiveImportResponse | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const exportArchive = useMutation({
    mutationFn: async (kind: ExportKind) => {
      if (!threadId) throw new Error("Select a conversation before exporting it.");
      const payload = kind === "thread"
        ? await api.exportThreadArchive(threadId)
        : await api.exportThreadLineageArchive(threadId);
      return { kind, payload };
    },
    onSuccess: ({ kind, payload }) => {
      downloadArchiveJson(payload, archiveFileName(threadTitle, threadId!, kind));
      setLocalError(null);
    },
    onError: (error) => {
      setLocalError(error instanceof Error ? error.message : "Could not export the archive.");
    },
  });

  const importArchive = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a Mindweft archive file first.");
      const archive = await readArchiveFile(file);
      const options: ArchiveImportOptions = {
        profilePolicy,
        organizationPolicy,
        timestampPolicy,
        dryRun,
      };
      return archive.lineage
        ? await api.importThreadLineageArchive(archive.payload, options)
        : await api.importThreadArchive(archive.payload, options);
    },
    onSuccess: (response) => {
      setLocalError(null);
      setResult(response);
      if (response.dry_run) return;
      const importedThreadId = response.requested_thread_id ?? response.thread_id;
      if (!importedThreadId) {
        setLocalError("The server did not return the restored conversation ID.");
        return;
      }
      onImported(importedThreadId, response);
    },
    onError: (error) => {
      setResult(null);
      setLocalError(error instanceof Error ? error.message : "Could not import the archive.");
    },
  });

  function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setResult(null);
    setLocalError(null);
    importArchive.mutate();
  }

  const pending = exportArchive.isPending || importArchive.isPending;

  return (
    <dialog
      ref={dialogRef}
      className="archive-transfer-dialog"
      aria-labelledby="archive-transfer-title"
      onCancel={onClose}
      onClose={onClose}
    >
      <header className="archive-transfer-heading">
        <div>
          <p className="eyebrow">Portable backup</p>
          <h2 id="archive-transfer-title">Transfer conversations</h2>
          <p>Download a checksummed archive or restore one into new destination conversations.</p>
        </div>
        <button type="button" aria-label="Close archive transfer" onClick={onClose}>×</button>
      </header>

      <section className="archive-transfer-section" aria-labelledby="archive-export-title">
        <div>
          <h3 id="archive-export-title">Export</h3>
          <p>Archive files can include message history, context, configuration, and attachment bytes. Treat them as sensitive.</p>
        </div>
        <div className="archive-export-actions">
          <button
            type="button"
            disabled={!threadId || pending}
            onClick={() => exportArchive.mutate("thread")}
          >
            Download this conversation
          </button>
          <button
            type="button"
            disabled={!threadId || pending}
            onClick={() => exportArchive.mutate("lineage")}
          >
            Download complete lineage
          </button>
        </div>
        {!threadId && <p className="archive-transfer-hint">Select a conversation to enable export.</p>}
      </section>

      <form className="archive-transfer-section archive-import-form" onSubmit={submitImport}>
        <div>
          <h3>Import</h3>
          <p>Import creates new conversation IDs and never overwrites the source conversation.</p>
        </div>
        <label className="archive-file-field">
          Archive file
          <input
            type="file"
            accept="application/json,.json"
            required
            disabled={pending}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setResult(null);
              setLocalError(null);
            }}
          />
        </label>
        <div className="archive-policy-grid">
          <label>
            Execution profiles
            <select
              value={profilePolicy}
              disabled={pending}
              onChange={(event) => setProfilePolicy(event.target.value as ArchiveImportOptions["profilePolicy"])}
            >
              <option value="available">Restore when available</option>
              <option value="defaults">Use destination defaults</option>
              <option value="strict">Require exact matches</option>
            </select>
          </label>
          <label>
            Organization
            <select
              value={organizationPolicy}
              disabled={pending}
              onChange={(event) => setOrganizationPolicy(event.target.value as ArchiveImportOptions["organizationPolicy"])}
            >
              <option value="reset">Reset pin/archive state</option>
              <option value="preserve">Preserve source state</option>
            </select>
          </label>
          <label>
            Timestamps
            <select
              value={timestampPolicy}
              disabled={pending}
              onChange={(event) => setTimestampPolicy(event.target.value as ArchiveImportOptions["timestampPolicy"])}
            >
              <option value="reset">Use current time</option>
              <option value="preserve">Preserve source times</option>
            </select>
          </label>
        </div>
        <label className="archive-dry-run">
          <input
            type="checkbox"
            checked={dryRun}
            disabled={pending}
            onChange={(event) => setDryRun(event.target.checked)}
          />
          Validate only; do not retain restored conversations
        </label>
        <button type="submit" className="archive-import-submit" disabled={!file || pending}>
          {importArchive.isPending ? "Checking archive…" : dryRun ? "Validate archive" : "Import archive"}
        </button>
      </form>

      {localError && <p className="archive-transfer-error" role="alert">{localError}</p>}
      {result && (
        <section className="archive-transfer-result" role="status">
          <strong>{result.dry_run ? "Archive validation passed." : "Archive imported."}</strong>
          <span>
            {result.thread_count ?? 1} conversation{(result.thread_count ?? 1) === 1 ? "" : "s"}, {result.message_count} messages, {result.attachment_count} attachments
          </span>
          {result.warnings.length > 0 && (
            <ul>{result.warnings.map((warning, index) => <li key={`${warning.code}-${index}`}>{warning.message}</li>)}</ul>
          )}
        </section>
      )}

      <footer className="archive-transfer-footer">
        <button type="button" disabled={pending} onClick={onClose}>{result ? "Done" : "Cancel"}</button>
      </footer>
    </dialog>
  );
}
