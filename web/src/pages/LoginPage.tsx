import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/auth-context";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(username.trim(), password);
      setPassword("");
    } catch (caught) {
      setError(caught instanceof ApiError || caught instanceof Error ? caught.message : "Sign-in failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand"><span className="brand-mark">M</span><div><strong>Mindweft</strong><small>Agent operations</small></div></div>
        <div>
          <p className="eyebrow">Secure administration</p>
          <h1 id="login-title">Sign in</h1>
          <p>Use the credentials configured by your Mindweft administrator.</p>
        </div>
        <form onSubmit={(event) => { void submit(event); }}>
          <label>Username<input autoComplete="username" autoFocus required value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>Password<input autoComplete="current-password" required type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          {error && <p className="login-error" role="alert">{error}</p>}
          <button type="submit" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
        </form>
      </section>
    </main>
  );
}
