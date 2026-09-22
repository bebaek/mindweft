import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/auth-context";

export function OAuthProfileBindings({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { api, authentication } = useAuth();
  const connections = useQuery({ queryKey: ["oauth-connections", authentication], queryFn: () => api.listOAuthConnections(), retry: false });
  let profiles: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    profiles = parsed as Record<string, unknown>;
  } catch { return null; }
  return <div>{Object.entries(profiles).map(([name, raw]) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const profile = raw as Record<string, unknown>;
    if (profile.provider !== "generic-oauth") return null;
    const rawRef = profile.oauth_connection_ref ?? profile.oauthConnectionRef;
    const ref = typeof rawRef === "string" ? rawRef : "";
    return <label key={name}>OAuth connection for {name}<select value={ref} disabled={connections.isPending || Boolean(connections.error)} onChange={(event) => {
      const updated = { ...profile };
      delete updated.oauthConnectionRef;
      if (event.target.value) updated.oauth_connection_ref = event.target.value;
      else delete updated.oauth_connection_ref;
      onChange(JSON.stringify({ ...profiles, [name]: updated }, null, 2));
    }}><option value="">Legacy default connection</option>{ref && !connections.data?.items.some((connection) => connection.ref === ref) && <option value={ref}>{ref}</option>}{connections.data?.items.map((connection) => <option key={connection.name} value={connection.ref}>{connection.name} · {connection.provider_id}</option>)}</select></label>;
  })}{connections.error && <p role="alert">Named OAuth connection options are unavailable. Existing profile references are preserved.</p>}</div>;
}
