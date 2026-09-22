import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";
import { pendingOAuthReturn, oauthReturnError, clearOAuthReturn } from "../auth/oauthReturn";

export function OAuthReturnBanner() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [visible, setVisible] = useState(Boolean(pendingOAuthReturn || oauthReturnError));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(oauthReturnError ?? "Provider sign-in returned. Sign in to Mindweft as the initiating tenant manager, then finish connecting.");
  async function finish() {
    if (!pendingOAuthReturn || busy) return;
    setBusy(true);
    try {
      const result = await api.completeOAuthConnection(pendingOAuthReturn.state, pendingOAuthReturn.code);
      clearOAuthReturn();
      setMessage(`Connected ${result.name}. Credentials were stored; model access has not been verified.`);
      await queryClient.invalidateQueries({ queryKey: ["oauth-connections"] });
    } catch {
      setMessage("Could not complete OAuth. Check that you are signed in as the initiating tenant manager, or restart sign-in in tenant settings.");
    } finally { setBusy(false); }
  }
  if (!visible) return null;
  return <section aria-label="OAuth connection return"><p role="status">{message}</p>{pendingOAuthReturn && <button type="button" disabled={busy} onClick={() => void finish()}>Finish OAuth connection</button>}<button type="button" disabled={busy} onClick={() => { clearOAuthReturn(); setVisible(false); }}>Dismiss</button></section>;
}
