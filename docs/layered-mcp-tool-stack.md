# Layered MCP tool stack for coding agents

Status: Implemented (see [Coding workspace setup](coding-workspace.md) for the current setup guide)

## Context

Mindweft coding workflows can use MCP servers for workspace access, code navigation, and
other local capabilities. The current filesystem MCP used by the coding workspace is a
third-party Node.js package. It provides basic file operations, but it does not expose
arbitrary line-range reads, which can make agents inefficient when they need only a small
section of a large file.

We are also evaluating structural code-navigation tools such as
[`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp). Those tools can
index a repository and answer graph-oriented questions about symbols, calls, routes,
architecture, and impact. They should reduce broad exploratory grep/read loops, but they do
not replace authoritative filesystem reads or file editing.

## Decision

Use a layered MCP tool stack rather than replacing the filesystem MCP.

### Layer 1: Authoritative filesystem tools

The filesystem MCP remains required. It is the source-of-truth layer for exact workspace
state and file mutation.

Responsibilities:

- list files and directories;
- read exact current file contents;
- create, move, write, and edit files;
- inspect file metadata;
- access files that are ignored, unindexed, newly created, or outside a code graph.

Before editing a file, the agent should verify the exact current contents from the
filesystem layer. Graph or memory tools can guide where to look, but they are not the
source of truth for safe edits.

### Layer 2: Targeted text reading

Add a small targeted-read companion MCP, or upstream equivalent tools to the third-party
filesystem MCP, when practical.

Candidate tools:

- `read_text_file_lines(path, start_line, end_line)`;
- `read_text_file_around(path, line, before, after)`;
- `search_text_file(path, pattern, before, after, max_matches)`.

This layer is intentionally narrow. It should provide efficient exact inspection after the
agent has identified a relevant file or region. It should not become a full code-intelligence
system.

### Layer 3: Codebase memory and navigation

Codebase-memory/navigation MCPs are optional but high-value discovery tools.

Responsibilities:

- index repository structure;
- search symbols, functions, classes, routes, and files;
- trace call paths and dependency relationships;
- summarize architecture and boundaries;
- map git diffs to affected symbols and risk;
- return snippets by qualified symbol when available.

This layer helps answer where to inspect and what may be affected. It should reduce broad
filesystem exploration, but its index may be stale, incomplete, or intentionally scoped by
ignore rules.

## Tool selection policy

Preferred workflow for coding tasks:

1. Use codebase-memory/navigation tools for discovery when an index is available.
2. Use targeted text reads to inspect exact current code around relevant regions.
3. Use authoritative filesystem reads when broader file context is needed or targeted reads
   are unavailable.
4. Use filesystem edit/write tools for modifications.
5. Run tests, linters, type checks, or other validation commands appropriate to the task.
6. Re-query impact or refresh indexes when changes may affect graph-backed answers.

The read-before-edit invariant remains mandatory: before changing an existing file, verify
the exact current file contents from the filesystem layer.

## Non-goals

- Do not replace filesystem tools with codebase-memory/navigation tools.
- Do not require codebase-memory MCPs for basic coding functionality.
- Do not fork the third-party filesystem MCP unless extension, upstream contribution, or a
  companion MCP is insufficient.
- Do not rely on indexed snippets as the only context for edits.

## Risks and mitigations

- **Stale graph data:** verify exact file contents before edits and refresh indexes after
  meaningful changes.
- **Ignored or unindexed files:** fall back to filesystem tools.
- **Snippet-only context:** use targeted or broader filesystem reads when neighboring code,
  imports, formatting, or tests matter.
- **Multiple MCP servers:** keep tool names and descriptions clear, and document which layer
  is authoritative.
- **Permission differences:** apply least privilege per MCP server and avoid granting graph
  tools write access unless explicitly needed.

## Future work

- Prototype a minimal targeted-read companion MCP for line-range and around-line reads.
- Evaluate `codebase-memory-mcp` on real Mindweft coding tasks.
- Consider upstreaming targeted-read tools to the third-party filesystem MCP.
- Add coding-agent instructions that prefer graph discovery, then targeted exact reads, then
  filesystem edits.
- Measure whether the layered workflow reduces token usage and tool calls compared with
  file-by-file exploration.
