// Keep transient authorization codes in memory only, and remove them before requests.
export let pendingOAuthReturn: { state: string; code: string } | null = null;
export let oauthReturnError: string | null = null;

export function clearOAuthReturn() { pendingOAuthReturn = null; oauthReturnError = null; }
export function captureOAuthReturn() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  if (!fragment.has("oauth_state") && !fragment.has("oauth_error")) return;
  const state = fragment.get("oauth_state"), code = fragment.get("oauth_code");
  clearOAuthReturn();
  if (state && code) pendingOAuthReturn = { state, code };
  else oauthReturnError = "Provider sign-in failed or was cancelled. Restart it in tenant settings.";
  for (const key of ["oauth_state", "oauth_code", "oauth_error"]) fragment.delete(key);
  window.history.replaceState(null, "", window.location.pathname + window.location.search + (fragment.size ? "#" + fragment.toString() : ""));
}
