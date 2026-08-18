# Context Offloading Proposal

## Status

Proposal / design note.

## Summary

Mindweft already has a basic form of context reduction through thread context compaction:
older messages can be folded into a thread summary while a recent message tail remains in
the active LLM prompt. This proposal extends that model into true context offloading by
preserving compacted raw context outside the active prompt and making it retrievable when
needed.

The goal is to keep prompts small for long-running threads without losing auditability,
debuggability, or task continuity.

## Current behavior

Today, Mindweft prompt construction is roughly:

1. runtime system prompt,
2. optional skill prompts,
3. optional `Thread summary:` system message,
4. unsummarized thread messages.

Thread context is represented by `ThreadContext`, which contains:

- `summary`, a text summary of compacted history;
- `summarized_message_count`, a boundary for history that has been summarized.

Compaction is available through:

- `POST /threads/{thread_id}/compact`;
- the interactive CLI `/compact` command;
- optional automatic compaction with `MINIGENT_CONTEXT_COMPACTION_ENABLED=true`.

Automatic compaction is disabled by default. This preserves stable prompt prefixes and can
improve provider-side prompt-cache reuse. Manual compaction remains available when a user
or operator wants smaller prompts.

The current compaction implementation is intentionally simple and deterministic. It
summarizes older messages into clipped lines and retains a recent message tail. It also
avoids splitting completed tool-call/tool-result pairs across a compaction boundary.

## Problem

The current model is prompt compaction, not full context offloading.

Once messages are compacted, raw historical details are removed from active message storage
in the current thread stores. The thread summary may preserve useful high-level context,
but summaries are lossy. This creates several issues for long-running or tool-heavy agent
sessions:

- detailed user requirements may be lost;
- large tool outputs may dominate active prompt context before compaction;
- debugging exact historical behavior is harder after compaction;
- the assistant has no first-class way to retrieve specific details from compacted spans;
- automatic compaction trades smaller prompts against prompt-cache stability.

## Goals

- Keep active LLM prompts small and relevant.
- Preserve raw compacted context outside the active prompt.
- Allow the assistant to retrieve offloaded details when needed.
- Keep manual compaction and prompt-cache-friendly defaults.
- Improve handling of large tool outputs and command/log/file results.
- Maintain tenant isolation and thread-level access controls.
- Preserve auditability and export/debug workflows.

## Non-goals

- Replacing the existing thread/message model.
- Requiring embeddings or a vector database for the first version.
- Enabling automatic compaction by default.
- Sending private offloaded context to remote services unless explicitly configured.

## Terminology

### Prompt compaction

Reducing the content sent to the next LLM call by summarizing older messages and retaining
a recent tail.

### Context offloading

Moving detailed context out of the active prompt into a tenant-scoped archive or artifact
store, while retaining a summary or reference in the active thread context.

### Retrieval

Searching or reading offloaded context and injecting selected details back into a future
assistant turn, usually through a tool call or controlled internal retrieval step.

## Proposed design

### 1. Preserve compacted spans in an offload archive

Before compacted messages are removed from active message storage, persist them as an
offloaded span.

Example archive record:

```json
{
  "archive_id": "ctx_...",
  "tenant_id": "tenant-a",
  "thread_id": "thread-a",
  "kind": "thread_compaction",
  "message_start": 0,
  "message_end": 24,
  "message_count": 24,
  "summary": "...",
  "raw_messages_json": "[...]",
  "token_estimate": 12345,
  "created_at": "..."
}
```

For SQLite, a minimal table could be:

```sql
CREATE TABLE thread_context_archives (
  archive_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  raw_messages_json TEXT NOT NULL,
  message_count INTEGER NOT NULL,
  token_estimate INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
```

The in-memory store should implement the same capability for test parity.

### 2. Keep the active prompt model simple

The active prompt should continue to be built from:

1. system and skill prompts;
2. thread summary;
3. recent raw messages.

The summary should include references to offloaded spans when useful, for example:

```text
Earlier context was offloaded:
- ctx_001: initial OpenRouter/static-auth setup discussion.
- ctx_002: MCP bridge debugging and command output.

Current objective: make coding-workspace runner easier to configure.
```

This keeps the model aware that additional context exists without stuffing all details into
every request.

### 3. Add offload inspection APIs

Add read APIs for debugging and clients:

```http
GET /threads/{thread_id}/context/offloads
GET /threads/{thread_id}/context/offloads/{archive_id}
```

These should be tenant-scoped and require the same thread access as messages/context.
They should support pagination or limits if archive count grows.

### 4. Add a thread-local retrieval tool

Add a local tool such as `retrieve_thread_context`.

Example input schema:

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string"},
    "top_k": {"type": "integer", "minimum": 1, "maximum": 20}
  },
  "required": ["query"]
}
```

For a first version, retrieval can use simple keyword scoring or SQLite FTS over archive
summaries and raw messages. Embeddings can be added later.

Returned results should include:

- archive ID;
- summary snippet;
- matching message snippets;
- role/tool metadata;
- enough provenance to cite the retrieved context accurately.

### 5. Offload large tool results as artifacts

Tool outputs are often the largest source of prompt bloat. Mindweft should support storing
large tool outputs separately while keeping a compact digest in the active message.

Active message example:

```text
Tool shell-workspace_run_command returned exit=1.
Summary: pytest failed in tests/test_runtime.py::test_context_compaction_boundary.
Full output offloaded as artifact tool_abc123.
```

A companion retrieval/read tool can expose exact ranges:

```text
read_tool_artifact(artifact_id, start_line?, end_line?)
```

This is especially useful for coding workspace sessions, where command output, file reads,
search results, and logs can be large.

### 6. Improve summary shape

The current line-based summary is predictable but weak as long-term memory. A better
summary should use a structured shape:

```markdown
## User goals
- ...

## Decisions made
- ...

## Current state
- ...

## Tool evidence
- ...

## Open questions / TODOs
- ...

## Offloaded spans
- ctx_001: ...
```

This can start as deterministic formatting and later support optional LLM-generated
summaries with a deterministic fallback.

## Configuration

Suggested future settings:

```dotenv
MINIGENT_CONTEXT_OFFLOAD_ENABLED=true
MINIGENT_CONTEXT_AUTO_OFFLOAD_POLICY=manual
MINIGENT_CONTEXT_TARGET_PROMPT_TOKENS=3000
MINIGENT_CONTEXT_RETAIN_RECENT_MESSAGES=8
MINIGENT_CONTEXT_ARCHIVE_RAW=true
MINIGENT_TOOL_OUTPUT_OFFLOAD_ENABLED=true
MINIGENT_TOOL_OUTPUT_OFFLOAD_THRESHOLD_BYTES=20000
```

Suggested policies:

- `manual`: preserve current prompt-cache-friendly default behavior.
- `token-threshold`: offload when estimated active prompt tokens exceed a threshold.
- `before-limit`: offload only when approaching the selected model's context limit.
- `tool-output-only`: offload large tool outputs while keeping normal conversation history
  append-only.

The safest default remains manual/off unless a deployment explicitly opts in.

## Privacy, tenancy, and retention

Offloaded context may contain private prompts, source code snippets, command output,
internal URLs, or accidental secrets. The design should enforce:

- tenant-scoped archive records;
- thread-scoped access checks;
- deletion of offloads when a thread is deleted or pruned;
- export options that either include or exclude offloaded context explicitly;
- redaction/truncation in logs and streamed events;
- no cross-tenant retrieval;
- no remote summarization or remote retrieval unless explicitly configured.

## Implementation plan

### Phase 1: Archive compacted spans

- Extend the `ThreadStore` protocol with archive methods.
- Add in-memory archive storage.
- Add SQLite table and persistence methods.
- During compaction, save the compacted span before deleting it from active messages.
- Preserve current active prompt behavior.

### Phase 2: Inspection APIs and CLI visibility

- Add `GET /threads/{thread_id}/context/offloads`.
- Add `GET /threads/{thread_id}/context/offloads/{archive_id}`.
- Optionally add CLI commands such as `/offloads` and `/offload <id>`.
- Ensure exports can include offloaded spans when requested.

### Phase 3: Retrieval tool

- Add `retrieve_thread_context` as a local tool when context offloading is enabled.
- Implement simple keyword or SQLite FTS retrieval.
- Return snippets with clear provenance.

### Phase 4: Tool artifact offloading

- Add artifact storage for large tool outputs.
- Replace oversized tool message content with a digest and artifact reference.
- Add a read/range API or tool for artifact inspection.

### Phase 5: Optional smarter summaries

- Introduce structured deterministic summaries.
- Optionally add LLM-generated compaction summaries behind configuration.
- Keep deterministic fallback behavior for reliability.

## Open questions

- Should raw archive preservation be mandatory when compaction is enabled, or controlled by
  `MINIGENT_CONTEXT_ARCHIVE_RAW`?
- Should offload retrieval be a normal local tool, an internal runtime step, or both?
- How should archives participate in thread export and admin pruning?
- What is the best default threshold for large tool-output artifact offloading?
- Should offloaded raw messages preserve exact original message IDs and positions?
- Should summaries be regenerated when retrieval finds missing or stale details?

## Recommendation

Keep Mindweft's current prompt-cache-friendly default: no automatic compaction unless the
operator opts in. Add true context offloading incrementally by first archiving compacted raw
spans, then adding retrieval and large tool-output artifacts.

This preserves the simplicity of the existing runtime while making long-running, tool-heavy
threads more durable and scalable.
