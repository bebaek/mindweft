import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AdminCredentialSetup, AdminTenantUser } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function CredentialSetupDialog({
  tenantId,
  user,
  onClose,
}: {
  tenantId: string;
  user: AdminTenantUser;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ["admin-user-credential", tenantId, user.id, authentication];
  const credential = useQuery({
    queryKey,
    queryFn: ({ signal }) => api.getAdminTenantUserCredential(tenantId, user.id, signal),
    retry: false,
  });
  const [username, setUsername] = useState((user.email || user.user_id).toLowerCase());
  const [setup, setSetup] = useState<AdminCredentialSetup | null>(null);
  const [copied, setCopied] = useState(false);
  const issue = useMutation({
    mutationFn: () => api.createAdminTenantUserCredentialSetup(tenantId, user.id, (credential.data?.username || username).trim()),
    onSuccess: (result) => {
      setSetup(result);
      setCopied(false);
    },
  });
  const disable = useMutation({
    mutationFn: () => api.disableAdminTenantUserCredential(tenantId, user.id),
    onSuccess: async () => {
      setSetup(null);
      await queryClient.invalidateQueries({ queryKey: ["admin-user-credential", tenantId, user.id] });
    },
  });

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    issue.mutate();
  }

  const setupUrl = setup
    ? `${window.location.origin}${window.location.pathname}#setup=${encodeURIComponent(setup.setup_token)}`
    : null;
  const error = issue.error || disable.error || credential.error;

  return (
    <dialog ref={dialogRef} className="admin-dialog credential-dialog" aria-labelledby="credential-title" onCancel={onClose} onClose={onClose}>
      <form onSubmit={submit}>
        <header className="dialog-heading"><div><p className="eyebrow">Local sign-in</p><h2 id="credential-title">Set up {user.display_name || user.user_id}</h2></div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}>×</button></header>
        <p className="credential-intro">{credential.data?.managed_externally ? "This bootstrap credential is managed by deployment configuration." : "Create a single-use link. The user chooses their password directly, and the link expires after 24 hours."}</p>
        <label className="field">Login username<input required disabled={Boolean(credential.data?.configured)} pattern="[A-Za-z0-9][A-Za-z0-9._@+\-]*" value={credential.data?.username || username} onChange={(event) => setUsername(event.target.value)} /></label>
        {credential.data?.configured && <p className="credential-status"><strong>{credential.data.disabled ? "Sign-in disabled" : "Sign-in configured"}</strong><span>{credential.data.username}</span></p>}
        {setupUrl && <div className="setup-link-result" role="status"><strong>Single-use setup link</strong><input readOnly value={setupUrl} aria-label="Password setup link" /><p>Expires {new Date(setup?.expires_at ?? "").toLocaleString()}. Creating another link invalidates this one.</p><button type="button" className="button button-secondary" onClick={() => { void navigator.clipboard.writeText(setupUrl).then(() => setCopied(true)); }}>{copied ? "Copied" : "Copy link"}</button></div>}
        {error && <p className="dialog-error" role="alert">{error instanceof Error ? error.message : "Credential operation failed"}</p>}
        <div className="dialog-actions">
          {credential.data?.configured && !credential.data.disabled && !credential.data.managed_externally && <button type="button" className="button button-danger" disabled={disable.isPending} onClick={() => disable.mutate()}>{disable.isPending ? "Disabling…" : "Disable sign-in"}</button>}
          <button type="button" className="button button-secondary" onClick={onClose}>Close</button>
          {!credential.data?.managed_externally && <button type="submit" className="button button-primary" disabled={issue.isPending || !username.trim()}>{issue.isPending ? "Creating…" : credential.data?.configured ? "Create reset link" : "Create setup link"}</button>}
        </div>
      </form>
    </dialog>
  );
}
