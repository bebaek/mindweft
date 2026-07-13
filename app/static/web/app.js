const storageKey = "minigent.webClient.v1";

const defaults = {
  baseUrl: window.location.origin,
  apiToken: "",
  userId: "demo-user",
  tenantId: "demo-tenant",
  skill: "",
  capabilityProfile: "",
  threadId: "",
};

const state = { ...defaults, ...loadState() };
const runState = {
  abortController: null,
  activityEvents: [],
  cancelRequested: false,
  currentThreadId: "",
};

const elements = {
  status: document.querySelector("#status"),
  threadsButton: document.querySelector("#threads-button"),
  contextButton: document.querySelector("#context-button"),
  settingsButton: document.querySelector("#settings-button"),
  newThreadButton: document.querySelector("#new-thread-button"),
  settingsPanel: document.querySelector("#settings-panel"),
  baseUrl: document.querySelector("#base-url"),
  apiToken: document.querySelector("#api-token"),
  userId: document.querySelector("#user-id"),
  tenantId: document.querySelector("#tenant-id"),
  skill: document.querySelector("#skill"),
  skillOptions: document.querySelector("#skill-options"),
  capabilityProfile: document.querySelector("#capability-profile"),
  capabilityProfileOptions: document.querySelector("#capability-profile-options"),
  executionOptionsStatus: document.querySelector("#execution-options-status"),
  messages: document.querySelector("#messages"),
  activityButton: document.querySelector("#activity-button"),
  runStatusLabel: document.querySelector("#run-status-label"),
  runStatusHint: document.querySelector("#run-status-hint"),
  activityBackdrop: document.querySelector("#activity-backdrop"),
  activitySheet: document.querySelector("#activity-sheet"),
  activityClose: document.querySelector("#activity-close"),
  activitySummary: document.querySelector("#activity-summary"),
  activityList: document.querySelector("#activity-list"),
  contextBackdrop: document.querySelector("#context-backdrop"),
  contextSheet: document.querySelector("#context-sheet"),
  contextClose: document.querySelector("#context-close"),
  contextSummaryLine: document.querySelector("#context-summary-line"),
  contextTotalTokens: document.querySelector("#context-total-tokens"),
  contextMessageCount: document.querySelector("#context-message-count"),
  contextSummarizedCount: document.querySelector("#context-summarized-count"),
  contextUnsummarizedCount: document.querySelector("#context-unsummarized-count"),
  contextSummary: document.querySelector("#context-summary"),
  contextRendered: document.querySelector("#context-rendered"),
  compactContextButton: document.querySelector("#compact-context-button"),
  threadsBackdrop: document.querySelector("#threads-backdrop"),
  threadsSheet: document.querySelector("#threads-sheet"),
  threadsClose: document.querySelector("#threads-close"),
  threadsSummary: document.querySelector("#threads-summary"),
  threadsList: document.querySelector("#threads-list"),
  threadsNewButton: document.querySelector("#threads-new-button"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#message-input"),
  sendButton: document.querySelector("#send-button"),
  stopButton: document.querySelector("#stop-button"),
};

syncViewportHeight();
hydrateForm();
loadExecutionOptions();
renderMessages([]);
setStatus(state.threadId ? `Thread ${state.threadId}` : "Ready");
updateThreadControls();
if (state.threadId) {
  refreshMessages();
}

elements.settingsButton.addEventListener("click", () => {
  elements.settingsPanel.classList.toggle("open");
});

elements.newThreadButton.addEventListener("click", startNewThread);

elements.threadsButton.addEventListener("click", openThreadsSheet);
elements.threadsClose.addEventListener("click", closeThreadsSheet);
elements.threadsBackdrop.addEventListener("click", closeThreadsSheet);
elements.threadsNewButton.addEventListener("click", () => {
  startNewThread();
  closeThreadsSheet();
});
elements.activityButton.addEventListener("click", openActivitySheet);
elements.activityClose.addEventListener("click", closeActivitySheet);
elements.activityBackdrop.addEventListener("click", closeActivitySheet);
elements.contextButton.addEventListener("click", openContextSheet);
elements.contextClose.addEventListener("click", closeContextSheet);
elements.contextBackdrop.addEventListener("click", closeContextSheet);
elements.compactContextButton.addEventListener("click", compactThreadContext);
elements.stopButton.addEventListener("click", cancelActiveRun);

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeActivitySheet();
    closeContextSheet();
    closeThreadsSheet();
  }
});

for (const input of [elements.skill, elements.capabilityProfile]) {
  input.addEventListener("change", saveFormState);
}

for (const input of [elements.baseUrl, elements.apiToken, elements.userId, elements.tenantId]) {
  input.addEventListener("change", () => {
    saveFormState();
    loadExecutionOptions();
  });
}

elements.messageInput.addEventListener("input", () => {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${elements.messageInput.scrollHeight}px`;
});

window.visualViewport?.addEventListener("resize", syncViewportHeight);
window.visualViewport?.addEventListener("scroll", syncViewportHeight);
window.addEventListener("resize", syncViewportHeight);

elements.messageInput.addEventListener("focus", () => {
  syncViewportHeight();
  scrollMessagesToBottom();
});

elements.messageInput.addEventListener("blur", () => {
  // Let taps on Send complete before dropping the mobile keyboard clearance;
  // otherwise the button can move under the finger and cancel the click.
  window.setTimeout(syncViewportHeight, 300);
});

elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && shouldSubmitOnEnter(event)) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

elements.composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = elements.messageInput.value.trim();
  if (!content) {
    return;
  }

  saveFormState();
  setBusy(true);
  setStatus("Sending");

  try {
    const threadId = await ensureThread();
    appendMessage({ role: "user", content });
    elements.messageInput.value = "";
    elements.messageInput.style.height = "auto";

    await requestJson(`/threads/${encodeURIComponent(threadId)}/messages`, {
      method: "POST",
      body: { content },
    });

    setStatus("Running");
    await streamRun(threadId);
    updateThreadControls();
    if (!elements.threadsSheet.hidden) {
      await loadThreadList();
    }
    setStatus(`Thread ${threadId}`);
  } catch (error) {
    if (runState.cancelRequested || error.name === "AbortError") {
      appendNotice("Run cancelled.");
      setRunStatus("Activity", {
        completed: true,
        forceVisible: true,
        hint: "Cancelled",
      });
      setStatus(`Thread ${threadId}`);
    } else {
      appendNotice(`Run failed: ${error.message}`);
      setRunStatus("Activity", {
        completed: true,
        error: true,
        forceVisible: true,
        hint: "Run failed",
      });
      setStatus(error.message, true);
    }
  } finally {
    runState.abortController = null;
    runState.cancelRequested = false;
    runState.currentThreadId = "";
    setBusy(false);
    elements.messageInput.focus();
  }
});

function startNewThread() {
  if (runState.abortController) {
    return;
  }
  state.threadId = "";
  saveFormState();
  renderMessages([]);
  clearActivity();
  clearContext();
  updateThreadControls();
  setStatus("Ready");
  elements.messageInput.focus();
}

async function refreshMessages() {
  saveFormState();
  setStatus("Loading");
  try {
    const messages = await requestJson(`/threads/${encodeURIComponent(state.threadId)}/messages`);
    renderMessages(messages);
    setStatus(`Thread ${state.threadId}`);
  } catch (error) {
    if (error.status === 404) {
      state.threadId = "";
      saveState();
      renderMessages([]);
      clearContext();
      updateThreadControls();
      appendNotice("Previous thread was no longer available. Started a fresh session.");
      setStatus("Ready");
      return;
    }
    renderMessages([]);
    setStatus(error.message, true);
  }
}

async function loadExecutionOptions() {
  saveFormState();
  elements.executionOptionsStatus.textContent = "Loading execution options…";
  try {
    const options = await requestJson("/execution-options");
    hydrateOptionList(elements.skillOptions, options.skills?.items || []);
    hydrateOptionList(
      elements.capabilityProfileOptions,
      options.capability_profiles?.items || []
    );
    const skillSummary = summarizeOptionSection("skill", options.skills);
    const capabilitySummary = summarizeOptionSection(
      "capability profile",
      options.capability_profiles
    );
    elements.executionOptionsStatus.textContent = `${skillSummary}; ${capabilitySummary}. You can still type a custom value.`;
    if (!elements.skill.placeholder || elements.skill.placeholder === "default") {
      elements.skill.placeholder = options.skills?.default || "default";
    }
    if (
      !elements.capabilityProfile.placeholder ||
      elements.capabilityProfile.placeholder === "default"
    ) {
      elements.capabilityProfile.placeholder = options.capability_profiles?.default || "default";
    }
  } catch (error) {
    elements.executionOptionsStatus.textContent =
      "Could not load execution options; manual skill/profile entry is still available.";
  }
}

function hydrateOptionList(datalist, items) {
  datalist.replaceChildren();
  for (const item of items) {
    if (!item?.name) {
      continue;
    }
    const option = document.createElement("option");
    option.value = item.name;
    if (item.description) {
      option.label = item.description;
    }
    datalist.append(option);
  }
}

function summarizeOptionSection(label, section) {
  const count = section?.items?.length || 0;
  const defaultName = section?.default;
  const suffix = count === 1 ? "" : "s";
  if (defaultName) {
    return `${count} ${label}${suffix}, default ${defaultName}`;
  }
  return `${count} ${label}${suffix}`;
}

function hydrateForm() {
  elements.baseUrl.value = state.baseUrl;
  elements.apiToken.value = state.apiToken;
  elements.userId.value = state.userId;
  elements.tenantId.value = state.tenantId;
  elements.skill.value = state.skill;
  elements.capabilityProfile.value = state.capabilityProfile;
}

function saveFormState() {
  state.baseUrl = elements.baseUrl.value.trim().replace(/\/+$/, "") || defaults.baseUrl;
  state.apiToken = elements.apiToken.value.trim();
  state.userId = elements.userId.value.trim() || defaults.userId;
  state.tenantId = elements.tenantId.value.trim() || defaults.tenantId;
  state.skill = elements.skill.value.trim();
  state.capabilityProfile = elements.capabilityProfile.value.trim();
  saveState();
}

function loadState() {
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKey) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function saveState() {
  window.localStorage.setItem(storageKey, JSON.stringify(state));
}

async function ensureThread() {
  if (state.threadId) {
    return state.threadId;
  }

  const body = {};
  if (state.skill) {
    body.skill_name = state.skill;
  }
  if (state.capabilityProfile) {
    body.capability_profile = state.capabilityProfile;
  }

  const response = await requestJson("/threads", { method: "POST", body });
  state.threadId = response.thread_id;
  saveState();
  updateThreadControls();
  if (!elements.threadsSheet.hidden) {
    await loadThreadList();
  }
  return state.threadId;
}

async function streamRun(threadId) {
  let assistantMessage = null;
  let runFailed = false;
  let runErrorMessage = "Run failed";
  const peerTaskStatuses = new Map();
  const seenProgressLabels = new Set();
  runState.abortController = new AbortController();
  runState.currentThreadId = threadId;
  runState.cancelRequested = false;
  clearActivity();
  setRunStatus("Thinking…");
  addActivityEvent("Run started");
  await requestNdjson(`/threads/${encodeURIComponent(threadId)}/run/stream`, {
    method: "POST",
    signal: runState.abortController.signal,
    onEvent(event) {
      if (event.type === "assistant.message") {
        assistantMessage = appendMessage({ role: "assistant", content: event.content || "" });
        setRunStatus("Activity", { completed: true, hint: "Response ready" });
        addActivityEvent("Assistant response received", event);
        return;
      }
      if (event.type === "run.error") {
        runFailed = true;
        runErrorMessage = `${event.status_code || "error"} ${event.detail || ""}`.trim();
        addActivityEvent(`Run failed: ${runErrorMessage}`, event, true);
        setStatus(event.detail || "Run failed", true);
        return;
      }
      const label = formatRunEvent(event, peerTaskStatuses);
      if (label && !seenProgressLabels.has(label)) {
        seenProgressLabels.add(label);
        addActivityEvent(label, event);
        setRunStatus(label);
        setStatus(label);
      }
    },
  });
  if (runState.cancelRequested) {
    const error = new Error("Run cancelled");
    error.name = "AbortError";
    throw error;
  }
  if (runFailed) {
    throw new Error(runErrorMessage);
  }
  if (!assistantMessage && !runState.cancelRequested) {
    throw new Error("Run stream ended without an assistant message.");
  }
}

async function requestJson(path, options = {}) {
  const response = await fetch(`${state.baseUrl}${path}`, {
    method: options.method || "GET",
    headers: {
      ...authHeaders(),
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(`${response.status} ${detail || response.statusText}`);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

async function requestNdjson(path, options = {}) {
  const response = await fetch(`${state.baseUrl}${path}`, {
    method: options.method || "GET",
    headers: {
      ...authHeaders(),
      Accept: "application/x-ndjson",
    },
    signal: options.signal,
  });

  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(`${response.status} ${detail || response.statusText}`);
    error.status = response.status;
    throw error;
  }

  if (!response.body) {
    throw new Error("Streaming responses are not supported by this browser.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed) {
        options.onEvent?.(JSON.parse(trimmed));
      }
    }
    if (done) {
      break;
    }
  }
  const finalLine = buffer.trim();
  if (finalLine) {
    options.onEvent?.(JSON.parse(finalLine));
  }
}

function authHeaders() {
  if (state.apiToken) {
    return { Authorization: `Bearer ${state.apiToken}` };
  }
  return {
    "X-Minigent-User-Id": state.userId,
    "X-Minigent-Tenant-Id": state.tenantId,
    "X-Minigent-Admin": "false",
  };
}

function renderMessages(messages) {
  elements.messages.replaceChildren();
  const visibleMessages = messages.filter((message) =>
    ["user", "assistant"].includes(message.role || "")
  );
  if (visibleMessages.length === 0) {
    const empty = document.createElement("article");
    empty.className = "message assistant";
    empty.textContent = "Start a thread from this browser.";
    elements.messages.append(empty);
    return;
  }
  for (const message of visibleMessages) {
    appendMessage(message);
  }
}

function appendMessage(message) {
  if (
    elements.messages.children.length === 1 &&
    elements.messages.firstElementChild.textContent === "Start a thread from this browser."
  ) {
    elements.messages.replaceChildren();
  }

  const item = document.createElement("article");
  item.className = `message ${message.role || "assistant"}`;
  const role = document.createElement("span");
  role.className = "role";
  role.textContent = message.role || "assistant";
  const content = document.createElement("div");
  content.className = "content";
  renderMessageContent(content, message.content || "", message.role || "assistant");
  item.append(role, content);
  elements.messages.append(item);
  scrollMessagesToBottom();
  return item;
}

function renderMessageContent(target, text, role) {
  target.replaceChildren();
  if (role !== "assistant") {
    target.textContent = text;
    return;
  }

  for (const node of renderMarkdownBlocks(text)) {
    target.append(node);
  }
}

function renderMarkdownBlocks(text) {
  const nodes = [];
  const fencePattern = /```([^\n`]*)\n?([\s\S]*?)```/g;
  let cursor = 0;
  let match;
  while ((match = fencePattern.exec(text)) !== null) {
    appendTextBlock(nodes, text.slice(cursor, match.index));
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    const language = match[1].trim();
    if (language) {
      code.dataset.language = language;
    }
    code.textContent = match[2].replace(/\n$/, "");
    pre.append(code);
    nodes.push(pre);
    cursor = match.index + match[0].length;
  }
  appendTextBlock(nodes, text.slice(cursor));
  return nodes.length ? nodes : [document.createTextNode("")];
}

function appendTextBlock(nodes, text) {
  if (!text) {
    return;
  }
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    if (index > 0) {
      nodes.push(document.createElement("br"));
    }
    nodes.push(...renderInlineMarkdown(line));
  });
}

function renderInlineMarkdown(text) {
  const nodes = [];
  let cursor = 0;
  while (cursor < text.length) {
    const next = findNextInlineToken(text, cursor);
    if (!next) {
      nodes.push(document.createTextNode(text.slice(cursor)));
      break;
    }
    if (next.index > cursor) {
      nodes.push(document.createTextNode(text.slice(cursor, next.index)));
    }
    nodes.push(next.node);
    cursor = next.end;
  }
  return nodes;
}

function findNextInlineToken(text, start) {
  const candidates = [
    findInlineCode(text, start),
    findBold(text, start),
    findLink(text, start),
  ].filter(Boolean);
  candidates.sort((left, right) => left.index - right.index);
  return candidates[0] || null;
}

function findInlineCode(text, start) {
  const index = text.indexOf("`", start);
  if (index === -1) {
    return null;
  }
  const endMarker = text.indexOf("`", index + 1);
  if (endMarker === -1) {
    return null;
  }
  const code = document.createElement("code");
  code.textContent = text.slice(index + 1, endMarker);
  return { index, end: endMarker + 1, node: code };
}

function findBold(text, start) {
  const index = text.indexOf("**", start);
  if (index === -1) {
    return null;
  }
  const endMarker = text.indexOf("**", index + 2);
  if (endMarker === -1) {
    return null;
  }
  const strong = document.createElement("strong");
  strong.textContent = text.slice(index + 2, endMarker);
  return { index, end: endMarker + 2, node: strong };
}

function findLink(text, start) {
  const match = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/.exec(text.slice(start));
  if (!match) {
    return null;
  }
  const index = start + match.index;
  const link = document.createElement("a");
  link.textContent = match[1];
  link.href = match[2];
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return { index, end: index + match[0].length, node: link };
}

function appendNotice(content) {
  return appendInlineMessage("notice", content);
}

function appendProgress(content) {
  return appendInlineMessage("progress", content);
}

async function openThreadsSheet() {
  elements.threadsBackdrop.hidden = false;
  elements.threadsSheet.hidden = false;
  requestAnimationFrame(() => {
    elements.threadsBackdrop.classList.add("open");
    elements.threadsSheet.classList.add("open");
  });
  await loadThreadList();
}

function closeThreadsSheet() {
  elements.threadsBackdrop.classList.remove("open");
  elements.threadsSheet.classList.remove("open");
  window.setTimeout(() => {
    if (!elements.threadsSheet.classList.contains("open")) {
      elements.threadsBackdrop.hidden = true;
      elements.threadsSheet.hidden = true;
    }
  }, 180);
}

async function loadThreadList() {
  saveFormState();
  elements.threadsSummary.textContent = "Loading recent conversations…";
  elements.threadsList.replaceChildren();
  try {
    const response = await requestJson("/threads?limit=50");
    renderThreadList(response.threads || []);
    elements.threadsSummary.textContent = `${formatNumber(response.total || 0)} conversation${
      response.total === 1 ? "" : "s"
    }`;
  } catch (error) {
    elements.threadsSummary.textContent = `Could not load threads: ${error.message}`;
  }
}

function renderThreadList(threads) {
  elements.threadsList.replaceChildren();
  if (threads.length === 0) {
    const empty = document.createElement("li");
    empty.className = "thread-empty";
    empty.textContent = "No threads yet.";
    elements.threadsList.append(empty);
    return;
  }
  for (const thread of threads) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = thread.thread_id === state.threadId ? "thread-item current" : "thread-item";
    button.addEventListener("click", () => selectThread(thread.thread_id));

    const title = document.createElement("span");
    title.className = "thread-title";
    title.textContent = thread.title || "New thread";

    const meta = document.createElement("span");
    meta.className = "thread-meta";
    meta.textContent = formatThreadMeta(thread);

    button.append(title, meta);
    item.append(button);
    elements.threadsList.append(item);
  }
}

function formatThreadMeta(thread) {
  const parts = [];
  if (thread.message_count !== undefined) {
    parts.push(`${thread.message_count} message${thread.message_count === 1 ? "" : "s"}`);
  }
  if (thread.capability_profile) {
    parts.push(thread.capability_profile);
  }
  if (thread.updated_at) {
    parts.push(formatRelativeTime(thread.updated_at));
  }
  return parts.join(" · ");
}

function formatRelativeTime(value) {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return "";
  }
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.floor(hours / 24);
  if (days < 7) {
    return `${days}d ago`;
  }
  return new Date(timestamp).toLocaleDateString();
}

async function selectThread(threadId) {
  if (!threadId || threadId === state.threadId || runState.abortController) {
    closeThreadsSheet();
    return;
  }
  state.threadId = threadId;
  saveState();
  clearActivity();
  clearContext();
  updateThreadControls();
  renderMessages([]);
  closeThreadsSheet();
  await refreshMessages();
}

function updateThreadControls() {
  elements.contextButton.hidden = !state.threadId;
}

function clearContext() {
  elements.contextSummaryLine.textContent = "No context loaded";
  elements.contextTotalTokens.textContent = "—";
  elements.contextMessageCount.textContent = "—";
  elements.contextSummarizedCount.textContent = "—";
  elements.contextUnsummarizedCount.textContent = "—";
  elements.contextSummary.textContent = "No summary yet.";
  elements.contextRendered.textContent = "Load context to preview.";
  elements.compactContextButton.disabled = !state.threadId;
}

async function openContextSheet() {
  if (!state.threadId) {
    return;
  }
  elements.contextBackdrop.hidden = false;
  elements.contextSheet.hidden = false;
  requestAnimationFrame(() => {
    elements.contextBackdrop.classList.add("open");
    elements.contextSheet.classList.add("open");
  });
  await loadThreadContext();
}

function closeContextSheet() {
  elements.contextBackdrop.classList.remove("open");
  elements.contextSheet.classList.remove("open");
  window.setTimeout(() => {
    if (!elements.contextSheet.classList.contains("open")) {
      elements.contextBackdrop.hidden = true;
      elements.contextSheet.hidden = true;
    }
  }, 180);
}

async function loadThreadContext() {
  if (!state.threadId) {
    clearContext();
    return;
  }
  elements.contextSummaryLine.textContent = "Loading context…";
  elements.compactContextButton.disabled = true;
  try {
    const context = await requestJson(
      `/threads/${encodeURIComponent(state.threadId)}/context/raw`
    );
    renderThreadContext(context);
  } catch (error) {
    elements.contextSummaryLine.textContent = `Could not load context: ${error.message}`;
  } finally {
    elements.compactContextButton.disabled = !state.threadId;
  }
}

function renderThreadContext(context) {
  const usage = context.usage || {};
  elements.contextSummaryLine.textContent = `Thread ${context.thread_id || state.threadId}`;
  elements.contextTotalTokens.textContent = formatNumber(usage.total_tokens);
  elements.contextMessageCount.textContent = formatNumber(
    usage.message_count ?? context.messages?.length
  );
  elements.contextSummarizedCount.textContent = formatNumber(
    usage.summarized_message_count ?? context.summarized_message_count
  );
  elements.contextUnsummarizedCount.textContent = formatNumber(
    usage.unsummarized_message_count
  );
  elements.contextSummary.textContent = context.summary || "No summary yet.";
  elements.contextRendered.textContent = context.rendered || "No rendered context available.";
}

async function compactThreadContext() {
  if (!state.threadId) {
    return;
  }
  elements.compactContextButton.disabled = true;
  elements.contextSummaryLine.textContent = "Compacting context…";
  try {
    const result = await requestJson(`/threads/${encodeURIComponent(state.threadId)}/compact`, {
      method: "POST",
    });
    elements.contextSummaryLine.textContent = `Compacted ${formatNumber(
      result.compacted_message_count
    )} message${result.compacted_message_count === 1 ? "" : "s"}.`;
    await refreshMessages();
    await loadThreadContext();
  } catch (error) {
    elements.contextSummaryLine.textContent = `Could not compact context: ${error.message}`;
  } finally {
    elements.compactContextButton.disabled = !state.threadId;
  }
}

function formatNumber(value) {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

function clearActivity() {
  runState.activityEvents = [];
  elements.activityList.replaceChildren();
  elements.activitySummary.textContent = "No activity yet";
  elements.runStatusLabel.textContent = "Ready";
  elements.runStatusHint.textContent = "Activity";
  elements.activityButton.classList.remove("complete", "error");
  elements.activityButton.hidden = true;
}

function addActivityEvent(label, event = null, isError = false) {
  if (!label) {
    return;
  }
  const activityEvent = { label, event, isError, time: new Date() };
  runState.activityEvents.push(activityEvent);
  elements.activityButton.hidden = false;
  elements.activitySummary.textContent = `${runState.activityEvents.length} event${
    runState.activityEvents.length === 1 ? "" : "s"
  }`;

  const item = document.createElement("li");
  item.className = isError ? "activity-event error" : "activity-event";
  const time = document.createElement("time");
  time.dateTime = activityEvent.time.toISOString();
  time.textContent = activityEvent.time.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const text = document.createElement("span");
  text.textContent = label;
  item.append(time, text);
  elements.activityList.append(item);
}

function setRunStatus(label, options = {}) {
  const normalizedOptions = typeof options === "boolean" ? { forceVisible: options } : options;
  elements.runStatusLabel.textContent = label;
  elements.runStatusHint.textContent = normalizedOptions.hint || "Activity";
  elements.activityButton.classList.toggle("complete", Boolean(normalizedOptions.completed));
  elements.activityButton.classList.toggle("error", Boolean(normalizedOptions.error));
  elements.activityButton.hidden =
    !normalizedOptions.forceVisible && runState.activityEvents.length === 0;
}

function openActivitySheet() {
  elements.activityBackdrop.hidden = false;
  elements.activitySheet.hidden = false;
  requestAnimationFrame(() => {
    elements.activityBackdrop.classList.add("open");
    elements.activitySheet.classList.add("open");
  });
}

function closeActivitySheet() {
  elements.activityBackdrop.classList.remove("open");
  elements.activitySheet.classList.remove("open");
  window.setTimeout(() => {
    if (!elements.activitySheet.classList.contains("open")) {
      elements.activityBackdrop.hidden = true;
      elements.activitySheet.hidden = true;
    }
  }, 180);
}

async function cancelActiveRun() {
  if (!runState.currentThreadId || runState.cancelRequested) {
    return;
  }
  runState.cancelRequested = true;
  elements.stopButton.disabled = true;
  setRunStatus("Cancelling…", { forceVisible: true });
  addActivityEvent("Cancellation requested");
  try {
    await requestJson(`/threads/${encodeURIComponent(runState.currentThreadId)}/run/cancel`, {
      method: "POST",
    });
  } catch (error) {
    addActivityEvent(`Cancel request failed: ${error.message}`, null, true);
  } finally {
    runState.abortController?.abort();
  }
}

function appendInlineMessage(kind, content) {
  if (
    elements.messages.children.length === 1 &&
    elements.messages.firstElementChild.textContent === "Start a thread from this browser."
  ) {
    elements.messages.replaceChildren();
  }

  const item = document.createElement("article");
  item.className = `message ${kind}`;
  item.textContent = content;
  elements.messages.append(item);
  scrollMessagesToBottom();
  return item;
}

function formatRunEvent(event, peerTaskStatuses = new Map()) {
  if (event.type === "run.started") {
    return "Run started";
  }
  if (event.type === "llm.request") {
    return `LLM request ${event.iteration || ""}`.trim();
  }
  if (event.type === "tool.call") {
    return `Tool call: ${event.name || "unknown"}`;
  }
  if (event.type === "tool.result") {
    return `Tool result: ${event.name || "unknown"} ${event.is_error ? "error" : "ok"}`;
  }
  if (event.type === "peer.task.created") {
    rememberPeerTaskStatus(event, peerTaskStatuses);
    return `Peer task created: ${event.peer || "peer"} ${event.status || ""}`.trim();
  }
  if (event.type === "peer.task.poll") {
    if (event.status === "completed") {
      rememberPeerTaskStatus(event, peerTaskStatuses);
      return "";
    }
    if (!rememberPeerTaskStatus(event, peerTaskStatuses)) {
      return "";
    }
    return `Peer task: ${event.peer || "peer"} ${event.status || ""}`.trim();
  }
  if (event.type === "peer.task.completed") {
    rememberPeerTaskStatus(event, peerTaskStatuses);
    return `Peer task completed: ${event.peer || "peer"} ${event.status || ""}`.trim();
  }
  if (event.type === "peer.task.event") {
    return formatPeerTaskEvent(event.event || {});
  }
  if (event.type === "run.completed") {
    return "";
  }
  return "";
}

function rememberPeerTaskStatus(event, peerTaskStatuses) {
  const taskId = event.task_id || "";
  const status = event.status || "";
  if (!taskId || !status) {
    return true;
  }
  const previousStatus = peerTaskStatuses.get(taskId);
  peerTaskStatuses.set(taskId, status);
  return previousStatus !== status;
}

function formatPeerTaskEvent(peerEvent) {
  const peerEventType = peerEvent.type || peerEvent.event || "event";
  if (peerEventType === "message_update") {
    return "";
  }
  if (["message_start", "message_end", "turn_start", "turn_end", "session"].includes(peerEventType)) {
    return "";
  }
  if (peerEventType === "agent_start") {
    return "Peer agent started";
  }
  if (peerEventType === "agent_end") {
    return "Peer agent finished";
  }
  if (peerEventType === "tool_execution_start" || peerEventType === "tool_execution_end") {
    const action = peerEventType.endsWith("start") ? "start" : "end";
    const toolName = peerEvent.tool_name || peerEvent.name || peerEvent.tool || peerEvent.tool_call?.name;
    return `Peer tool ${action}${toolName ? `: ${toolName}` : ""}`;
  }
  return `Peer event: ${peerEventType}`;
}

function syncViewportHeight() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  const inputFocused = document.activeElement === elements.messageInput;
  const mobileLike = window.matchMedia("(pointer: coarse), (max-width: 640px)").matches;

  // Some mobile keyboards expose predictive text / shortcut bars that still overlap
  // the visual viewport. Reserve extra layout height whenever the input is focused
  // so the composer sits above those bars without floating over the chat.
  const keyboardLift = inputFocused && mobileLike ? 40 : 0;

  document.documentElement.style.setProperty("--app-height", `${height}px`);
  document.documentElement.style.setProperty("--keyboard-lift", `${keyboardLift}px`);
  scrollMessagesToBottom();
}

function scrollMessagesToBottom() {
  requestAnimationFrame(() => {
    elements.messages.scrollTop = elements.messages.scrollHeight;
  });
}

function shouldSubmitOnEnter(event) {
  if (event.shiftKey || event.isComposing) {
    return false;
  }
  return !window.matchMedia("(pointer: coarse), (max-width: 640px)").matches;
}

function setBusy(isBusy) {
  elements.sendButton.disabled = isBusy;
  elements.sendButton.hidden = isBusy;
  elements.stopButton.hidden = !isBusy;
  elements.stopButton.disabled = !isBusy;
  elements.newThreadButton.disabled = isBusy;
  elements.contextButton.disabled = isBusy || !state.threadId;
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle("error", isError);
}
