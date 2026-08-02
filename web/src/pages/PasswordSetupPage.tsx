import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, MinigentApiClient } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function PasswordSetupPage({ token }: { token: string }) {
  const { completePasswordSetup } = useAuth();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const setup = useQuery({
    queryKey: ["password-setup", token],
    queryFn: () => new MinigentApiClient({ mode: "session" }).getPasswordSetupStatus(token),
    retry: false,
  });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (password !== confirmation) {
      setError("Passwords do not match.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      await completePasswordSetup(token, password);
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    } catch (caught) {
      setError(caught instanceof ApiError || caught instanceof Error ? caught.message : "Password setup failed");
    } finally {
      setPending(false);
    }
  }

  const invalid = setup.isError || (setup.data && !setup.data.valid);
  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="password-setup-title">
        <div className="login-brand"><span className="brand-mark">M</span><div><strong>Minigent</strong><small>Agent operations</small></div></div>
        <div>
          <p className="eyebrow">Account activation</p>
          <h1 id="password-setup-title">Choose a password</h1>
          {setup.isPending && <p>Checking your setup link…</p>}
          {setup.data?.valid && <p>Set the password for <strong>{setup.data.username}</strong>. Use at least 12 characters.</p>}
          {invalid && <p className="login-error" role="alert">This password setup link is invalid, expired, or already used.</p>}
        </div>
        {setup.data?.valid && (
          <form onSubmit={(event) => { void submit(event); }}>
            <label>New password<input autoComplete="new-password" minLength={12} required type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
            <label>Confirm password<input autoComplete="new-password" minLength={12} required type="password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
            {error && <p className="login-error" role="alert">{error}</p>}
            <button type="submit" disabled={pending}>{pending ? "Activating…" : "Activate account"}</button>
          </form>
        )}
      </section>
    </main>
  );
}
