# Minimal AI Agent Runtime — Design Document

## Overview

Minigent is a **minimal AI agent runtime API service** designed to support:

* Multiple independent conversation threads
* Simple agent execution loop with tool calling
* Stateless API + stateful thread store
* User-controlled session lifecycle (create/reset)

This is **not a framework**. It is a **runtime service** that users configure with their own
tools, models, and data.

## Goals

### Primary Goals

* Extremely simple architecture
* Multi-thread support
* Pluggable tools
* Replaceable LLM provider
* Minimal persistent state

### Non-Goals

* No vector DB — MiniRAG is opt-in, not built-in
* No workflow engine
* No background workers

## High-Level Architecture

```
Client
  ↓
FastAPI Service
  ├── Thread Store (in-memory or SQLite)
  ├── Agent Runtime (core loop)
  ├── Tool Registry (local + MCP)
  ├── LLM Adapter (mock, OpenAI, OpenRouter, Gemini, Generic OAuth)
  └── Agent Backends (native, peer-agent)
```

## Core Concepts

### Thread

A thread represents a single conversation session. It is identified by `thread_id`, contains
ordered messages, and is isolated from other threads.

### Message

Each message has: `id`, `thread_id`, `role` (system | user | assistant | tool), `content`,
optional `tool_name`, and `created_at`.

### Agent Runtime

The agent is a pure execution loop:

```
messages → LLM → (optional tool call) → LLM → response
```

### Tools

Registered functions, called by name, returning JSON-like results. Tools can be local
(built-in) or remote (via MCP servers).

### LLM Adapter

Abstract interface that handles model calls. Can be swapped between mock, OpenAI, OpenRouter,
Gemini, and Generic OAuth providers.

### Structured message content

`MessagePart` is the discriminated union of content modalities that Mindweft supports end to
end. `AttachmentPartBase` owns only shared attachment-source mechanics such as MIME type, inline
data, URL, and stored attachment ID. Attachment lifecycle operations—reference tracking, deletion,
forking, compaction, and runtime hydration—operate on that base class. Validation and LLM provider
serialization continue to operate on concrete modality classes such as `ImagePart`, `AudioPart`,
and `DocumentPart`. Validated uncompressed PCM WAV audio requires explicit profile capability and
is adapted by OpenAI/OpenRouter Chat Completions and Gemini; Responses and Anthropic reject it.
PDF and validated UTF-8 plain-text documents require explicit profile capability
metadata and provider adaptation; omitted metadata remains permissive only for legacy image
compatibility.

A new concrete part must not be added to the public `MessagePart` union until its validation,
provider adaptation, client behavior, and lifecycle tests are implemented together. Defining or
using the attachment base does not by itself advertise a new supported modality.

## Design Principles

### 1. Simplicity First

Avoid abstractions until necessary.

### 2. Agent as Pure Function

```
Agent = f(messages, tools) → new messages
```

### 3. Loose Coupling

* LLM is swappable
* Tools are pluggable
* Storage is replaceable

### 4. Stateless API

* API does not hold session
* State lives in thread store

## Summary

This system provides a minimal, extensible agent runtime with multi-thread conversation
handling, tool execution capability, and clean separation of concerns. It is intentionally
simple to validate core ideas before scaling complexity.

For detailed configuration, API reference, and setup guides, see the
[full reference](docs/reference.md) and [CLI reference](docs/cli.md). The proposed model for
principal-scoped personal agents, skills, capability profiles, and third-party MCP servers is
specified in [User execution extensibility](docs/user-execution-extensibility.md).
