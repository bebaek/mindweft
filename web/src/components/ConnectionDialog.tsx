import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import type { Authentication } from "../api/client";

interface ConnectionDialogProps {
  authentication: Authentication;
  open: boolean;
  onClose: () => void;
  onSave: (authentication: Authentication) => void;
}

type Mode = Authentication["mode"];

export function ConnectionDialog({
  authentication,
  open,
  onClose,
  onSave,
}: ConnectionDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [mode, setMode] = useState<Mode>(authentication.mode);
  const [tenantId, setTenantId] = useState(
    authentication.mode === "development" ? authentication.tenantId : "tenant-1",
  );
  const [userId, setUserId] = useState(
    authentication.mode === "development" ? authentication.userId : "web-user",
  );
  const [isAdmin, setIsAdmin] = useState(
    authentication.mode === "development" && authentication.isAdmin,
  );
  const [token, setToken] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode === "development") {
      onSave({ mode, tenantId: tenantId.trim(), userId: userId.trim(), isAdmin });
    } else if (mode === "bearer") {
      onSave({ mode, token });
      setToken("");
    } else {
      onSave({ mode });
    }
    onClose();
  }

  return (
    <dialog
      className="connection-dialog"
      ref={dialogRef}
      aria-labelledby={titleId}
      onCancel={onClose}
      onClose={onClose}
    >
      <form method="dialog" onSubmit={submit}>
        <div className="dialog-heading">
          <div>
            <p className="eyebrow">Connection</p>
            <h2 id={titleId}>Authentication</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <fieldset className="mode-picker">
          <legend>Authentication mode</legend>
          <label>
            <input
              type="radio"
              name="mode"
              value="session"
              checked={mode === "session"}
              onChange={() => setMode("session")}
            />
            <span><strong>Secure session</strong><small>Recommended for production</small></span>
          </label>
          <label>
            <input
              type="radio"
              name="mode"
              value="development"
              checked={mode === "development"}
              onChange={() => setMode("development")}
            />
            <span><strong>Development headers</strong><small>Trusted local environments only</small></span>
          </label>
          <label>
            <input
              type="radio"
              name="mode"
              value="bearer"
              checked={mode === "bearer"}
              onChange={() => setMode("bearer")}
            />
            <span><strong>Bearer token</strong><small>Held in memory and never persisted</small></span>
          </label>
        </fieldset>

        {mode === "development" && (
          <div className="form-grid">
            <label>Tenant ID<input required value={tenantId} onChange={(event) => setTenantId(event.target.value)} /></label>
            <label>User ID<input required value={userId} onChange={(event) => setUserId(event.target.value)} /></label>
            <label className="checkbox-row"><input type="checkbox" checked={isAdmin} onChange={(event) => setIsAdmin(event.target.checked)} />Administrator</label>
          </div>
        )}
        {mode === "bearer" && (
          <label className="field">Bearer token<input required type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} /></label>
        )}
        {mode === "session" && (
          <p className="callout">Requests use same-origin cookies. The session endpoint will be connected when the production identity provider is selected.</p>
        )}

        <div className="dialog-actions">
          <button className="button button-secondary" type="button" onClick={onClose}>Cancel</button>
          <button className="button button-primary" type="submit">Use connection</button>
        </div>
      </form>
    </dialog>
  );
}
