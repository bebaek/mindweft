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

const elements = {
  status: document.querySelector("#status"),
  settingsButton: document.querySelector("#settings-button"),
  newThreadButton: document.querySelector("#new-thread-button"),
  settingsPanel: document.querySelector("#settings-panel"),
  baseUrl: document.querySelector("#base-url"),
  apiToken: document.querySelector("#api-token"),
  userId: document.querySelector("#user-id"),
  tenantId: document.querySelector("#tenant-id"),
  skill: document.querySelector("#skill"),
  capabilityProfile: document.querySelector("#capability-profile"),
  messages: document.querySelector("#messages"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#message-input"),
  sendButton: document.querySelector("#send-button"),
};

syncViewportHeight();
hydrateForm();
renderMessages([]);
setStatus(state.threadId ? `Thread ${state.threadId}` : "Ready");
if (state.threadId) {
  refreshMessages();
}

elements.settingsButton.addEventListener("click", () => {
  elements.settingsPanel.classList.toggle("open");
});

elements.newThreadButton.addEventListener("click", () => {
  state.threadId = "";
  saveFormState();
  renderMessages([]);
  setStatus("Ready");
  elements.messageInput.focus();
});

for (const input of [
  elements.baseUrl,
  elements.apiToken,
  elements.userId,
  elements.tenantId,
  elements.skill,
  elements.capabilityProfile,
]) {
  input.addEventListener("change", saveFormState);
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
    setStatus(`Thread ${threadId}`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
    elements.messageInput.focus();
  }
});

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
      appendNotice("Previous thread was no longer available. Started a fresh session.");
      setStatus("Ready");
      return;
    }
    renderMessages([]);
    setStatus(error.message, true);
  }
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
  return state.threadId;
}

async function streamRun(threadId) {
  let assistantMessage = null;
  let runFailed = false;
  const peerTaskStatuses = new Map();
  const seenProgressLabels = new Set();
  await requestNdjson(`/threads/${encodeURIComponent(threadId)}/run/stream`, {
    method: "POST",
    onEvent(event) {
      if (event.type === "assistant.message") {
        assistantMessage = appendMessage({ role: "assistant", content: event.content || "" });
        return;
      }
      if (event.type === "run.error") {
        runFailed = true;
        appendNotice(`Run failed: ${event.status_code || "error"} ${event.detail || ""}`.trim());
        setStatus(event.detail || "Run failed", true);
        return;
      }
      const label = formatRunEvent(event, peerTaskStatuses);
      if (label && !seenProgressLabels.has(label)) {
        seenProgressLabels.add(label);
        appendProgress(label);
        setStatus(label);
      }
    },
  });
  if (runFailed) {
    throw new Error("Run failed");
  }
  if (!assistantMessage) {
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
  content.textContent = message.content || "";
  item.append(role, content);
  elements.messages.append(item);
  scrollMessagesToBottom();
}

function appendNotice(content) {
  return appendInlineMessage("notice", content);
}

function appendProgress(content) {
  return appendInlineMessage("progress", content);
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
  elements.newThreadButton.disabled = isBusy;
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle("error", isError);
}
