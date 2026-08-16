import { lazy, Suspense, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  ImagePart,
  Message,
  PrivateValueConsentRequest,
  RunEvent,
  ThreadListItem,
} from "../api/client";
import { useAuth } from "../auth/auth-context";
import { visibleChatMessages } from "./workspaceMessages";
import { ContextDialog } from "../components/ContextDialog";
import { ConsentDialog } from "../components/ConsentDialog";

const AssistantMarkdown = lazy(async () => {
  const module = await import("../components/AssistantMarkdown");
  return { default: module.AssistantMarkdown };
});

interface PendingImage {
  file: File;
  previewUrl: string;
  detail: "auto" | "low" | "high";
}

interface ActivityItem {
  id: number;
  label: string;
  error?: boolean;
}

export function WorkspacePage() {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [selectedLlmProfile, setSelectedLlmProfile] = useState("");
  const [draft, setDraft] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [streamedReply, setStreamedReply] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [mobileThreadRailOpen, setMobileThreadRailOpen] = useState(false);
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [consentRequest, setConsentRequest] = useState<PrivateValueConsentRequest | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const activityId = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const pendingImagesRef = useRef<PendingImage[]>([]);
  const pendingConsentRef = useRef<PrivateValueConsentRequest | null>(null);

  const pendingConsents = useQuery({
    queryKey: ["pending-consents", selectedThreadId, authentication],
    queryFn: ({ signal }) => api.listPendingPrivateValueConsents(selectedThreadId!, signal),
    enabled: selectedThreadId !== null && consentRequest === null,
    retry: false,
    staleTime: 10_000,
  });

  useEffect(() => {
    pendingImagesRef.current = pendingImages;
  }, [pendingImages]);

  const config = useQuery({
    queryKey: ["public-config"],
    queryFn: ({ signal }) => api.getPublicConfig(signal),
    staleTime: 300_000,
  });
  const executionOptions = useQuery({
    queryKey: ["execution-options", authentication],
    queryFn: ({ signal }) => api.getExecutionOptions(signal),
    retry: false,
    staleTime: 60_000,
  });
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

  const effectiveAgent = selectedAgent || executionOptions.data?.agents.default || "";


  useEffect(
    () => () => {
      abortRef.current?.abort();
      for (const image of pendingImagesRef.current) URL.revokeObjectURL(image.previewUrl);
    },
    [],
  );

  function addActivity(label: string, event?: RunEvent) {
    activityId.current += 1;
    setActivity((items) => [
      ...items,
      { id: activityId.current, label, error: event?.type === "run.error" },
    ]);
  }

  function handleRunEvent(event: RunEvent) {
    if (event.type === "private_value.consent_required") {
      const request = privateValueConsentRequest(event.request);
      if (request) {
        pendingConsentRef.current = request;
        setConsentRequest(request);
        addActivity(`Approval required for ${request.tool_name}`);
      } else {
        setError("The private-value consent request was malformed.");
      }
      return;
    }
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
    const queuedImages = [...pendingImages];
    if ((!content && queuedImages.length === 0) || isRunning) return;

    setError(null);
    setDraft("");
    setStreamedReply(null);
    setActivity([]);
    pendingConsentRef.current = null;
    setConsentRequest(null);
    setIsRunning(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let threadId = selectedThreadId;
    const uploaded: Array<{ attachment_id: string }> = [];
    let messageStored = false;
    try {
      if (threadId === null) {
        const created = await api.createThread(
          {
            ...(effectiveAgent ? { agentName: effectiveAgent } : {}),
            ...(selectedLlmProfile ? { llmProfile: selectedLlmProfile } : {}),
          },
          controller.signal,
        );
        threadId = created.thread_id;
        setSelectedThreadId(threadId);
      }
      for (const image of queuedImages) {
        uploaded.push(await api.uploadAttachment(threadId, image.file, controller.signal));
      }
      const parts = uploaded.length
        ? [
            ...(content ? [{ type: "text" as const, text: content }] : []),
            ...uploaded.map((attachment, index) => ({
              type: "image" as const,
              mime_type: queuedImages[index].file.type,
              attachment_id: attachment.attachment_id,
              detail: queuedImages[index].detail,
            })),
          ]
        : undefined;
      await api.addMessage(threadId, content, parts, controller.signal);
      messageStored = true;
      for (const image of queuedImages) URL.revokeObjectURL(image.previewUrl);
      setPendingImages([]);
      await queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
      addActivity("Run started");
      await api.streamRun(threadId, handleRunEvent, controller.signal);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["messages", threadId] }),
        queryClient.invalidateQueries({ queryKey: ["threads"] }),
      ]);
      if (!pendingConsentRef.current) setStreamedReply(null);
    } catch (caught) {
      if (!messageStored && threadId !== null) {
        await Promise.allSettled(
          uploaded.map((attachment) => api.deleteAttachment(threadId!, attachment.attachment_id)),
        );
      }
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

  function addImageFiles(files: FileList | null) {
    if (!files?.length) return;
    const imageConfig = config.data?.image_input;
    if (!imageConfig?.enabled) {
      setError("Image input is disabled on this server.");
      return;
    }
    const candidates = Array.from(files);
    const totalCount = pendingImages.length + candidates.length;
    const totalBytes = [...pendingImages.map((image) => image.file.size), ...candidates.map((file) => file.size)]
      .reduce((sum, size) => sum + size, 0);
    if (totalCount > imageConfig.max_images) {
      setError(`A message can include at most ${String(imageConfig.max_images)} images.`);
      return;
    }
    if (totalBytes > imageConfig.max_total_bytes) {
      setError("Selected images exceed the total message size limit.");
      return;
    }
    for (const file of candidates) {
      if (!imageConfig.allowed_mime_types.includes(file.type.toLowerCase())) {
        setError(`Unsupported image type: ${file.type || file.name}`);
        return;
      }
      if (file.size > imageConfig.max_bytes) {
        setError(`${file.name} exceeds the per-image size limit.`);
        return;
      }
    }
    setError(null);
    setPendingImages((images) => [
      ...images,
      ...candidates.map((file) => ({ file, previewUrl: URL.createObjectURL(file), detail: "auto" as const })),
    ]);
  }

  function removeImage(index: number) {
    setPendingImages((images) => {
      const removed = images[index];
      if (removed) URL.revokeObjectURL(removed.previewUrl);
      return images.filter((_, imageIndex) => imageIndex !== index);
    });
  }

  function resolveConsent(result: { decision: "approved" | "denied" | "discarded"; reply?: string }) {
    if (selectedThreadId) {
      queryClient.setQueryData(
        ["pending-consents", selectedThreadId, authentication],
        [],
      );
    }
    setConsentRequest(null);
    pendingConsentRef.current = null;
    if (result.decision === "approved") {
      setStreamedReply(result.reply ?? null);
      addActivity("Private-value disclosure approved; action completed");
    } else if (result.decision === "denied") {
      addActivity("Private-value disclosure denied");
    } else {
      addActivity("Uncertain private action record discarded");
    }
    if (selectedThreadId) {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["messages", selectedThreadId] }),
        queryClient.invalidateQueries({ queryKey: ["threads"] }),
      ]).then(() => setStreamedReply(null));
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
    setMobileThreadRailOpen(false);
    setSelectedThreadId(null);
    setSelectedLlmProfile("");
    setStreamedReply(null);
    setActivity([]);
    setError(null);
    for (const image of pendingImages) URL.revokeObjectURL(image.previewUrl);
    setPendingImages([]);
  }

  async function renameSelectedThread() {
    if (!selectedThreadId || isRunning) return;
    const currentTitle = selectedTitle(threads.data?.threads, selectedThreadId);
    const requestedTitle = window.prompt("Rename conversation", currentTitle)?.trim();
    if (!requestedTitle || requestedTitle === currentTitle) return;
    try {
      await api.renameThread(selectedThreadId, requestedTitle);
      await queryClient.invalidateQueries({ queryKey: ["threads"] });
    } catch (renameError) {
      setError(renameError instanceof Error ? renameError.message : "Could not rename conversation");
    }
  }

  return (
    <section className="workspace-page">
      <aside className={`thread-rail ${mobileThreadRailOpen ? "is-open" : ""}`} aria-label="Conversations">
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
                  setMobileThreadRailOpen(false);
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
      {mobileThreadRailOpen && <button type="button" className="thread-rail-backdrop" aria-label="Close conversations" onClick={() => setMobileThreadRailOpen(false)} />}

      <div className="conversation">
        <header className="conversation-header">
          <button type="button" className="thread-rail-toggle" aria-label="Show conversations" onClick={() => setMobileThreadRailOpen(true)}>☰</button>
          <div><span className={`run-dot ${isRunning ? "active" : ""}`} /><div><h1>{selectedTitle(threads.data?.threads, selectedThreadId)}</h1><small>{isRunning ? "Agent is working" : selectedThreadId ? "Ready" : "New conversation"}</small></div></div>
          <div className="conversation-actions">
            {activity.length > 0 && <span className="activity-count">{activity.length} event{activity.length === 1 ? "" : "s"}</span>}
            <button type="button" disabled={!selectedThreadId || isRunning} onClick={() => void renameSelectedThread()}>Rename</button>
            <button type="button" disabled={!selectedThreadId || isRunning} onClick={() => setContextOpen(true)}>Context</button>
          </div>
        </header>

        <div className="message-scroll" aria-live="polite" tabIndex={0}>
          {!selectedThreadId && !isRunning && <Welcome />}
          {messages.isPending && selectedThreadId && <p className="loading-messages">Loading conversation…</p>}
          {visibleChatMessages(messages.data, streamedReply).map((message) => (
            <article className={`chat-message ${message.role}`} key={message.id}>
              <span className="message-author">{message.role === "user" ? "You" : "Minigent"}</span>
              {message.content && (message.role === "assistant" ? <RenderedAssistantMessage content={message.content} /> : <div className="message-content plain-message-content">{message.content}</div>)}
              <MessageImages message={message} />
            </article>
          ))}
          {streamedReply !== null && <article className="chat-message assistant streaming"><span className="message-author">Minigent</span><RenderedAssistantMessage content={streamedReply} /></article>}
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
          {pendingImages.length > 0 && (
            <div className="pending-images">
              {pendingImages.map((image, index) => (
                <div className="pending-image" key={image.previewUrl}>
                  <img src={image.previewUrl} alt={image.file.name} />
                  <select
                    aria-label={`Image detail for ${image.file.name}`}
                    value={image.detail}
                    onChange={(event) => setPendingImages((images) => images.map((item, imageIndex) => imageIndex === index ? { ...item, detail: event.target.value as PendingImage["detail"] } : item))}
                  >
                    <option value="auto">Auto detail</option><option value="low">Low detail</option><option value="high">High detail</option>
                  </select>
                  <button type="button" aria-label={`Remove ${image.file.name}`} onClick={() => removeImage(index)}>×</button>
                </div>
              ))}
            </div>
          )}
          <div className="composer-runtime-selectors">
            <label className="agent-selector">
              <span>Agent</span>
              <select
                aria-label="Agent"
                value={effectiveAgent}
                disabled={selectedThreadId !== null || isRunning || executionOptions.isPending}
                onChange={(event) => setSelectedAgent(event.target.value)}
              >
                {!executionOptions.data?.agents.items.length && <option value="">Default agent</option>}
                {executionOptions.data?.agents.items.map((agent) => {
                  const value = agent.id ?? agent.name;
                  const profile = agent.llm_profile ? ` · ${agent.llm_profile.replace(/^shared:/, "")}` : "";
                  return <option key={value} value={value}>{agent.display_name ?? agent.name}{profile}</option>;
                })}
              </select>
            </label>
            <label className="agent-selector">
              <span>Model profile</span>
              <select
                aria-label="Model profile"
                value={selectedLlmProfile}
                disabled={selectedThreadId !== null || isRunning || executionOptions.isPending}
                onChange={(event) => setSelectedLlmProfile(event.target.value)}
              >
                <option value="">Automatic</option>
                {executionOptions.data?.llm_profiles.items.map((profile) => {
                  const value = profile.name;
                  return <option key={value} value={value}>{profile.display_name ?? profile.name}</option>;
                })}
              </select>
            </label>
          </div>
          <label className={`attach-image ${config.data?.image_input.enabled ? "" : "disabled"}`} title={config.data?.image_input.enabled ? "Attach images" : "Image input is unavailable"}>
            <span aria-hidden="true">+</span><span className="sr-only">Attach images</span>
            <input
              type="file"
              accept={config.data?.image_input.allowed_mime_types.join(",") ?? "image/*"}
              multiple
              disabled={isRunning || !config.data?.image_input.enabled}
              onChange={(event) => { addImageFiles(event.target.files); event.target.value = ""; }}
            />
          </label>
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
            <button className="send-message" type="submit" disabled={!draft.trim() && pendingImages.length === 0} aria-label="Send message">↑</button>
          )}
          <small>Enter to send · Shift+Enter for a new line</small>
        </form>
      </div>
      <ContextDialog threadId={selectedThreadId} open={contextOpen} onClose={() => setContextOpen(false)} />
      <ConsentDialog
        threadId={selectedThreadId}
        request={consentRequest ?? pendingConsents.data?.[0] ?? null}
        onResolved={resolveConsent}
        onError={(message) => setError(message)}
      />
    </section>
  );
}

function ThreadButton({ thread, active, onClick }: { thread: ThreadListItem; active: boolean; onClick: () => void }) {
  const title = thread.title?.trim() || "New conversation";
  const context = thread.skill_name?.replace(/^(?:shared|user):/, "") || thread.capability_profile?.replace(/^(?:shared|user):/, "") || thread.llm_profile?.replace(/^shared:/, "") || "Default";
  const shortId = thread.thread_id.slice(0, 4);
  return (
    <button className={`thread-button ${active ? "active" : ""}`} type="button" onClick={onClick} title={`${title} · ${context} · ${thread.thread_id}`}>
      <span className="thread-title">{title}</span>
      <span className="thread-meta"><small>{context} · {thread.message_count} msg · {shortId}</small><time dateTime={thread.updated_at}>{relativeTime(thread.updated_at)}</time></span>
    </button>
  );
}

function RenderedAssistantMessage({ content }: { content: string }) {
  return <Suspense fallback={<div className="message-content plain-message-content">{content}</div>}><AssistantMarkdown>{content}</AssistantMarkdown></Suspense>;
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

function privateValueConsentRequest(value: unknown): PrivateValueConsentRequest | null {
  if (!value || typeof value !== "object") return null;
  const request = value as Partial<PrivateValueConsentRequest>;
  if (
    typeof request.consent_id !== "string" ||
    typeof request.thread_id !== "string" ||
    typeof request.tool_name !== "string" ||
    !Array.isArray(request.disclosures)
  ) return null;
  return {
    consent_id: request.consent_id,
    thread_id: request.thread_id,
    tool_name: request.tool_name,
    argument_fingerprint: typeof request.argument_fingerprint === "string" ? request.argument_fingerprint : "",
    status: typeof request.status === "string" ? request.status : "pending",
    one_shot: request.one_shot !== false,
    expires_at: typeof request.expires_at === "number" ? request.expires_at : 0,
    disclosures: request.disclosures.filter((item) =>
      Boolean(item) && typeof item.path === "string" && typeof item.kind === "string" && typeof item.count === "number"
    ),
  };
}

function MessageImages({ message }: { message: Message }) {
  const images = message.parts?.filter((part): part is ImagePart => part.type === "image") ?? [];
  if (images.length === 0) return null;
  return <div className="message-images">{images.map((image) => <AuthenticatedImage key={image.attachment_id} threadId={message.thread_id} image={image} />)}</div>;
}

function AuthenticatedImage({ threadId, image }: { threadId: string; image: ImagePart }) {
  const { api } = useAuth();
  const [source, setSource] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void api.getAttachmentBlob(threadId, image.attachment_id, controller.signal).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      setSource(objectUrl);
    }).catch((caught: unknown) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setSource("");
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, image.attachment_id, threadId]);
  if (source === null) return <div className="message-image-loading">Loading image…</div>;
  if (!source) return <div className="message-image-error">Image unavailable</div>;
  return <img src={source} alt="User attachment" />;
}
