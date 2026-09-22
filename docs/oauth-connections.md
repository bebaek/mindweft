# Named OAuth connections

Agents select LLM profiles; profiles can now select a **named OAuth connection**.
Multiple connections may use the same provider with different accounts, and several
profiles may reuse one connection. Definitions are deployment-owned; credentials are
tenant-scoped and managed by tenant owners/admins. Personal user-owned connections and
arbitrary provider endpoint editing through the browser are not included.

## Configuration

Named connections require encrypted SQLite OAuth storage. Configure
`MINDWEFT_OAUTH_STORE_PATH` and `MINDWEFT_OAUTH_ENCRYPTION_KEY` or the versioned
`MINDWEFT_OAUTH_ENCRYPTION_KEYS` using your existing secret-management mechanism.
Do not put encryption keys or tokens into the examples below. JSON credential stores
remain supported for legacy OAuth, but are intentionally rejected for named connections:
atomic disconnect/login/refresh coordination requires SQLite.

In your deployment or user-level `mindweft.toml`:

```toml
[oauth.connections.work]
provider_id = "openai-codex"
client_id = "registered-client-id"
authorize_url = "https://provider.example/authorize"
token_url = "https://provider.example/token"
redirect_uri = "http://127.0.0.1:8000/oauth/connections/callback"
scope = "openid offline_access"

[oauth.connections.personal]
provider_id = "openai-codex"
client_id = "registered-client-id"
authorize_url = "https://provider.example/authorize"
token_url = "https://provider.example/token"
redirect_uri = "http://127.0.0.1:8000/oauth/connections/callback"
scope = "openid offline_access"

[llm]
default = "coding-work"

[llm.providers.coding-work]
provider = "generic-oauth"
model = "provider-model-name"
url = "https://provider.example/responses"
oauth_connection_ref = "shared:work"

[llm.providers.personal-chat]
provider = "generic-oauth"
model = "provider-model-name"
url = "https://provider.example/responses"
oauth_connection_ref = "shared:personal"
```

These are placeholders, not usable provider endpoints. Use the provider's registered
public-client/PKCE settings. Authorization and token endpoints must be HTTPS. Redirects
must end in `/oauth/connections/callback`, use HTTPS or literal loopback HTTP, and match
the API's external origin. Configure trusted proxy handling at deployment level rather
than allowing callers to supply a redirect origin. Named coding instances on different
ports need their own registered redirect URI/configuration; login fails explicitly on a
port mismatch. Imports do not require a matching callback port.

`auth_params` and `account_id_jwt_claim` are optional. Core OAuth/PKCE parameters cannot
be overridden with `auth_params`. Connection definition keys accept snake_case or
camelCase. `oauthConnectionRef` is accepted for profile references. Environment-only
configuration can supply the definition map as `MINDWEFT_OAUTH_CONNECTIONS` JSON. The
single default profile can use `MINDWEFT_LLM_OAUTH_CONNECTION_REF`; named profiles use
`oauth_connection_ref` in `MINDWEFT_LLM_PROFILES` or tenant execution JSON. Restart after
changing deployment definitions.

## Console workflow

1. Open tenant settings → **Named OAuth connections**.
2. Choose **Connect** or **Reconnect** on the specific named connection. This navigates
   the current tab to the provider, preserving the coding console's tab-scoped session proof.
3. The provider returns through a transport-only callback. The console removes the
   authorization code/state fragment before other requests and keeps them only in memory.
4. Click **Finish OAuth connection**, authenticated as the same user and tenant that
   initiated login. If authentication has expired, sign in again first. Reloading or dismissing
   the return banner discards the code; restart provider sign-in when necessary.
5. In the execution editor's advanced section, use **OAuth connection for PROFILE** to
   bind a generic-OAuth LLM profile. Agents then select that profile normally.

For OpenAI Codex connections, **Import Pi credentials** supports either Pi's whole auth
JSON or an extracted `openai-codex` OAuth object. It transfers reusable credentials into
this tenant's encrypted store. Only import an account you are authorized to share with
the tenant. The UI clears the file input and does not persist credential contents in
browser storage. Imports for other provider IDs are rejected.

Status reports **stored credentials only**, including expiry/account metadata. Listing
never refreshes a token or calls a model/provider. Expiry does not itself prove revocation:
the runtime will attempt the normal refresh on use. Do not interpret “stored” as a live
provider availability or subscription check.

## Isolation and compatibility

- Credential keys bind tenant, connection name and the complete OAuth provider definition.
  A definition change requires reconnecting; it never sends an old refresh token to a new
  endpoint. Old encrypted records are not automatically deleted or migrated.
- Login state is encrypted, expiring, single-use and bound to initiating tenant/user,
  connection, configuration and a durable connection generation.
- Disconnect invalidates pending logins and deletes that connection's local credentials.
  A newer login/import supersedes older login attempts. In-flight login completion cannot
  resurrect a disconnected connection; existing version/lease checks coordinate refresh.
- Disconnect is local, not provider-side account revocation, and cannot cancel an already
  dispatched provider request. Revoke at the provider too when that is required.
- Named connections never fall back to global credentials or another account.
- Existing profiles without a connection reference retain the existing legacy OAuth
  configuration, import endpoints and fallback behavior. No existing tokens are copied.
- All deployment-defined names are available to tenant managers; each tenant connects its
  own independent accounts. Selecting a model profile does not expand tool permissions.

## API

Tenant owner/admin authorization (or platform admin) is required except on the callback:

- `GET /oauth/connections` — redacted local status.
- `POST /oauth/connections/{name}/login` — start connection-specific PKCE login.
- `POST /oauth/connections/complete` — authenticated `{ "state": "…", "code": "…" }` exchange.
- `POST /oauth/connections/{name}/import/pi` — extracted Pi OAuth object, bounded to 256 KiB.
- `DELETE /oauth/connections/{name}` — disconnect and invalidate pending logins.
- `GET /oauth/connections/callback` — public same-console redirect only; never exchanges tokens.

Standard bearer/session authentication applies. Cookie mutations retain same-origin/CSRF
checks, and coding instances retain their session proof and launch binding. No tenant or
user identity is accepted from completion/import bodies.
