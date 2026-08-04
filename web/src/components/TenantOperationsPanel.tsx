import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  AdminAuditFilters,
  AdminAuditRecord,
  AdminThreadDetail,
  AdminThreadFilters,
  AdminThreadPruneInput,
  AdminThreadPruneResult,
  Message,
  ThreadStatus,
} from "../api/client";
import { useAuth } from "../auth/auth-context";

const PAGE_SIZE = 10;

type OperationsTab = "threads" | "audit";

export function TenantOperationsPanel({ tenantId }: { tenantId: string }) {
  const [tab, setTab] = useState<OperationsTab>("threads");
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [pruning, setPruning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  return <section className="tenant-operations" aria-labelledby="tenant-operations-title">
    <header className="tenant-operations-heading">
      <div><p className="eyebrow">Operations</p><h3 id="tenant-operations-title">Threads and audit activity</h3><p>Inspect runtime conversations, remove stale data, and review administrative changes.</p></div>
      {tab === "threads" && <button type="button" onClick={() => { setNotice(null); setPruning(true); }}>Prune threads</button>}
    </header>
    <nav className="tenant-operations-tabs" aria-label="Tenant operations"><button type="button" className={tab === "threads" ? "active" : ""} onClick={() => setTab("threads")}>Threads</button><button type="button" className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>Audit log</button></nav>
    {notice && <p className="operations-notice" role="status">{notice}</p>}
    {tab === "threads" ? <ThreadBrowser tenantId={tenantId} onInspect={setSelectedThreadId} /> : <AuditBrowser tenantId={tenantId} />}
    {selectedThreadId && <ThreadDetailDialog tenantId={tenantId} threadId={selectedThreadId} onClose={() => setSelectedThreadId(null)} onDeleted={() => { setNotice("Thread deleted and recorded in the audit log."); setSelectedThreadId(null); }} />}
    {pruning && <PruneThreadsDialog tenantId={tenantId} onClose={() => setPruning(false)} onPruned={(result) => { setNotice(`${String(result.deleted_count)} ${result.deleted_count === 1 ? "thread" : "threads"} deleted and recorded in the audit log.`); setPruning(false); }} />}
  </section>;
}

function ThreadBrowser({ tenantId, onInspect }: { tenantId: string; onInspect: (threadId: string) => void }) {
  const { api, authentication } = useAuth();
  const [draft, setDraft] = useState({ status: "", profile: "", skill: "", updatedAfter: "" });
  const [filters, setFilters] = useState<AdminThreadFilters>({ limit: PAGE_SIZE, offset: 0 });
  const threads = useQuery({
    queryKey: ["admin-tenant-threads", tenantId, authentication, filters],
    queryFn: ({ signal }) => api.getAdminTenantThreads(tenantId, filters, signal),
  });

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    setFilters({ limit: PAGE_SIZE, offset: 0, status: draft.status as ThreadStatus | "", profile: draft.profile.trim(), skill: draft.skill.trim(), updated_after: localDateToIso(draft.updatedAfter) });
  }

  return <div className="thread-browser">
    <form className="operations-filters" onSubmit={applyFilters}>
      <label>Status<select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })}><option value="">All statuses</option><option value="idle">Idle</option><option value="running">Running</option><option value="error">Error</option></select></label>
      <label>Capability profile<input value={draft.profile} onChange={(event) => setDraft({ ...draft, profile: event.target.value })} placeholder="Any profile" /></label>
      <label>Skill<input value={draft.skill} onChange={(event) => setDraft({ ...draft, skill: event.target.value })} placeholder="Any skill" /></label>
      <label>Updated after<input type="datetime-local" value={draft.updatedAfter} onChange={(event) => setDraft({ ...draft, updatedAfter: event.target.value })} /></label>
      <button type="submit">Apply filters</button>
      <button type="button" className="subtle" onClick={() => { setDraft({ status: "", profile: "", skill: "", updatedAfter: "" }); setFilters({ limit: PAGE_SIZE, offset: 0 }); }}>Clear</button>
    </form>
    {threads.isPending && <p className="operations-loading">Loading tenant threads…</p>}
    {threads.isError && <p className="inline-error" role="alert">{errorMessage(threads.error)}</p>}
    {threads.data && <>
      <div className="operations-result-heading"><strong>{String(threads.data.total)} {threads.data.total === 1 ? "thread" : "threads"}</strong><span>Newest activity first</span></div>
      {threads.data.threads.length === 0 ? <p className="operations-empty">No threads match these filters.</p> : <div className="thread-table">{threads.data.threads.map((thread) => <button type="button" key={thread.thread_id} onClick={() => onInspect(thread.thread_id)}><span className={`thread-status-dot ${thread.status}`} aria-label={thread.status} /><span className="thread-primary"><strong>{shortId(thread.thread_id)}</strong><small>{thread.thread_id}</small></span><span><strong>{String(thread.message_count)}</strong><small>messages</small></span><span><strong>{thread.capability_profile || "Default"}</strong><small>{thread.skill_names?.join(", ") || thread.skill_name || "No skill"}</small></span><time dateTime={thread.updated_at}>{formatDate(thread.updated_at)}</time><span className="thread-open" aria-hidden="true">›</span></button>)}</div>}
      <Pagination offset={threads.data.offset} limit={threads.data.limit} total={threads.data.total} nextOffset={threads.data.next_offset} onPage={(offset) => setFilters({ ...filters, offset })} />
    </>}
  </div>;
}

function AuditBrowser({ tenantId }: { tenantId: string }) {
  const { api, authentication } = useAuth();
  const [draft, setDraft] = useState({ action: "", actor: "", after: "", before: "" });
  const [filters, setFilters] = useState<AdminAuditFilters>({ limit: PAGE_SIZE, offset: 0 });
  const [expanded, setExpanded] = useState<string | null>(null);
  const audit = useQuery({
    queryKey: ["admin-tenant-audit", tenantId, authentication, filters],
    queryFn: ({ signal }) => api.getAdminTenantAuditRecords(tenantId, filters, signal),
  });

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    setFilters({ limit: PAGE_SIZE, offset: 0, action: draft.action.trim(), actor: draft.actor.trim(), created_after: localDateToIso(draft.after), created_before: localDateToIso(draft.before) });
  }

  return <div className="audit-browser">
    <form className="operations-filters audit" onSubmit={applyFilters}>
      <label>Action<input value={draft.action} onChange={(event) => setDraft({ ...draft, action: event.target.value })} placeholder="For example, threads.delete" /></label>
      <label>Actor<input value={draft.actor} onChange={(event) => setDraft({ ...draft, actor: event.target.value })} placeholder="User ID" /></label>
      <label>Created after<input type="datetime-local" value={draft.after} onChange={(event) => setDraft({ ...draft, after: event.target.value })} /></label>
      <label>Created before<input type="datetime-local" value={draft.before} onChange={(event) => setDraft({ ...draft, before: event.target.value })} /></label>
      <button type="submit">Apply filters</button><button type="button" className="subtle" onClick={() => { setDraft({ action: "", actor: "", after: "", before: "" }); setFilters({ limit: PAGE_SIZE, offset: 0 }); }}>Clear</button>
    </form>
    {audit.isPending && <p className="operations-loading">Loading audit records…</p>}
    {audit.isError && <p className="inline-error" role="alert">{errorMessage(audit.error)}</p>}
    {audit.data && <>
      <div className="operations-result-heading"><strong>{String(audit.data.total)} audit {audit.data.total === 1 ? "record" : "records"}</strong><span>Most recent first</span></div>
      {audit.data.audit_records.length === 0 ? <p className="operations-empty">No audit records match these filters.</p> : <div className="audit-list">{audit.data.audit_records.map((record) => <AuditRow key={record.audit_id} record={record} expanded={expanded === record.audit_id} onToggle={() => setExpanded(expanded === record.audit_id ? null : record.audit_id)} />)}</div>}
      <Pagination offset={audit.data.offset} limit={audit.data.limit} total={audit.data.total} nextOffset={audit.data.next_offset} onPage={(offset) => setFilters({ ...filters, offset })} />
    </>}
  </div>;
}

function AuditRow({ record, expanded, onToggle }: { record: AdminAuditRecord; expanded: boolean; onToggle: () => void }) {
  const hasDetails = Boolean(record.old_values || record.new_values || record.metadata || record.thread_ids.length);
  return <article className="audit-row"><button type="button" onClick={onToggle} aria-expanded={expanded} disabled={!hasDetails}><span className="audit-action-icon">{auditIcon(record.action)}</span><span><strong>{humanizeAction(record.action)}</strong><small>{record.action}</small></span><span><strong>{record.actor_user_id}</strong><small>Actor</small></span><span><strong>{String(record.affected_count)}</strong><small>Affected</small></span><time dateTime={record.created_at}>{formatDate(record.created_at)}</time>{hasDetails && <span aria-hidden="true">{expanded ? "⌃" : "⌄"}</span>}</button>{expanded && <div className="audit-details">{record.resource_type && <p><strong>Resource</strong> {record.resource_type}{record.resource_id ? ` · ${record.resource_id}` : ""}</p>}{record.thread_ids.length > 0 && <div><strong>Thread IDs</strong><code>{record.thread_ids.join("\n")}</code></div>}{record.old_values && <JsonDetail label="Previous values" value={record.old_values} />}{record.new_values && <JsonDetail label="New values" value={record.new_values} />}{record.metadata && <JsonDetail label="Metadata" value={record.metadata} />}</div>}</article>;
}

function ThreadDetailDialog({ tenantId, threadId, onClose, onDeleted }: { tenantId: string; threadId: string; onClose: () => void; onDeleted: () => void }) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const dialogRef = useModalDialog();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const detail = useQuery({ queryKey: ["admin-tenant-thread", tenantId, threadId, authentication], queryFn: ({ signal }) => api.getAdminTenantThread(tenantId, threadId, signal) });
  const deletion = useMutation({
    mutationFn: () => api.deleteAdminTenantThread(tenantId, threadId),
    onSuccess: async () => {
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["admin-tenant-threads", tenantId] }), queryClient.invalidateQueries({ queryKey: ["admin-tenant-audit", tenantId] })]);
      onDeleted();
    },
  });
  return <dialog ref={dialogRef} className="admin-dialog thread-detail-dialog" aria-labelledby="thread-detail-title" onCancel={onClose} onClose={onClose}><div>
    <header className="dialog-heading"><div><p className="eyebrow">Thread inspection</p><h2 id="thread-detail-title">{shortId(threadId)}</h2><code>{threadId}</code></div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header>
    {detail.isPending && <p className="operations-loading">Loading thread details…</p>}
    {detail.isError && <p className="dialog-error" role="alert">{errorMessage(detail.error)}</p>}
    {detail.data && <ThreadDetailContent detail={detail.data} />}
    {confirmDelete && <div className="thread-delete-confirm" role="alertdialog" aria-label="Delete thread confirmation"><div><strong>Delete this thread permanently?</strong><span>Its messages and compacted context will be removed. An audit record will remain.</span></div><button type="button" onClick={() => setConfirmDelete(false)}>Cancel</button><button type="button" className="danger" disabled={deletion.isPending} onClick={() => deletion.mutate()}>{deletion.isPending ? "Deleting…" : "Delete thread"}</button></div>}
    {deletion.isError && <p className="dialog-error" role="alert">{errorMessage(deletion.error)}</p>}
    <div className="dialog-actions"><button type="button" className="button button-danger" onClick={() => setConfirmDelete(true)} disabled={detail.isPending || confirmDelete}>Delete thread</button><button type="button" className="button button-secondary" onClick={onClose}>Close</button></div>
  </div></dialog>;
}

function ThreadDetailContent({ detail }: { detail: AdminThreadDetail }) {
  return <div className="thread-detail-content"><div className="thread-detail-metrics"><Metric label="Status" value={detail.status} /><Metric label="Messages" value={String(detail.message_count)} /><Metric label="Profile" value={detail.capability_profile || "Default"} /><Metric label="Updated" value={formatDate(detail.updated_at)} /></div><section className="thread-context"><div><h3>Compacted context</h3><span>{String(detail.context.summarized_message_count)} summarized messages</span></div><p>{detail.context.summary || "No compacted summary has been created."}</p></section><section className="thread-messages"><header><h3>Retained messages</h3><span>{String(detail.messages.length)} shown</span></header>{detail.messages.length === 0 ? <p>No retained messages.</p> : detail.messages.map((message) => <MessageInspection key={message.id} message={message} />)}</section></div>;
}

function MessageInspection({ message }: { message: Message }) {
  return <article className={`inspected-message ${message.role}`}><header><strong>{message.tool_name ? `${message.role} · ${message.tool_name}` : message.role}</strong><time dateTime={message.created_at}>{formatDate(message.created_at)}</time></header><pre>{message.content || "(No text content)"}</pre>{message.parts && message.parts.length > 0 && <small>{String(message.parts.length)} structured content {message.parts.length === 1 ? "part" : "parts"}</small>}{message.tool_arguments && <details><summary>Tool arguments</summary><code>{JSON.stringify(message.tool_arguments, null, 2)}</code></details>}{message.metadata && <details><summary>Message metadata</summary><code>{JSON.stringify(message.metadata, null, 2)}</code></details>}</article>;
}

function PruneThreadsDialog({ tenantId, onClose, onPruned }: { tenantId: string; onClose: () => void; onPruned: (result: AdminThreadPruneResult) => void }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const dialogRef = useModalDialog();
  const [cutoff, setCutoff] = useState(defaultCutoff());
  const [status, setStatus] = useState("");
  const [profile, setProfile] = useState("");
  const [skill, setSkill] = useState("");
  const [preview, setPreview] = useState<AdminThreadPruneResult | null>(null);
  const previewMutation = useMutation({ mutationFn: (input: AdminThreadPruneInput) => api.pruneAdminTenantThreads(tenantId, { ...input, dry_run: true }), onSuccess: setPreview });
  const pruneMutation = useMutation({ mutationFn: (input: AdminThreadPruneInput) => api.pruneAdminTenantThreads(tenantId, { ...input, dry_run: false }), onSuccess: async (result) => { await Promise.all([queryClient.invalidateQueries({ queryKey: ["admin-tenant-threads", tenantId] }), queryClient.invalidateQueries({ queryKey: ["admin-tenant-audit", tenantId] })]); onPruned(result); } });
  const input = (): AdminThreadPruneInput => ({ updated_before: requiredLocalDateToIso(cutoff), status: status as ThreadStatus | "", profile: profile.trim(), skill: skill.trim() });
  function submitPreview(event: FormEvent) { event.preventDefault(); setPreview(null); previewMutation.mutate(input()); }
  return <dialog ref={dialogRef} className="admin-dialog prune-dialog" aria-labelledby="prune-title" onCancel={onClose} onClose={onClose}><form onSubmit={submitPreview}><header className="dialog-heading"><div><p className="eyebrow">Data retention</p><h2 id="prune-title">Prune stale threads</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header><p>Preview tenant-scoped candidates before permanently deleting threads and their retained messages. Times are interpreted in your local timezone.</p><div className="prune-fields"><label>Not updated since<input required type="datetime-local" value={cutoff} onChange={(event) => { setCutoff(event.target.value); setPreview(null); }} /></label><label>Status<select value={status} onChange={(event) => { setStatus(event.target.value); setPreview(null); }}><option value="">Any status</option><option value="idle">Idle</option><option value="running">Running</option><option value="error">Error</option></select></label><label>Capability profile<input value={profile} onChange={(event) => { setProfile(event.target.value); setPreview(null); }} placeholder="Any profile" /></label><label>Skill<input value={skill} onChange={(event) => { setSkill(event.target.value); setPreview(null); }} placeholder="Any skill" /></label></div>{preview && <div className={`prune-preview ${preview.candidate_thread_ids.length ? "has-candidates" : ""}`} role="status"><strong>{String(preview.candidate_thread_ids.length)} {preview.candidate_thread_ids.length === 1 ? "candidate" : "candidates"}</strong><span>{preview.candidate_thread_ids.length ? "Review the matching IDs before confirming deletion." : "No threads currently match these criteria."}</span>{preview.candidate_thread_ids.length > 0 && <details><summary>Candidate thread IDs</summary><code>{preview.candidate_thread_ids.join("\n")}</code></details>}</div>}{(previewMutation.isError || pruneMutation.isError) && <p className="dialog-error" role="alert">{errorMessage(previewMutation.error || pruneMutation.error)}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>Cancel</button><button type="submit" className="button button-secondary" disabled={previewMutation.isPending || pruneMutation.isPending}>{previewMutation.isPending ? "Checking…" : "Preview candidates"}</button>{preview && preview.candidate_thread_ids.length > 0 && <button type="button" className="button button-danger" disabled={pruneMutation.isPending} onClick={() => pruneMutation.mutate(input())}>{pruneMutation.isPending ? "Deleting…" : `Delete ${String(preview.candidate_thread_ids.length)} ${preview.candidate_thread_ids.length === 1 ? "thread" : "threads"}`}</button>}</div></form></dialog>;
}

function Pagination({ offset, limit, total, nextOffset, onPage }: { offset: number; limit: number; total: number; nextOffset?: number | null; onPage: (offset: number) => void }) {
  if (total <= limit) return null;
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + limit, total);
  return <nav className="operations-pagination" aria-label="Results pages"><span>{String(start)}–{String(end)} of {String(total)}</span><button type="button" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}>Previous</button><button type="button" disabled={nextOffset === null || nextOffset === undefined} onClick={() => nextOffset !== null && nextOffset !== undefined && onPage(nextOffset)}>Next</button></nav>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div>; }
function JsonDetail({ label, value }: { label: string; value: Record<string, unknown> }) { return <div><strong>{label}</strong><code>{JSON.stringify(value, null, 2)}</code></div>; }
function shortId(value: string) { return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value; }
function formatDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date); }
function localDateToIso(value: string) { return value ? new Date(value).toISOString() : undefined; }
function requiredLocalDateToIso(value: string) { const iso = localDateToIso(value); if (!iso) throw new Error("A cutoff date is required."); return iso; }
function defaultCutoff() { const date = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000); const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000); return local.toISOString().slice(0, 16); }
function auditIcon(action: string) { return action.includes("delete") || action.includes("prune") ? "−" : action.includes("create") || action.includes("add") ? "+" : "·"; }
function humanizeAction(action: string) { return action.replaceAll("_", " ").replaceAll(".", " · ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "The request failed. No changes were applied."; }
function useModalDialog() { const ref = useRef<HTMLDialogElement>(null); useEffect(() => { const dialog = ref.current; if (dialog && !dialog.open) dialog.showModal(); }, []); return ref; }
