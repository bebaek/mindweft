# Minimal AI Agent Runtime — Design Document (POC)

## 1. Overview

This project is a **minimal AI agent runtime API service** designed to support:

* Multiple independent conversation threads
* Simple agent execution loop
* Tool calling (pluggable)
* Stateless API + stateful thread store
* User-controlled session lifecycle (create/reset)

This is **NOT a framework**.
It is a **runtime service** that users can configure with their own tools, models, and data.

---

## 2. Goals

### Primary Goals

* Extremely simple architecture
* Multi-thread support
* Pluggable tools
* Replaceable LLM provider
* Minimal persistent state

### Non-Goals (for POC)

* No vector DB
* No long-term memory
* No workflow engine
* No multi-agent orchestration
* No background workers
* No streaming (initially)

---

## 3. High-Level Architecture

```
Client
  ↓
FastAPI Service
  ├── Thread Store (in-memory → SQLite later)
  ├── Agent Runtime (core loop)
  ├── Tool Registry
  └── LLM Adapter
```

---

## 4. Core Concepts

### Thread

A thread represents a single conversation session.

* Identified by `thread_id`
* Contains ordered messages
* Isolated from other threads

---

### Message

Each message has:

* `id`
* `thread_id`
* `role` (system | user | assistant | tool)
* `content`
* `tool_name` (optional)
* `created_at`

---

### Agent Runtime

The agent is a **pure execution loop**:

```
messages → LLM → (optional tool call) → LLM → response
```

---

### Tools

* Registered functions
* Called by name
* Return JSON-like results

---

### LLM Adapter

* Abstract interface
* Handles model calls
* Can be swapped (OpenAI, local, etc.)

---

## 5. API Specification

### Create Thread

```
POST /threads
```

Response:

```
{
  "thread_id": string
}
```

---

### Add Message

```
POST /threads/{thread_id}/messages
```

Body:

```
{
  "content": string
}
```

---

### Run Agent

```
POST /threads/{thread_id}/run
```

Response:

```
{
  "reply": string
}
```

---

### Get Messages

```
GET /threads/{thread_id}/messages
```

---

### (Optional) Reset Thread

```
DELETE /threads/{thread_id}
```

---

## 6. Data Model

### Thread (optional for now)

```
thread_id: string
status: "idle" | "running" | "error"
created_at: datetime
updated_at: datetime
```

---

### Message

```
id: string
thread_id: string
role: string
content: string
tool_name: optional string
created_at: datetime
```

---

## 7. Agent Execution Flow

### Step-by-step

1. User sends message
2. Store message in thread
3. Load full thread history
4. Call LLM with messages + tools

---

### If LLM returns tool call:

5. Execute tool
6. Store tool result as message
7. Re-run agent loop

---

### If LLM returns final response:

8. Store assistant message
9. Return response to client

---

### Pseudocode

```
function run_thread(thread_id):
    messages = get_messages(thread_id)

    response = llm.generate(messages, tools)

    if response contains tool_call:
        result = execute_tool(response.tool_call)
        store(tool_message)
        return run_thread(thread_id)

    store(assistant_message)
    return response
```

---

## 8. Tool System

### Interface

```
tool = {
  name: string
  description: string
  execute(input) → output
}
```

### Registry

* Tools stored in a dictionary/map
* Looked up by name
* Easily extendable

---

## 9. LLM Adapter Interface

```
generate(messages, tools) → {
  text?: string
  tool_call?: {
    name: string
    input: any
  }
}
```

---

## 10. Storage Strategy

### Phase 1 (POC)

* In-memory store (dict/list)

### Phase 2

* SQLite

### Future

* External DB (Postgres, etc.)

---

## 11. Concurrency Model

For POC:

* Requests handled independently
* No locking required initially

Future improvements:

* Thread-level locking
* Status tracking (`running`, `idle`)
* Async execution queue

---

## 12. Extensibility Plan

### Short-term

* Replace LLM stub with real provider
* Add structured tool calling
* Add thread reset endpoint

### Mid-term

* Load tools dynamically (config or MCP)
* Add per-thread configuration
* Add streaming responses

### Long-term

* External tool providers (MCP servers)
* Memory summarization
* Multi-agent orchestration
* Workflow engine (optional)

---

## 13. Design Principles

### 1. Simplicity First

Avoid abstractions until necessary.

---

### 2. Agent as Pure Function

```
Agent = f(messages, tools) → new messages
```

---

### 3. Loose Coupling

* LLM is swappable
* Tools are pluggable
* Storage is replaceable

---

### 4. Stateless API

* API does not hold session
* State lives in thread store

---

## 14. Future Considerations

* Authentication / multi-user support
* Rate limiting
* Observability (logs, tracing)
* Error handling + retries
* Tool sandboxing/security

---

## 15. Summary

This system provides:

* A minimal, extensible agent runtime
* Multi-thread conversation handling
* Tool execution capability
* Clean separation of concerns

It is intentionally simple to validate core ideas before scaling complexity.

---

## 16. Implementation Checklist

* [ ] FastAPI app setup
* [ ] Thread store (in-memory)
* [ ] Message model
* [ ] Tool registry
* [ ] LLM adapter (stub)
* [ ] Agent loop
* [ ] API endpoints
* [ ] Basic testing via `/docs`
