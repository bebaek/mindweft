import { lazy, Suspense, useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  type AudioPart,
  type DocumentPart,
  type ExecutionLlmOptionItem,
  type ExecutionOptionsResponse,
  type ImagePart,
  type Message,
  type PrivateValueConsentRequest,
  type RunEvent,
  type ThreadListItem,
  type ThreadSearchResult,
} from "../api/client";
import { useAuth } from "../auth/auth-context";
import { reasoningSummary, persistedToolSteps, visibleChatMessages, withDefaultAgent, type PersistedToolStep } from "./workspaceMessages";
import {
  classifyAttachmentFiles,
  unsupportedAttachmentMessage,
  validateQueueAddition,
} from "./workspaceAttachments";

import { runErrorMessage } from "./runEvents";
import { AudioAttachment } from "../components/AudioAttachment";
import { AudioRecorder } from "../components/AudioRecorder";
import { ContextDialog } from "../components/ContextDialog";
import { ConsentDialog } from "../components/ConsentDialog";
import { DocumentAttachment } from "../components/DocumentAttachment";

const AssistantMarkdown = lazy(async () => {
  const module = await import("../components/AssistantMarkdown");
  return { default: module.AssistantMarkdown };
});

interface PendingAudio {
  file: File;
  previewUrl: string;
}

interface PendingDocument {
  file: File;
}

interface PendingImage {
  file: File;
  previewUrl: string;
  detail: "auto" | "low" | "high";
}

function normalizedProfileName(name: string | null | undefined): string | null {
  return name?.replace(/^shared:/, "") || null;
}

function effectiveLlmOption(
  options: ExecutionOptionsResponse | undefined,
  thread: ThreadListItem | undefined,
  selectedProfile: string,
  effectiveAgent: string,
): ExecutionLlmOptionItem | undefined {
  if (!options) return undefined;
  const findProfile = (name: string | null | undefined) => {
    const normalized = normalizedProfileName(name);
    return normalized
      ? options.llm_profiles.items.find((profile) => profile.name === normalized)
      : undefined;
  };
  if (thread) {
    return findProfile(thread.llm_profile) ?? options.llm_profiles.effective_default;
  }
  const explicit = findProfile(selectedProfile);
  if (explicit) return explicit;
  const agent = options.agents.items.find(
    (item) => (item.id ?? item.name) === effectiveAgent,
  );
  return (
    findProfile(agent?.llm_profile) ??
    findProfile(options.llm_profiles.default) ??
    options.llm_profiles.effective_default
  );
}

function audioInputUnavailableMessage(profile: ExecutionLlmOptionItem | undefined): string | null {
  if (profile?.audio_input_allowed !== false) return null;
  if (profile.audio_input_reason === "backend_unsupported") {
    return "The selected agent backend does not support audio input.";
  }
  if (profile.audio_input_reason === "profile_unsupported") {
    return "The selected model profile does not accept audio.";
  }
  return "Audio input is disabled on this server.";
}

function documentInputUnavailableMessage(profile: ExecutionLlmOptionItem | undefined): string | null {
  if (profile?.document_input_allowed !== false) return null;
  if (profile.document_input_reason === "backend_unsupported") {
    return "The selected agent backend does not support document input.";
  }
  if (profile.document_input_reason === "profile_unsupported") {
    return "The selected model profile does not accept documents.";
  }
  return "Document input is disabled on this server.";
}

function imageInputUnavailableMessage(profile: ExecutionLlmOptionItem | undefined): string | null {
  if (profile?.image_input_allowed !== false) return null;
  if (profile.image_input_reason === "backend_unsupported") {
    return "The selected agent backend does not support image input.";
  }
  if (profile.image_input_reason === "profile_unsupported") {
    return "The selected model profile only accepts text.";
  }
  return "Image input is disabled on this server.";
}

type ActivityStatus = "pending" | "success" | "error" | "info";

interface ActivityItem {
  id: number;
  type: string;
  label: string;
  status: ActivityStatus;
  toolName?: string;
  toolCallId?: string;
  arguments?: unknown;
  result?: unknown;
  durationMs?: number;
  startedAt?: number;
  error?: boolean;
}

interface WorkspacePageProps {
  sidebarHeader?: ReactNode;
  sidebarFooter?: ReactNode;
}

export function WorkspacePage({ sidebarHeader, sidebarFooter }: WorkspacePageProps) {
  const { api, authentication } = useAuth();
  const queryClient = useQueryClient();
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [defaultAgentFeedback, setDefaultAgentFeedback] = useState<{ message: string; error: boolean } | null>(null);
  const [selectedLlmProfile, setSelectedLlmProfile] = useState("");
  const [draft, setDraft] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [showThinking, setShowThinking] = useState(false);
  const [liveThinking, setLiveThinking] = useState<string | null>(null);
  const [streamedReply, setStreamedReply] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [branchNotice, setBranchNotice] = useState<string | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [mobileThreadRailOpen, setMobileThreadRailOpen] = useState(false);
  const [threadSearch, setThreadSearch] = useState("");
  const [threadSearchScope, setThreadSearchScope] = useState<"title" | "all">("title");
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  const [showArchivedThreads, setShowArchivedThreads] = useState(false);
  const [pendingAudio, setPendingAudio] = useState<PendingAudio[]>([]);
  const [audioRecordingActive, setAudioRecordingActive] = useState(false);
  const [pendingDocuments, setPendingDocuments] = useState<PendingDocument[]>([]);
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);
  const [attachmentDragActive, setAttachmentDragActive] = useState(false);
  const [consentRequest, setConsentRequest] = useState<PrivateValueConsentRequest | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const activityId = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messageInputRef = useRef<HTMLTextAreaElement>(null);
  const pendingAudioRef = useRef<PendingAudio[]>([]);
  const pendingImagesRef = useRef<PendingImage[]>([]);
  const attachmentDragDepth = useRef(0);
  const pendingConsentRef = useRef<PrivateValueConsentRequest | null>(null);

  const pendingConsents = useQuery({
    queryKey: ["pending-consents", selectedThreadId, authentication],
    queryFn: ({ signal }) => api.listPendingPrivateValueConsents(selectedThreadId!, signal),
    enabled: selectedThreadId !== null && consentRequest === null,
    retry: false,
    staleTime: 10_000,
  });

  useEffect(() => {
    pendingAudioRef.current = pendingAudio;
  }, [pendingAudio]);

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
  const saveDefaultAgent = useMutation({
    mutationFn: async (agentRef: string) => {
      let currentConfig;
      try {
        currentConfig = await api.getUserExecutionConfig();
      } catch (caught) {
        if (!(caught instanceof ApiError && caught.status === 404)) throw caught;
      }
      return api.updateUserExecutionConfig(
        withDefaultAgent(currentConfig?.config, agentRef),
        currentConfig?.version,
      );
    },
    onSuccess: (saved, agentRef) => {
      queryClient.setQueryData(["user-execution-config", authentication], saved);
      queryClient.setQueryData<ExecutionOptionsResponse>(
        ["execution-options", authentication],
        (current) => current ? { ...current, agents: { ...current.agents, default: agentRef } } : current,
      );
      setSelectedAgent("");
      setDefaultAgentFeedback({ message: "Default saved for future conversations.", error: false });
      void queryClient.invalidateQueries({ queryKey: ["execution-options"] });
    },
    onError: (caught) => {
      setDefaultAgentFeedback({
        message: caught instanceof Error ? caught.message : "Could not save the default agent.",
        error: true,
      });
    },
  });
  const threads = useQuery({
    queryKey: ["threads", authentication, threadSearch, threadSearchScope, showArchivedThreads],
    queryFn: async ({ signal }) => {
      const query = threadSearch.trim();
      if (query && threadSearchScope === "all") {
        const search = await api.searchThreads(query, signal, {
          scope: "all",
          archived: showArchivedThreads,
          limit: 50,
        });
        return {
          threads: search.results.map((result) => result.thread),
          total: search.total,
          limit: search.limit,
          offset: search.offset,
          searchResults: search.results,
        };
      }
      const listed = await api.listThreads(50, signal, {
        ...(query ? { q: query } : {}),
        archived: showArchivedThreads,
      });
      return { ...listed, searchResults: [] as ThreadSearchResult[] };
    },
    retry: false,
    refetchInterval: (query) => {
      const selected = query.state.data?.threads.find((thread) => thread.thread_id === selectedThreadId);
      if (selected?.title_source !== "generated" || !selected.title_updated_at) return false;
      return Date.now() - Date.parse(selected.title_updated_at) < 30_000 ? 5_000 : false;
    },
  });
  const messages = useQuery({
    queryKey: ["messages", selectedThreadId, authentication],
    queryFn: ({ signal }) => api.listMessages(selectedThreadId!, signal),
    enabled: selectedThreadId !== null,
    retry: false,
  });
  const lineage = useQuery({
    queryKey: ["thread-lineage", selectedThreadId, authentication],
    queryFn: ({ signal }) => api.getThreadLineage(selectedThreadId!, signal),
    enabled: selectedThreadId !== null,
    retry: false,
  });
  const forkThread = useMutation({
    mutationFn: ({ threadId, messageId }: { threadId: string; messageId: string }) =>
      api.forkThread(threadId, messageId),
    onSuccess: (result) => {
      setError(null);
      const sourceTitle = selectedTitle(threads.data?.threads, result.parent_thread_id);
      setBranchNotice(`Branched from “${sourceTitle}”. The source thread was preserved.`);
      setSelectedThreadId(result.thread_id);
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["thread-lineage"] });
    },
    onError: (caught) => {
      setBranchNotice(null);
      setError(caught instanceof Error ? caught.message : "Could not branch from this message.");
    },
  });
  const organizeThread = useMutation({
    mutationFn: ({ threadId, organization }: {
      threadId: string;
      organization: { pinned?: boolean; archived?: boolean };
    }) => api.updateThreadOrganization(threadId, organization),
    onSuccess: (updated, variables) => {
      setError(null);
      if (variables.organization.archived === true && updated.thread_id === selectedThreadId) {
        newThread();
      }
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["thread-lineage"] });
    },
    onError: (caught) => {
      setError(caught instanceof Error ? caught.message : "Could not organize conversation.");
    },
  });

  useEffect(() => {
    if (!isRunning) messageInputRef.current?.focus({ preventScroll: true });
  }, [isRunning, selectedThreadId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView?.({ block: "end", behavior: "smooth" });
  }, [messages.data, streamedReply, activity]);

  useEffect(() => {
    if (!highlightedMessageId || !messages.data) return;
    const element = document.getElementById(`message-${highlightedMessageId}`);
    element?.scrollIntoView({ block: "center", behavior: "smooth" });
    const timeout = window.setTimeout(() => setHighlightedMessageId(null), 2400);
    return () => window.clearTimeout(timeout);
  }, [highlightedMessageId, messages.data]);

  const effectiveAgent = selectedAgent || executionOptions.data?.agents.default || "";
  const composerThread = threads.data?.threads.find(
    (thread) => thread.thread_id === selectedThreadId,
  ) ?? lineage.data?.thread;
  const composerLlmOption = effectiveLlmOption(
    executionOptions.data,
    composerThread,
    selectedLlmProfile,
    effectiveAgent,
  );
  const profileAudioUnavailable = audioInputUnavailableMessage(composerLlmOption);
  const audioInputAvailable = Boolean(config.data?.audio_input?.enabled) && !profileAudioUnavailable;
  const audioInputMessage = !config.data?.audio_input?.enabled
    ? "Audio input is disabled on this server."
    : profileAudioUnavailable;
  const queuedAudioBlocked = pendingAudio.length > 0 && !audioInputAvailable;
  const profileDocumentUnavailable = documentInputUnavailableMessage(composerLlmOption);
  const documentInputAvailable = Boolean(config.data?.document_input.enabled) && !profileDocumentUnavailable;
  const documentInputMessage = !config.data?.document_input.enabled
    ? "Document input is disabled on this server."
    : profileDocumentUnavailable;
  const queuedDocumentsBlocked = pendingDocuments.length > 0 && !documentInputAvailable;
  const profileImageUnavailable = imageInputUnavailableMessage(composerLlmOption);
  const imageInputAvailable = Boolean(config.data?.image_input.enabled) && !profileImageUnavailable;
  const imageInputMessage = !config.data?.image_input.enabled
    ? "Image input is disabled on this server."
    : profileImageUnavailable;
  const queuedImagesBlocked = pendingImages.length > 0 && !imageInputAvailable;
  const documentAccept = config.data?.document_input.allowed_mime_types.includes("text/plain")
    ? [...config.data.document_input.allowed_mime_types, ".txt", ".md", ".csv", ".log"].join(",")
    : (config.data?.document_input.allowed_mime_types.join(",") ?? "application/pdf");
  const attachmentKinds = [imageInputAvailable && "images", documentInputAvailable && "documents", audioInputAvailable && "audio"].filter((value): value is string => Boolean(value));
  const attachmentDropLabel = attachmentKinds.length === 0
    ? null
    : `Drop ${attachmentKinds.length === 1 ? attachmentKinds[0] : `${attachmentKinds.slice(0, -1).join(", ")}${attachmentKinds.length > 2 ? "," : ""} or ${attachmentKinds.at(-1)}`} to attach them`;
  const canSaveDefaultAgent = Boolean(
    selectedAgent && selectedAgent !== executionOptions.data?.agents.default,
  );


  useEffect(
    () => () => {
      abortRef.current?.abort();
      for (const audio of pendingAudioRef.current) URL.revokeObjectURL(audio.previewUrl);
      for (const image of pendingImagesRef.current) URL.revokeObjectURL(image.previewUrl);
    },
    [],
  );

  function addActivity(label: string, event?: RunEvent, details: Partial<ActivityItem> = {}) {
    activityId.current += 1;
    setActivity((items) => [
      ...items,
      {
        id: activityId.current,
        type: event?.type ?? "status",
        label,
        status: details.status ?? (event?.type === "run.error" ? "error" : "info"),
        error: event?.type === "run.error",
        ...details,
      },
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
    if (event.type === "tool.call") {
      const toolName = eventToolName(event);
      addActivity(`Calling ${toolName}`, event, {
        toolName,
        toolCallId: eventToolCallId(event),
        arguments: event.arguments,
        startedAt: Date.now(),
        status: "pending",
      });
    } else if (event.type === "tool.result") {
      const toolCallId = eventToolCallId(event);
      const completedAt = Date.now();
      setActivity((items) => {
        const index = items.findIndex((item) => item.toolCallId === toolCallId);
        if (index < 0) {
          activityId.current += 1;
          return [...items, {
            id: activityId.current,
            type: event.type,
            label: `Completed ${eventToolName(event)}`,
            toolName: eventToolName(event),
            toolCallId,
            result: event.result,
            status: event.is_error === true ? "error" : "success",
            error: event.is_error === true,
          }];
        }
        const updated = [...items];
        const item = updated[index];
        updated[index] = {
          ...item,
          label: `${item.toolName ?? eventToolName(event)} ${event.is_error === true ? "failed" : "completed"}`,
          result: event.result,
          durationMs: item.startedAt === undefined ? undefined : Math.max(0, completedAt - item.startedAt),
          status: event.is_error === true ? "error" : "success",
          error: event.is_error === true,
        };
        return updated;
      });
    } else if (event.type === "llm.progress") {
      return;
    } else if (event.type === "reasoning") {
      if (typeof event.content === "string" && event.content.trim()) {
        addActivity("Thinking summary", event, { result: event.content, status: "info" });
        setLiveThinking(event.content.trim());
      }
    } else if (event.type === "assistant.message") {
      setStreamedReply(event.content ?? "");
      addActivity("Assistant response received", event);
    } else if (event.type === "run.error") {
      const message = runErrorMessage(event.detail);
      setError(message);
      addActivity(message, event);
    } else {
      addActivity(formatRunEvent(event), event);
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    const queuedAudio = [...pendingAudio];
    const queuedDocuments = [...pendingDocuments];
    const queuedImages = [...pendingImages];
    if ((!content && queuedImages.length === 0 && queuedDocuments.length === 0 && queuedAudio.length === 0) || isRunning || audioRecordingActive) return;
    if (queuedAudio.length > 0 && !audioInputAvailable) {
      setError(audioInputMessage ?? "Audio input is unavailable.");
      return;
    }
    if (queuedDocuments.length > 0 && !documentInputAvailable) {
      setError(documentInputMessage ?? "Document input is unavailable.");
      return;
    }
    if (queuedImages.length > 0 && !imageInputAvailable) {
      setError(imageInputMessage ?? "Image input is unavailable.");
      return;
    }

    setError(null);
    setDraft("");
    setStreamedReply(null);
    setLiveThinking(null);
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
      for (const audio of queuedAudio) {
        uploaded.push(await api.uploadAttachment(threadId, audio.file, controller.signal));
      }
      for (const document of queuedDocuments) {
        uploaded.push(await api.uploadAttachment(threadId, document.file, controller.signal));
      }
      const parts = uploaded.length
        ? [
            ...(content ? [{ type: "text" as const, text: content }] : []),
            ...uploaded.slice(0, queuedImages.length).map((attachment, index) => ({
              type: "image" as const,
              mime_type: queuedImages[index].file.type,
              attachment_id: attachment.attachment_id,
              detail: queuedImages[index].detail,
            })),
            ...uploaded.slice(queuedImages.length, queuedImages.length + queuedAudio.length).map((attachment, index) => ({
              type: "audio" as const,
              mime_type: queuedAudio[index].file.type,
              attachment_id: attachment.attachment_id,
              filename: queuedAudio[index].file.name,
            })),
            ...uploaded.slice(queuedImages.length + queuedAudio.length).map((attachment, index) => ({
              type: "document" as const,
              mime_type: queuedDocuments[index].file.type,
              attachment_id: attachment.attachment_id,
              filename: queuedDocuments[index].file.name,
            })),
          ]
        : undefined;
      await api.addMessage(threadId, content, parts, controller.signal);
      messageStored = true;
      for (const audio of queuedAudio) URL.revokeObjectURL(audio.previewUrl);
      for (const image of queuedImages) URL.revokeObjectURL(image.previewUrl);
      setPendingAudio([]);
      setPendingDocuments([]);
      setPendingImages([]);
      await queryClient.invalidateQueries({ queryKey: ["messages", threadId] });
      addActivity("Run started");
      await api.streamRun(threadId, handleRunEvent, controller.signal);
      setLiveThinking(null);
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

  function addAttachmentFiles(files: FileList | File[] | null) {
    if (!files?.length) return;
    const audioConfig = config.data?.audio_input;
    const imageConfig = config.data?.image_input;
    const documentConfig = config.data?.document_input;
    if (!imageConfig || !documentConfig) {
      setError("Attachment options are still loading.");
      return;
    }

    const candidates = classifyAttachmentFiles(files, imageConfig, documentConfig, audioConfig);
    if (candidates.audio.length > 0 && (!audioConfig || !audioInputAvailable)) {
      setError(audioInputMessage ?? "Audio input is unavailable.");
      return;
    }
    if (candidates.images.length > 0 && !imageInputAvailable) {
      setError(imageInputMessage ?? "Image input is unavailable.");
      return;
    }
    if (candidates.documents.length > 0 && !documentInputAvailable) {
      setError(documentInputMessage ?? "Document input is unavailable.");
      return;
    }

    const audioError = audioConfig ? validateQueueAddition(
      pendingAudio.map((audio) => audio.file),
      candidates.audio,
      {
        allowed_mime_types: audioConfig.allowed_mime_types,
        max_bytes: audioConfig.max_bytes,
        max_items: audioConfig.max_audio_files,
        max_total_bytes: audioConfig.max_total_bytes,
      },
      { singular: "audio file", plural: "audio files" },
    ) : null;
    const imageError = validateQueueAddition(
      pendingImages.map((image) => image.file),
      candidates.images,
      {
        allowed_mime_types: imageConfig.allowed_mime_types,
        max_bytes: imageConfig.max_bytes,
        max_items: imageConfig.max_images,
        max_total_bytes: imageConfig.max_total_bytes,
      },
      { singular: "image", plural: "images" },
    );
    const oversizedTextDocument = candidates.documents.find(
      (file) => file.type === "text/plain" && file.size > documentConfig.max_text_bytes,
    );
    if (oversizedTextDocument) {
      setError(`${oversizedTextDocument.name} exceeds the text-document size limit.`);
      return;
    }
    const documentError = validateQueueAddition(
      pendingDocuments.map((document) => document.file),
      candidates.documents,
      {
        allowed_mime_types: documentConfig.allowed_mime_types,
        max_bytes: documentConfig.max_bytes,
        max_items: documentConfig.max_documents,
        max_total_bytes: documentConfig.max_total_bytes,
      },
      { singular: "document", plural: "documents" },
    );
    if (audioError || imageError || documentError) {
      setError(audioError ?? imageError ?? documentError);
      return;
    }

    if (candidates.audio.length > 0) {
      setPendingAudio((audio) => [
        ...audio,
        ...candidates.audio.map((file) => ({ file, previewUrl: URL.createObjectURL(file) })),
      ]);
    }
    if (candidates.images.length > 0) {
      setPendingImages((images) => [
        ...images,
        ...candidates.images.map((file) => ({
          file,
          previewUrl: URL.createObjectURL(file),
          detail: "auto" as const,
        })),
      ]);
    }
    if (candidates.documents.length > 0) {
      setPendingDocuments((documents) => [
        ...documents,
        ...candidates.documents.map((file) => ({ file })),
      ]);
    }
    setError(
      candidates.unsupported.length > 0
        ? unsupportedAttachmentMessage(candidates.unsupported)
        : null,
    );
  }

  function isFileDrag(event: DragEvent<HTMLElement>): boolean {
    return Array.from(event.dataTransfer.types).includes("Files");
  }

  function handleAttachmentDragEnter(event: DragEvent<HTMLFormElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    if (isRunning || !attachmentDropLabel) return;
    attachmentDragDepth.current += 1;
    setAttachmentDragActive(true);
  }

  function handleAttachmentDragOver(event: DragEvent<HTMLFormElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = isRunning ? "none" : "copy";
  }

  function handleAttachmentDragLeave(event: DragEvent<HTMLFormElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    attachmentDragDepth.current = Math.max(0, attachmentDragDepth.current - 1);
    if (attachmentDragDepth.current === 0) setAttachmentDragActive(false);
  }

  function handleAttachmentDrop(event: DragEvent<HTMLFormElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    attachmentDragDepth.current = 0;
    setAttachmentDragActive(false);
    if (!isRunning) addAttachmentFiles(event.dataTransfer.files);
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

  function openThread(threadId: string, messageId?: string) {
    if (isRunning) return;
    setMobileThreadRailOpen(false);
    setSelectedThreadId(threadId);
    setHighlightedMessageId(messageId ?? null);
    setStreamedReply(null);
    setActivity([]);
    setError(null);
    setBranchNotice(null);
  }

  function newThread() {
    if (isRunning) return;
    setMobileThreadRailOpen(false);
    setSelectedThreadId(null);
    setHighlightedMessageId(null);
    setSelectedLlmProfile("");
    setStreamedReply(null);
    setActivity([]);
    setError(null);
    setBranchNotice(null);
    for (const audio of pendingAudio) URL.revokeObjectURL(audio.previewUrl);
    for (const image of pendingImages) URL.revokeObjectURL(image.previewUrl);
    setPendingAudio([]);
    setPendingDocuments([]);
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

  const listedThreads = threads.data?.threads ?? [];
  const searchResults = new Map(
    (threads.data?.searchResults ?? []).map((result) => [result.thread.thread_id, result]),
  );
  const pinnedThreads = listedThreads.filter((thread) => thread.pinned_at);
  const recentThreads = listedThreads.filter((thread) => !thread.pinned_at);
  const selectedThread = listedThreads.find((thread) => thread.thread_id === selectedThreadId)
    ?? lineage.data?.thread;

  return (
    <section className="workspace-page">
      <aside className={`thread-rail ${mobileThreadRailOpen ? "is-open" : ""}`} aria-label="Conversations">
        {sidebarHeader}
        <div className="thread-rail-heading">
          <div><p className="eyebrow">Workspace</p><h2>Conversations</h2></div>
          <button type="button" onClick={newThread} aria-label="New conversation">+</button>
        </div>
        <div className="thread-library-controls">
          <select
            value={threadSearchScope}
            onChange={(event) => setThreadSearchScope(event.target.value as "title" | "all")}
            aria-label="Conversation search scope"
          >
            <option value="title">Titles</option>
            <option value="all">All messages</option>
          </select>
          <input
            type="search"
            value={threadSearch}
            onChange={(event) => setThreadSearch(event.target.value)}
            placeholder={threadSearchScope === "title" ? "Search titles" : "Search conversations"}
            aria-label="Search conversations"
          />
          <button
            type="button"
            aria-pressed={showArchivedThreads}
            onClick={() => setShowArchivedThreads((visible) => !visible)}
          >
            {showArchivedThreads ? "Show active" : "Archived"}
          </button>
        </div>
        {threads.isError && <p className="rail-error">Connect to load conversations.</p>}
        <div className="thread-list">
          {pinnedThreads.length > 0 && <p className="thread-section-label">Pinned</p>}
          {pinnedThreads.map((thread) => (
            <ThreadButton
              key={thread.thread_id}
              thread={thread}
              active={thread.thread_id === selectedThreadId}
              snippet={searchResults.get(thread.thread_id)?.matches[0]?.snippet}
              onClick={() => openThread(
                thread.thread_id,
                searchResults.get(thread.thread_id)?.matches[0]?.message_id,
              )}
            />
          ))}
          {recentThreads.length > 0 && pinnedThreads.length > 0 && (
            <p className="thread-section-label">{showArchivedThreads ? "Archived" : "Recent"}</p>
          )}
          {recentThreads.map((thread) => (
            <ThreadButton
              key={thread.thread_id}
              thread={thread}
              active={thread.thread_id === selectedThreadId}
              snippet={searchResults.get(thread.thread_id)?.matches[0]?.snippet}
              onClick={() => openThread(
                thread.thread_id,
                searchResults.get(thread.thread_id)?.matches[0]?.message_id,
              )}
            />
          ))}
          {!threads.isPending && !listedThreads.length && (
            <p className="empty-threads">
              {threadSearch.trim()
                ? "No matching conversations."
                : showArchivedThreads
                  ? "No archived conversations."
                  : <>No conversations yet.<br />Start with a message.</>}
            </p>
          )}
        </div>
        {sidebarFooter}
      </aside>
      {mobileThreadRailOpen && <button type="button" className="thread-rail-backdrop" aria-label="Close conversations" onClick={() => setMobileThreadRailOpen(false)} />}

      <div className="conversation">
        <header className="conversation-header">
          <button type="button" className="thread-rail-toggle" aria-label="Show conversations" onClick={() => setMobileThreadRailOpen(true)}>☰</button>
          <div><span className={`run-dot ${isRunning ? "active" : ""}`} /><div><h1>{selectedThread?.title ?? "Untitled conversation"}</h1><small>{isRunning ? "Agent is working" : selectedThreadId ? "Ready" : "New conversation"}</small></div></div>
          <div className="conversation-actions">
            {activity.filter((item) => showThinking || item.type !== "reasoning").length > 0 && <span className="activity-count">{activity.filter((item) => showThinking || item.type !== "reasoning").length} event{activity.filter((item) => showThinking || item.type !== "reasoning").length === 1 ? "" : "s"}</span>}
            <button type="button" className={showThinking ? "active" : ""} onClick={() => setShowThinking((visible) => !visible)}>Thinking {showThinking ? "on" : "off"}</button>
            <details className="conversation-menu">
              <summary role="button" aria-label="Conversation actions" title="Conversation actions">•••</summary>
              <div>
                <button
                  type="button"
                  disabled={!selectedThread || isRunning || organizeThread.isPending}
                  onClick={() => selectedThread && organizeThread.mutate({
                    threadId: selectedThread.thread_id,
                    organization: { pinned: !selectedThread.pinned_at },
                  })}
                >
                  {selectedThread?.pinned_at ? "Unpin" : "Pin"}
                </button>
                <button
                  type="button"
                  disabled={!selectedThread || isRunning || organizeThread.isPending}
                  onClick={() => selectedThread && organizeThread.mutate({
                    threadId: selectedThread.thread_id,
                    organization: { archived: !selectedThread.archived_at },
                  })}
                >
                  {selectedThread?.archived_at ? "Restore" : "Archive"}
                </button>
                <button type="button" disabled={!selectedThreadId || isRunning} onClick={() => void renameSelectedThread()}>Rename</button>
                <button type="button" disabled={!selectedThreadId || isRunning} onClick={() => setContextOpen(true)}>Context</button>
              </div>
            </details>
          </div>
        </header>

        <div className="thread-lineage-slot">
          {lineage.data && (
          lineage.data.parent
          || lineage.data.thread.parent_thread_id
          || lineage.data.children.length > 0
          || lineage.data.siblings.length > 0
        ) && (
          <nav className="thread-lineage" aria-label="Conversation branches">
            <div className="lineage-origin">
              {lineage.data.parent ? (
                <><span>Branched from</span><button type="button" disabled={isRunning} onClick={() => openThread(lineage.data.parent!.thread_id)}>{lineage.data.parent.title}</button></>
              ) : lineage.data.thread.parent_thread_id ? (
                <span>Source conversation unavailable</span>
              ) : (
                <span>Original conversation</span>
              )}
            </div>
            {(lineage.data.siblings.length > 0 || lineage.data.children.length > 0) && (
              <details className="lineage-related">
                <summary>{lineage.data.siblings.length + lineage.data.children.length} related branch{lineage.data.siblings.length + lineage.data.children.length === 1 ? "" : "es"}</summary>
                <div>
                  {lineage.data.siblings.length > 0 && <strong>Sibling branches</strong>}
                  {lineage.data.siblings.map((thread) => <button type="button" key={thread.thread_id} disabled={isRunning} onClick={() => openThread(thread.thread_id)}>{thread.title}</button>)}
                  {lineage.data.children.length > 0 && <strong>Child branches</strong>}
                  {lineage.data.children.map((thread) => <button type="button" key={thread.thread_id} disabled={isRunning} onClick={() => openThread(thread.thread_id)}>{thread.title}</button>)}
                </div>
              </details>
            )}
            </nav>
          )}
        </div>

        <div className="message-scroll" aria-live="polite" tabIndex={0}>
          {!selectedThreadId && !isRunning && <Welcome />}
          {messages.isPending && selectedThreadId && <p className="loading-messages">Loading conversation…</p>}
          {visibleChatMessages(messages.data, streamedReply).map((message) => (
            <article
              id={`message-${message.id}`}
              className={`chat-message ${message.role} ${highlightedMessageId === message.id ? "search-highlight" : ""}`}
              key={message.id}
            >
              <span className="message-author">{message.role === "user" ? "You" : "Mindweft"}</span>
              {message.content && (message.role === "assistant" ? <RenderedAssistantMessage content={message.content} /> : <div className="message-content plain-message-content">{message.content}</div>)}
              <MessageImages message={message} />
              <MessageAudio message={message} />
              <MessageDocuments message={message} />
              {message.role === "assistant" && showThinking && reasoningSummary(message.metadata) && <details className="thinking-summary"><summary>Thinking summary</summary><p>{reasoningSummary(message.metadata)}</p></details>}
              {message.role === "assistant" && <PersistedToolActivity steps={persistedToolSteps(messages.data, message.id)} />}
              <div className="message-actions">
                <button
                  type="button"
                  className="message-branch-action"
                  disabled={!selectedThreadId || isRunning || forkThread.isPending}
                  onClick={() => selectedThreadId && forkThread.mutate({ threadId: selectedThreadId, messageId: message.id })}
                >
                  Branch from here
                </button>
              </div>
            </article>
          ))}
          {liveThinking !== null && isRunning && (
            <div className="thinking-live" aria-live="polite">
              <span>Thinking</span>
              <p>{liveThinking}</p>
            </div>
          )}
          {streamedReply !== null && <article className="chat-message assistant streaming"><span className="message-author">Mindweft</span><RenderedAssistantMessage content={streamedReply} /></article>}
          {isRunning && streamedReply === null && <div className="thinking-row"><i /><i /><i /><span>Working</span></div>}
          {branchNotice && <div className="branch-notice" role="status">{branchNotice}</div>}
          {error && <div className="conversation-error" role="alert">{error}</div>}
          <div ref={messagesEndRef} />
        </div>

        {activity.filter((item) => showThinking || item.type !== "reasoning").length > 0 && (
          <details className="activity-tray">
            <summary><span>Run activity</span><small>{activity.at(-1)?.label}</small></summary>
            <ol>{activity.filter((item) => showThinking || item.type !== "reasoning").map((item) => (
              <li className={`activity-${item.status}${item.error ? " error" : ""}`} key={item.id}>
                <span aria-hidden="true">{activityStatusIcon(item.status)}</span>
                <div className="activity-item-content">
                  <strong>{item.label}</strong>
                  {item.durationMs !== undefined && <small>{formatDuration(item.durationMs)}</small>}
                  {(item.arguments !== undefined || item.result !== undefined) && (
                    <details className="activity-details">
                      <summary>Details</summary>
                      {item.arguments !== undefined && <ActivityPayload label="Arguments" value={item.arguments} />}
                      {item.result !== undefined && <ActivityPayload label="Result" value={item.result} />}
                    </details>
                  )}
                </div>
              </li>
            ))}</ol>
          </details>
        )}

        <form
          className={`chat-composer${selectedThreadId === null ? "" : " has-thread"}${attachmentDragActive ? " is-dragging-attachments" : ""}`}
          onSubmit={(event) => void sendMessage(event)}
          onDragEnter={handleAttachmentDragEnter}
          onDragOver={handleAttachmentDragOver}
          onDragLeave={handleAttachmentDragLeave}
          onDrop={handleAttachmentDrop}
        >
          {attachmentDragActive && (
            <div className="attachment-drop-target" role="status">{attachmentDropLabel}</div>
          )}
          {pendingAudio.length > 0 && (
            <div className="pending-audio">
              {pendingAudio.map((audio, index) => (
                <div className="pending-audio-item" key={audio.previewUrl}>
                  <strong>{audio.file.name}</strong>
                  <audio controls preload="metadata" src={audio.previewUrl}><track kind="captions" /></audio>
                  <button type="button" aria-label={`Remove ${audio.file.name}`} onClick={() => setPendingAudio((items) => {
                    URL.revokeObjectURL(items[index].previewUrl);
                    return items.filter((_, itemIndex) => itemIndex !== index);
                  })}>×</button>
                </div>
              ))}
            </div>
          )}
          {pendingDocuments.length > 0 && (
            <div className="pending-documents">
              {pendingDocuments.map((document, index) => (
                <div className="pending-document" key={`${document.file.name}-${String(index)}`}>
                  <span className={document.file.type === "text/plain" ? "text" : undefined} aria-hidden="true">{document.file.type === "text/plain" ? "TXT" : "PDF"}</span><strong>{document.file.name}</strong>
                  <button type="button" aria-label={`Remove ${document.file.name}`} onClick={() => setPendingDocuments((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button>
                </div>
              ))}
            </div>
          )}
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
            <div className="agent-selector-setting">
              <label className="agent-selector">
                <span>Agent</span>
                <select
                  aria-label="Agent"
                  value={effectiveAgent}
                  disabled={selectedThreadId !== null || isRunning || executionOptions.isPending}
                  onChange={(event) => {
                    setSelectedAgent(event.target.value);
                    setDefaultAgentFeedback(null);
                  }}
                >
                  {!executionOptions.data?.agents.items.length && <option value="">Default agent</option>}
                  {executionOptions.data?.agents.items.map((agent) => {
                    const value = agent.id ?? agent.name;
                    const profile = agent.llm_profile ? ` · ${agent.llm_profile.replace(/^shared:/, "")}` : "";
                    return <option key={value} value={value}>{agent.display_name ?? agent.name}{profile}</option>;
                  })}
                </select>
              </label>
              {canSaveDefaultAgent && (
                <button
                  type="button"
                  className="save-default-agent"
                  disabled={saveDefaultAgent.isPending}
                  onClick={() => saveDefaultAgent.mutate(selectedAgent)}
                >{saveDefaultAgent.isPending ? "Saving…" : "Make default"}</button>
              )}
              {defaultAgentFeedback && (
                <small className={defaultAgentFeedback.error ? "error" : ""} role={defaultAgentFeedback.error ? "alert" : "status"}>
                  {defaultAgentFeedback.message}
                </small>
              )}
            </div>
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
                  const capability = profile.image_input_reason === "profile_unsupported"
                    ? " · text only"
                    : "";
                  return <option key={value} value={value}>{profile.display_name ?? profile.name}{capability}</option>;
                })}
              </select>
            </label>
          </div>
          {queuedAudioBlocked && profileAudioUnavailable && (
            <p className="composer-capability-warning" role="alert">
              {profileAudioUnavailable} Remove the queued audio or choose an audio-capable profile.
            </p>
          )}
          {profileImageUnavailable && (
            <p className="composer-capability-warning" role={queuedImagesBlocked ? "alert" : "status"}>
              {profileImageUnavailable}
              {queuedImagesBlocked ? " Remove the queued images or choose an image-capable profile." : ""}
            </p>
          )}
          {queuedDocumentsBlocked && profileDocumentUnavailable && (
            <p className="composer-capability-warning" role={queuedDocumentsBlocked ? "alert" : "status"}>
              {profileDocumentUnavailable}
              {queuedDocumentsBlocked ? " Remove the queued documents or choose a document-capable profile." : ""}
            </p>
          )}
          <div className="composer-attachment-actions">
            <AudioRecorder
            disabled={isRunning || !audioInputAvailable}
            maxBytes={config.data?.audio_input?.max_bytes ?? 1}
            maxDurationSeconds={config.data?.audio_input?.max_duration_seconds ?? 1}
            unavailableReason={audioInputMessage}
            onError={(message) => setError(message || null)}
            onRecorded={(file) => addAttachmentFiles([file])}
            onRecordingChange={setAudioRecordingActive}
          />
          <label
            className={`attach-image ${audioInputAvailable ? "" : "disabled"}`}
            title={audioInputAvailable ? "Attach WAV audio" : (audioInputMessage ?? "Audio input is unavailable")}
          >
            <span aria-hidden="true">WAV</span><span className="sr-only">Attach WAV audio</span>
            <input
              type="file"
              accept={config.data?.audio_input?.allowed_mime_types.join(",") ?? "audio/wav"}
              multiple
              disabled={isRunning || !audioInputAvailable}
              onChange={(event) => { addAttachmentFiles(event.target.files); event.target.value = ""; }}
            />
          </label>
          <label
            className={`attach-image ${documentInputAvailable ? "" : "disabled"}`}
            title={documentInputAvailable ? "Attach documents" : (documentInputMessage ?? "Document input is unavailable")}
          >
            <span aria-hidden="true">DOC</span><span className="sr-only">Attach documents</span>
            <input
              type="file"
              accept={documentAccept}
              multiple
              disabled={isRunning || !documentInputAvailable}
              onChange={(event) => { addAttachmentFiles(event.target.files); event.target.value = ""; }}
            />
          </label>
          <label
            className={`attach-image ${imageInputAvailable ? "" : "disabled"}`}
            title={imageInputAvailable ? "Attach images" : (imageInputMessage ?? "Image input is unavailable")}
          >
            <span aria-hidden="true">+</span><span className="sr-only">Attach images</span>
            <input
              type="file"
              accept={config.data?.image_input.allowed_mime_types.join(",") ?? "image/*"}
              multiple
              disabled={isRunning || !imageInputAvailable}
              onChange={(event) => { addAttachmentFiles(event.target.files); event.target.value = ""; }}
            />
            </label>
          </div>
          <textarea
            ref={messageInputRef}
            aria-label="Message Mindweft"
            placeholder="Ask Mindweft anything…"
            value={draft}
            rows={1}
            disabled={isRunning}
            onChange={(event) => setDraft(event.target.value)}
            onPaste={(event: ClipboardEvent<HTMLTextAreaElement>) => {
              const clipboardFiles = event.clipboardData.files.length > 0
                ? Array.from(event.clipboardData.files)
                : Array.from(event.clipboardData.items)
                    .filter((item) => item.kind === "file")
                    .map((item) => item.getAsFile())
                    .filter((file): file is File => file !== null);
              if (clipboardFiles.length > 0) {
                event.preventDefault();
                addAttachmentFiles(clipboardFiles);
              }
            }}
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
            <button className="send-message" type="submit" disabled={audioRecordingActive || queuedAudioBlocked || queuedDocumentsBlocked || queuedImagesBlocked || (!draft.trim() && pendingAudio.length === 0 && pendingImages.length === 0 && pendingDocuments.length === 0)} aria-label="Send message">↑</button>
          )}
          <small>
            Enter to send · Shift+Enter for a new line
            {imageInputAvailable || documentInputAvailable || audioInputAvailable ? " · Paste or drop attachments" : ""}
          </small>
        </form>
      </div>
      <ContextDialog
        threadId={selectedThreadId}
        open={contextOpen}
        onClose={() => setContextOpen(false)}
        onThreadCompacted={setSelectedThreadId}
      />
      <ConsentDialog
        threadId={selectedThreadId}
        request={consentRequest ?? pendingConsents.data?.[0] ?? null}
        onResolved={resolveConsent}
        onError={(message) => setError(message)}
      />
    </section>
  );
}

function ThreadButton({
  thread,
  active,
  snippet,
  onClick,
}: {
  thread: ThreadListItem;
  active: boolean;
  snippet?: string;
  onClick: () => void;
}) {
  const title = thread.title?.trim() || "New conversation";
  const context = thread.skill_name?.replace(/^(?:shared|user):/, "") || thread.capability_profile?.replace(/^(?:shared|user):/, "") || thread.llm_profile?.replace(/^shared:/, "") || "Default";
  const shortId = thread.thread_id.slice(0, 4);
  return (
    <button className={`thread-button ${active ? "active" : ""}`} type="button" onClick={onClick} title={`${title} · ${context} · ${thread.thread_id}`}>
      <span className="thread-title">{title}{thread.parent_thread_id && <em className="thread-branch-badge">Branch</em>}</span>
      {snippet && <span className="thread-search-snippet">{snippet}</span>}
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

function activityStatusIcon(status: ActivityStatus): string {
  if (status === "pending") return "\u2026";
  if (status === "success") return "\u2713";
  if (status === "error") return "!";
  return "\u00b7";
}

function PersistedToolActivity({ steps }: { steps: PersistedToolStep[] }) {
  if (steps.length === 0) return null;
  const failed = steps.filter((step) => step.status === "error").length;
  return (
    <details className="persisted-tool-activity">
      <summary>
        <span>{failed ? "Tool activity · attention needed" : "Tool activity"}</span>
        <small>{steps.length} step{steps.length === 1 ? "" : "s"}</small>
      </summary>
      <ol className="persisted-tool-list">
        {steps.map((step) => (
          <li className={`activity-${step.status}`} key={step.toolCall.id}>
            <span aria-hidden="true">{activityStatusIcon(step.status)}</span>
            <div className="activity-item-content">
              <strong>{step.toolCall.tool_name ?? "tool"}</strong>
              <details className="activity-details">
                <summary>Details</summary>
                {step.toolCall.tool_arguments !== undefined && (
                  <ActivityPayload label="Arguments" value={step.toolCall.tool_arguments} />
                )}
                {step.result && <ActivityPayload label="Result" value={step.result.content} />}
              </details>
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function ActivityPayload({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="activity-payload">
      <small>{label}</small>
      <pre>{formatPayload(value)}</pre>
    </div>
  );
}

function formatPayload(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${String(milliseconds)}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function eventToolName(event: RunEvent): string {
  if (typeof event.name === "string" && event.name.trim()) return event.name;
  if (typeof event.tool_name === "string" && event.tool_name.trim()) return event.tool_name;
  return "tool";
}

function eventToolCallId(event: RunEvent): string | undefined {
  if (typeof event.tool_call_id === "string") return event.tool_call_id;
  if (typeof event.call_id === "string") return event.call_id;
  return undefined;
}

function formatRunEvent(event: RunEvent) {
  const labels: Record<string, string> = {
    "run.started": "Run started",
    "llm.request": "Requesting model response",
    "llm.response": "Model response received",
    "tool.call": `Calling ${eventToolName(event)}`,
    "tool.result": `Completed ${eventToolName(event)}`,
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

function MessageAudio({ message }: { message: Message }) {
  const audioParts = message.parts?.filter((part): part is AudioPart => part.type === "audio") ?? [];
  if (audioParts.length === 0) return null;
  return <div className="message-audio-list">{audioParts.map((audio) => <AudioAttachment key={audio.attachment_id} threadId={message.thread_id} audio={audio} />)}</div>;
}

function MessageDocuments({ message }: { message: Message }) {
  const documents = message.parts?.filter((part): part is DocumentPart => part.type === "document") ?? [];
  if (documents.length === 0) return null;
  return <div className="message-documents">{documents.map((document) => <DocumentAttachment key={document.attachment_id} threadId={message.thread_id} document={document} />)}</div>;
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
