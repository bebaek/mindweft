import { useEffect, useRef, useState } from "react";
import type { PrivateValueConsentRequest } from "../api/client";
import { useAuth } from "../auth/auth-context";

interface ConsentDialogProps {
  threadId: string | null;
  request: PrivateValueConsentRequest | null;
  onResolved: (result: { decision: "approved" | "denied" | "discarded"; reply?: string }) => void;
  onError: (message: string) => void;
}

type ConsentState = "idle" | "approving" | "denying" | "uncertain" | "discarding";

export function ConsentDialog({ threadId, request, onResolved, onError }: ConsentDialogProps) {
  const { api } = useAuth();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState<ConsentState>("idle");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (request && !dialog.open) dialog.showModal();
    if (!request && dialog.open) dialog.close();
  }, [request]);

  if (!request || !threadId) return <dialog ref={dialogRef} />;

  async function deny() {
    setState("denying");
    try {
      await api.decidePrivateValueConsent(threadId!, request!.consent_id, false);
      onResolved({ decision: "denied" });
      setState("idle");
    } catch (caught) {
      setState("idle");
      onError(errorMessage(caught, "Could not deny the disclosure request"));
    }
  }

  async function approve() {
    setState("approving");
    try {
      await api.decidePrivateValueConsent(threadId!, request!.consent_id, true);
      try {
        const result = await api.resumePrivateValueConsent(threadId!, request!.consent_id);
        onResolved({ decision: "approved", reply: result.reply });
        setState("idle");
      } catch (caught) {
        const actions = await api.listPrivateValueActions(threadId!);
        const action = actions.find((item) => item.consent_id === request!.consent_id);
        if (action?.state === "executing") {
          setState("uncertain");
          return;
        }
        throw caught;
      }
    } catch (caught) {
      setState("idle");
      onError(errorMessage(caught, "Could not complete the approved action"));
    }
  }

  async function discard() {
    setState("discarding");
    try {
      await api.discardPrivateValueAction(threadId!, request!.consent_id);
      onResolved({ decision: "discarded" });
      setState("idle");
    } catch (caught) {
      setState("uncertain");
      onError(errorMessage(caught, "Could not discard the local action record"));
    }
  }

  const busy = state === "approving" || state === "denying" || state === "discarding";
  return (
    <dialog
      className="consent-dialog"
      ref={dialogRef}
      aria-labelledby="consent-title"
      onCancel={(event) => event.preventDefault()}
    >
      {state === "uncertain" || state === "discarding" ? (
        <div className="consent-content uncertain-action">
          <span className="consent-shield warning" aria-hidden="true">!</span>
          <p className="eyebrow">Action outcome unknown</p>
          <h2 id="consent-title">Check the external system before continuing.</h2>
          <p>
            <strong>{request.tool_name}</strong> was claimed and may have completed, but Mindweft
            could not confirm the result. Retrying could repeat an external side effect.
          </p>
          <div className="consent-notice">
            Discarding removes only Mindweft’s local action record. It does not undo an external action.
          </div>
          <div className="consent-actions single">
            <button type="button" className="consent-deny" disabled={busy} onClick={() => void discard()}>
              {state === "discarding" ? "Discarding…" : "I checked — discard local record"}
            </button>
          </div>
        </div>
      ) : (
        <div className="consent-content">
          <span className="consent-shield" aria-hidden="true">◇</span>
          <p className="eyebrow">Private-value disclosure</p>
          <h2 id="consent-title">Allow this exact tool action?</h2>
          <p>
            <strong>{request.tool_name}</strong> is requesting temporary access to the following
            private values. Approval applies once, to this exact tool call only.
          </p>
          <ul className="disclosure-list">
            {request.disclosures.length ? request.disclosures.map((disclosure) => (
              <li key={`${disclosure.path}:${disclosure.kind}`}>
                <span>{disclosure.kind}</span>
                <code>{disclosure.path}</code>
                <small>{disclosure.count} value{disclosure.count === 1 ? "" : "s"}</small>
              </li>
            )) : <li><span>Action</span><code>No private values disclosed</code><small>Approval still required</small></li>}
          </ul>
          <div className="consent-meta">
            <span>One-time approval</span>
            <span>Expires {formatExpiry(request.expires_at)}</span>
          </div>
          <div className="consent-actions">
            <button type="button" className="consent-deny" disabled={busy} onClick={() => void deny()}>
              {state === "denying" ? "Denying…" : "Deny"}
            </button>
            <button type="button" className="consent-approve" disabled={busy} onClick={() => void approve()}>
              {state === "approving" ? "Approving and running…" : "Approve once and continue"}
            </button>
          </div>
        </div>
      )}
    </dialog>
  );
}

function formatExpiry(timestamp: number) {
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "soon";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}
