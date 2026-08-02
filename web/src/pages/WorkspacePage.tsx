import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { RunEvent, ThreadListItem } from "../api/client";
import { useAuth } from "../auth/auth-context";

interface ActivityItem {
  id: number;
  label: string;
  error?: boolean;
}

export function WorkspacePage() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [streamedReply, setStreamedReply] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const activityId = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const threads = useQuery({
    queryKey: ["threads", authentication],
    queryFn: ({ signal }) => api.listThreads(50, signal),
    retry: false,
  });
  const messages = useQuery({
    queryKey: ["messages", selectedThreadId, authentication],
    queryFn: ({ signal }) => api.listMessages(selectedThreadId!, signal),
    enabled: selectedThreadId !== null,
    retry: false,
  });

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages.data, streamedReply, activity]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function addActivity(label: string, event?: RunEvent) {
    activityId.current += 1;
    setActivity((items) => [
      ...items,
      { id: activityId.current, label, error: event?.type === "run.error" },
    ]);
  }

  function handleRunEvent(event: RunEvent) {
    if (event.type === "assistant.message") {
      setStreamedReply(event.content ?? "");
      addActivity("Assistant response received", event);
    } else if (event.type === "run.error") {
      setError(event.detail ?? "The run failed");
      addActivity(event.detail ?? "Run failed", event);
    } else {
      addActivity(formatRunEvent(event), event);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || isRunning) return;

    setError(null);
    setDraft("");
    setStreamedReply(null);
    setActivity([]);
    setIsRunning(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let threadId = selectedThreadId;
    try {
      if (threadId === null) {
        const created = await api.createThread(controller.signal);
        threadId = created.thread_id;
        setSelectedThreadId(threadId);
      }
      await api.addMessage(threadId, content, controller.signal);
      await queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
      addActivity("Run started");
      await api.streamRun(threadId, handleRunEvent, controller.signal);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] }),
        queryClient.invalidateQueries({ queryKey: ["threads"] }),
      ]);
      setStreamedReply(null);
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        addActivity("Run stopped");
      } else {
        setError(caught instanceof Error ? caught.message : "Could not complete the run");
      }
    } finally {
      abortRef.current = null;
      setIsRunning(false);
    }
  }

  async function stopRun() {
    if (!selectedThreadId || !abortRef.current) return;
    try {
      await api.cancelRun(selectedThreadId);
    } finally {
      abortRef.current.abort();
    }
  }

  function newThread() {
    if (isRunning) return;
    setSelectedThreadId(null);
    setStreamedReply(null);
    setActivity([]);
    setError(null);
  }

  return (
    <section className="workspace-page">
      <aside className="thread-rail" aria-label="Conversations">
        <div className="thread-rail-heading">
          <div><p className="eyebrow">Workspace</p><h2>Conversations</h2></div>
          <button type="button" onClick={newThread} aria-label="New conversation">+</button>
        </div>
        {threads.isError && <p className="rail-error">Connect to load conversations.</p>}
        <div className="thread-list">
          {threads.data?.threads.map((thread) => (
            <ThreadButton
              key={thread.thread_id}
              thread={thread}
              active={thread.thread_id === selectedThreadId}
              onClick={() => {
                if (!isRunning) {
                  setSelectedThreadId(thread.thread_id);
                  setStreamedReply(null);
                  setActivity([]);
                  setError(null);
                }
              }}
            />
          ))}
          {!threads.isPending && !threads.data?.threads.length && (
            <p className="empty-threads">No conversations yet.<br />Start with a message.</p>
          )}
        </div>
      </aside>

      <div className="conversation">
        <header className="conversation-header">
          <div><span className={`run-dot ${isRunning ? "active" : ""}`} /><div><h1>{selectedTitle(threads.data?.threads, selectedThreadId)}</h1><small>{isRunning ? "Agent is working" : selectedThreadId ? "Ready" : "New conversation"}</small></div></div>
          {activity.length > 0 && <span className="activity-count">{activity.length} event{activity.length === 1 ? "" : "s"}</span>}
        </header>

        <div className="message-scroll" aria-live="polite">
          {!selectedThreadId && !isRunning && <Welcome />}
          {messages.isPending && selectedThreadId && <p className="loading-messages">Loading conversation…</p>}
          {messages.data?.filter((message) => message.role === "user" || message.role === "assistant").map((message) => (
            <article className={`chat-message ${message.role}`} key={message.id}>
              <span className="message-author">{message.role === "user" ? "You" : "Minigent"}</span>
              <p>{message.content}</p>
            </article>
          ))}
          {streamedReply !== null && <article className="chat-message assistant streaming"><span className="message-author">Minigent</span><p>{streamedReply}</p></article>}
          {isRunning && streamedReply === null && <div className="thinking-row"><i /><i /><i /><span>Working</span></div>}
          {error && <div className="conversation-error" role="alert">{error}</div>}
          <div ref={messagesEndRef} />
        </div>

        {activity.length > 0 && (
          <details className="activity-tray">
            <summary><span>Run activity</span><small>{activity.at(-1)?.label}</small></summary>
            <ol>{activity.map((item) => <li className={item.error ? "error" : ""} key={item.id}><span />{item.label}</li>)}</ol>
          </details>
        )}

        <form className="chat-composer" onSubmit={(event) => void sendMessage(event)}>
          <textarea
            aria-label="Message Minigent"
            placeholder="Ask Minigent anything…"
            value={draft}
            rows={1}
            disabled={isRunning}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          {isRunning ? (
            <button className="stop-run" type="button" onClick={() => void stopRun()}>Stop</button>
          ) : (
            <button className="send-message" type="submit" disabled={!draft.trim()} aria-label="Send message">↑</button>
          )}
          <small>Enter to send · Shift+Enter for a new line</small>
        </form>
      </div>
    </section>
  );
}

function ThreadButton({ thread, active, onClick }: { thread: ThreadListItem; active: boolean; onClick: () => void }) {
  return (
    <button className={`thread-button ${active ? "active" : ""}`} type="button" onClick={onClick}>
      <strong>{thread.title || "New conversation"}</strong>
      <span><small>{thread.message_count} message{thread.message_count === 1 ? "" : "s"}</small><time dateTime={thread.updated_at}>{relativeTime(thread.updated_at)}</time></span>
    </button>
  );
}

function Welcome() {
  return <div className="workspace-welcome"><span>M</span><p className="eyebrow">New conversation</p><h2>What are we working on?</h2><p>Start a focused agent run. Tool calls and runtime progress will appear as they happen.</p></div>;
}

function selectedTitle(threads: ThreadListItem[] | undefined, id: string | null) {
  if (id === null) return "Untitled conversation";
  return threads?.find((thread) => thread.thread_id === id)?.title ?? "Conversation";
}

function relativeTime(value: string) {
  const elapsed = Math.max(0, Date.now() - new Date(value).getTime());
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${String(minutes)}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${String(hours)}h`;
  return `${String(Math.floor(hours / 24))}d`;
}

function formatRunEvent(event: RunEvent) {
  const labels: Record<string, string> = {
    "run.started": "Run started",
    "llm.request": "Requesting model response",
    "llm.response": "Model response received",
    "tool.call": `Calling ${typeof event.tool_name === "string" ? event.tool_name : "tool"}`,
    "tool.result": `Completed ${typeof event.tool_name === "string" ? event.tool_name : "tool"}`,
    "run.completed": "Run completed",
  };
  return labels[event.type] ?? event.type.replaceAll(".", " ");
}
