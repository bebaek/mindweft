## Status

Completed. The one-shot and interactive/client code now share the `minigent_client` API client, config, state, and output rendering helpers. `app/cli.py` is a compatibility wrapper around `minigent_client.one_shot_cli`.

## Goal

Make `minigent_client` the shared client implementation, and make both CLI entrypoints use the same API/session/thread code.

Current split:

- `app/cli.py`
  - Good one-shot command UX
  - Has `chat`, `threads`, `health`, `config`
  - Has local last-thread state
  - Has `--json`, `--trace`, `--stream`

- `minigent_client/cli.py`
  - Good interactive/voice UX
  - Has terminal chat loop and audio backends
  - Uses `MinigentAPIClient`
  - Has `--backend chat|stdin|manual-audio|passive-audio`

## Proposed shape

### 1. Promote shared API methods into `minigent_client/api_client.py`

Expand `MinigentAPIClient` so `app/cli.py` no longer needs its own raw urllib helpers.

Add methods like:

```python
class MinigentAPIClient:
    def health(self) -> dict: ...
    def config(self) -> dict: ...

    def create_thread(
        self,
        *,
        skill_name: str | None = None,
        skills: list[str] | None = None,
        capability_profile: str | None = None,
    ) -> dict: ...

    def get_thread(self, thread_id: str) -> dict: ...
    def delete_thread(self, thread_id: str) -> None: ...

    def add_message(self, thread_id: str, content: str) -> dict: ...
    def run_thread(self, thread_id: str, *, stream: bool = False) -> str: ...
```

Keep the existing runtime-facing methods as wrappers:

```python
def ensure_thread(self) -> str: ...
def send_user_message(self, content: str) -> dict: ...
```

That preserves voice/runtime behavior.

### 2. Move local CLI state into a shared module

Create something like:

```text
minigent_client/state.py
```

Move from `app/cli.py`:

- `STATE_DIR_NAME`
- `STATE_FILE_NAME`
- last-thread loading/saving
- principal/server key hashing

Expose:

```python
class ClientState:
    @classmethod
    def load(cls) -> ClientState: ...
    def save(self) -> None: ...

    def get_last_thread(self, key: str) -> str | None: ...
    def set_last_thread(self, key: str, thread_id: str) -> None: ...
```

Then both one-shot and interactive chat can resume the same remembered thread.

### 3. Move auth/config construction into one place

Right now both CLIs parse similar values:

- `--base-url`
- `--api-token`
- `--user-id`
- `--tenant-id`
- `--admin`
- skill/thread options

Unify this into a helper, probably in:

```text
minigent_client/config.py
```

Add a constructor like:

```python
def config_from_args(args: argparse.Namespace) -> ClientConfig:
    ...
```

Or lower-level:

```python
def build_client_config(
    *,
    base_url: str | None,
    api_token: str | None,
    user_id: str | None,
    tenant_id: str | None,
    admin: bool,
    thread_id: str | None = None,
    skill_name: str | None = None,
    stream_runs: bool = False,
) -> ClientConfig:
    ...
```

### 4. Keep `app/cli.py`, but make it thin

Do not delete it immediately. Convert it into a compatibility wrapper that imports command handlers from `minigent_client`.

For example:

```python
from minigent_client.commands import main

if __name__ == "__main__":
    raise SystemExit(main())
```

Or if preserving exact command behavior matters:

```python
from minigent_client.one_shot_cli import main
```

This avoids breaking existing docs/scripts using:

```bash
python -m app.cli ...
```

### 5. Add a real subcommand CLI to `minigent_client/cli.py`

Instead of only:

```bash
minigent-client --backend chat
```

Support:

```bash
minigent-client chat
minigent-client threads create
minigent-client threads show THREAD_ID
minigent-client threads delete THREAD_ID
minigent-client health
minigent-client config
minigent-client voice
minigent-client stdin
```

Keep old `--backend` behavior as compatibility:

```bash
minigent-client --backend chat
```

But internally route it to the new `chat` command.

### 6. Share rendering/output logic

Done: shared formatting and stream progress rendering live in `minigent_client/output.py`.

### 7. Suggested implementation order

Low-risk sequence:

1. Add missing methods to `minigent_client/api_client.py`.
2. Move last-thread state from `app/cli.py` into `minigent_client/state.py`.
3. Refactor `app/cli.py` to use `MinigentAPIClient`, but keep behavior identical.
4. Add subcommands to `minigent_client/cli.py`.
5. Make `app/cli.py` a thin compatibility wrapper.
6. Update README command examples.

## Best first PR

I’d start with only this:

- Extend `minigent_client/api_client.py`
- Add `minigent_client/state.py`
- Refactor `app/cli.py` to use those shared modules
- No UX changes yet

That gives immediate de-duplication while keeping the public CLI stable. Then the interactive CLI can gain thread commands on top of the same shared foundation.
