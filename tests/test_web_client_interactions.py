from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

WEB_CLIENT_DOM_CLASSES = r"""
            class ClassList {
              constructor(element) {
                this.element = element;
                this.values = new Set((element.className || "").split(/\\s+/).filter(Boolean));
              }
              add(...names) {
                for (const name of names) this.values.add(name);
                this.sync();
              }
              remove(...names) {
                for (const name of names) this.values.delete(name);
                this.sync();
              }
              contains(name) {
                return this.values.has(name);
              }
              toggle(name, force) {
                const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
                if (enabled) this.values.add(name);
                else this.values.delete(name);
                this.sync();
                return enabled;
              }
              sync() {
                this.element.className = [...this.values].join(" ");
              }
            }

            class Element {
              constructor(tagName = "div", id = "") {
                this.tagName = tagName.toUpperCase();
                this.id = id;
                this.className = "";
                this.classList = new ClassList(this);
                this.children = [];
                this.dataset = {};
                this.eventListeners = new Map();
                this.hidden = false;
                this.disabled = false;
                this.value = "";
                this.placeholder = "";
                this.scrollHeight = 0;
                this.scrollTop = 0;
                this.style = { setProperty(name, value) { this[name] = value; } };
                this.type = "";
              }
              addEventListener(type, listener) {
                const listeners = this.eventListeners.get(type) || [];
                listeners.push(listener);
                this.eventListeners.set(type, listeners);
              }
              dispatchEvent(event) {
                event.target ??= this;
                event.preventDefault ??= () => { event.defaultPrevented = true; };
                event.stopPropagation ??= () => { event.propagationStopped = true; };
                const results = [];
                for (const listener of this.eventListeners.get(event.type) || []) results.push(listener(event));
                return Promise.all(results);
              }
              click() {
                this.dispatchEvent({ type: "click" });
              }
              focus() {
                document.activeElement = this;
                this.dispatchEvent({ type: "focus" });
              }
              append(...nodes) {
                this.children.push(...nodes);
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }
              replaceChildren(...nodes) {
                this.children = [...nodes];
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }
              get firstElementChild() {
                return this.children.find((node) => node instanceof Element) || null;
              }
              requestSubmit() {
                return this.dispatchEvent({ type: "submit" });
              }
            }

            class TextNode {
              constructor(text) {
                this.textContent = text;
              }
            }
"""


WEB_CLIENT_ELEMENT_IDS_JS = r"""[
              "status", "threads-button", "context-button", "more-button", "more-backdrop",
              "more-sheet", "more-close", "more-context-button", "more-settings-button",
              "settings-button", "settings-close", "settings-backdrop", "new-thread-button",
              "settings-panel", "base-url", "api-token", "user-id", "tenant-id", "skill",
              "skill-options", "capability-profile", "capability-profile-options", "llm-profile",
              "llm-profile-options",
              "execution-options-status", "messages", "activity-button", "run-status-label",
              "run-status-hint", "activity-backdrop", "activity-sheet", "activity-close",
              "activity-summary", "activity-list", "context-backdrop", "context-sheet",
              "context-close", "context-summary-line", "context-total-tokens",
              "context-message-count", "context-summarized-count", "context-unsummarized-count",
              "context-summary", "context-rendered", "compact-context-button", "threads-backdrop",
              "threads-sheet", "threads-close", "threads-summary", "threads-list",
              "threads-new-button", "threads-refresh-button", "composer", "message-input",
              "send-button", "stop-button",
            ]"""

WEB_CLIENT_HIDDEN_ELEMENT_IDS_JS = r"""["more-backdrop", "more-sheet", "settings-backdrop", "activity-backdrop",
              "activity-sheet", "context-backdrop", "context-sheet", "threads-backdrop", "threads-sheet",
              "activity-button", "stop-button"]"""

WEB_CLIENT_ASYNC_HELPERS = r"""
            async function flushAsyncWork() {
              for (let index = 0; index < 8; index += 1) await Promise.resolve();
            }
            async function waitUntil(predicate) {
              for (let index = 0; index < 20; index += 1) {
                await Promise.resolve();
                if (predicate()) return;
              }
              assert.fail("Condition was not met before timeout");
            }
"""


WEB_CLIENT_BROWSER_HARNESS = (
    r"""
            function createWebClientHarness({ storageValue, isMobile = false, fetchImpl, abortController = AbortController }) {
              const ids = """
    + WEB_CLIENT_ELEMENT_IDS_JS
    + r""";
              const elements = new Map(ids.map((id) => [id, new Element("div", id)]));
              for (const id of """
    + WEB_CLIENT_HIDDEN_ELEMENT_IDS_JS
    + r""") {
                elements.get(id).hidden = true;
              }
              const localStorageStore = new Map([
                ["minigent.webClient.v1", storageValue],
              ]);
              const document = {
                activeElement: null,
                documentElement: new Element("html", "documentElement"),
                querySelector(selector) {
                  if (!selector.startsWith("#")) return null;
                  return elements.get(selector.slice(1)) || null;
                },
                createElement(tagName) { return new Element(tagName); },
                createTextNode(text) { return new TextNode(text); },
              };
              const window = {
                location: { origin: "http://ui.test" },
                localStorage: {
                  getItem(key) { return localStorageStore.get(key) || null; },
                  setItem(key, value) { localStorageStore.set(key, value); },
                },
                visualViewport: { height: 700, addEventListener() {} },
                innerHeight: 700,
                matchMedia(query) { return { matches: isMobile && (query.includes("max-width") || query.includes("pointer")) }; },
                addEventListener() {},
                setTimeout(callback) { callback(); },
                confirm() { return true; },
              };
              const fetchCalls = [];
              async function fetch(url, options = {}) {
                fetchCalls.push({ url, method: options.method || "GET", body: options.body || "" });
                const path = new URL(url).pathname + new URL(url).search;
                return fetchImpl(url, options, path);
              }
              const context = {
                AbortController: abortController, Date, Error, JSON, Map, Number, Promise, Set, TextDecoder, TextEncoder,
                Uint8Array, URL, console, document, fetch, requestAnimationFrame: (callback) => callback(), window,
              };
              context.globalThis = context;
              vm.createContext(context);
              return { elements, localStorageStore, document, window, fetchCalls, context };
            }
"""
)


def _run_node_script(tmp_path: Path, repo_root: Path, name: str, source: str) -> None:
    runner = tmp_path / name
    runner.write_text(textwrap.dedent(source), encoding="utf-8")
    subprocess.run(["node", str(runner)], cwd=repo_root, check=True)


def test_web_client_mobile_navigation_interactions(tmp_path: Path) -> None:
    """Smoke-test browser UI wiring with a tiny dependency-free DOM harness."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_interactions.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            {WEB_CLIENT_DOM_CLASSES}

            const ids = {WEB_CLIENT_ELEMENT_IDS_JS};
            const elements = new Map(ids.map((id) => [id, new Element("div", id)]));
            elements.get("context-button").hidden = true;
            elements.get("more-context-button").hidden = true;
            for (const id of {WEB_CLIENT_HIDDEN_ELEMENT_IDS_JS}) {{
              elements.get(id).hidden = true;
            }}

            const localStorageStore = new Map([
              ["minigent.webClient.v1", JSON.stringify({{ baseUrl: "http://ui.test", threadId: "t1" }})],
            ]);
            const document = {{
              activeElement: null,
              documentElement: new Element("html", "documentElement"),
              querySelector(selector) {{
                if (!selector.startsWith("#")) return null;
                return elements.get(selector.slice(1)) || null;
              }},
              createElement(tagName) {{ return new Element(tagName); }},
              createTextNode(text) {{ return new TextNode(text); }},
            }};
            const window = {{
              location: {{ origin: "http://ui.test" }},
              localStorage: {{
                getItem(key) {{ return localStorageStore.get(key) || null; }},
                setItem(key, value) {{ localStorageStore.set(key, value); }},
              }},
              visualViewport: {{ height: 700, addEventListener() {{}} }},
              innerHeight: 700,
              matchMedia(query) {{ return {{ matches: query.includes("max-width") || query.includes("pointer") }}; }},
              addEventListener() {{}},
              setTimeout(callback) {{ callback(); }},
              confirm() {{ return true; }},
            }};
            const fetchCalls = [];
            let completeRunStream = () => {{}};
            let nextRunEvents = [{{ type: "assistant.message", content: "Assistant reply" }}];
            function ndjsonResponse(events) {{
              const runStreamCanFinish = new Promise((resolve) => {{
                completeRunStream = resolve;
              }});
              const chunks = [new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\\n") + "\\n")];
              return {{
                ok: true,
                status: 200,
                body: {{
                  getReader() {{
                    return {{
                      async read() {{
                        await runStreamCanFinish;
                        const value = chunks.shift();
                        return value ? {{ value, done: false }} : {{ value: undefined, done: true }};
                      }},
                    }};
                  }},
                }},
                text: async () => "",
              }};
            }}
            async function fetch(url, options = {{}}) {{
              fetchCalls.push({{ url, method: options.method || "GET", body: options.body || "" }});
              const path = new URL(url).pathname + new URL(url).search;
              if (path === "/threads/t1/run/stream") {{
                return ndjsonResponse(nextRunEvents);
              }}
              const payloads = {{
                "/execution-options": {{ skills: {{ default: "default", items: [] }}, capability_profiles: {{ default: "default", items: [] }} }},
                "/threads/t1/messages": [
                  {{ role: "user", content: "hello" }},
                  {{ role: "assistant", content: "hi" }},
                ],
                "/threads/t1/context/raw": {{ thread_id: "t1", usage: {{ total_tokens: 12, message_count: 2, summarized_message_count: 0, unsummarized_message_count: 2 }}, summary: "Summary text", rendered: "Rendered context" }},
                "/threads?limit=50": {{ total: 1, threads: [{{ thread_id: "t1", title: "Existing", message_count: 2 }}] }},
              }};
              return {{ ok: true, status: 200, json: async () => payloads[path] ?? {{}}, text: async () => "" }};
            }}
            const context = {{
              AbortController, Date, Error, JSON, Map, Number, Promise, Set, TextDecoder, TextEncoder,
              Uint8Array, URL, console, document, fetch, requestAnimationFrame: (callback) => callback(), window,
            }};
            context.globalThis = context;
            vm.createContext(context);
            {WEB_CLIENT_ASYNC_HELPERS}
            vm.runInContext(fs.readFileSync({str(app_path)!r}, "utf8"), context, {{ filename: "app.js" }});
            await flushAsyncWork();

            assert.equal(elements.get("context-button").hidden, false);
            assert.equal(elements.get("more-context-button").hidden, false);

            elements.get("more-button").click();
            assert.equal(elements.get("more-backdrop").hidden, false);
            assert.equal(elements.get("more-sheet").hidden, false);
            assert.equal(elements.get("more-sheet").classList.contains("open"), true);

            elements.get("more-settings-button").click();
            assert.equal(elements.get("more-sheet").hidden, true);
            assert.equal(elements.get("settings-panel").classList.contains("open"), true);
            assert.equal(elements.get("settings-backdrop").hidden, false);

            elements.get("settings-close").click();
            assert.equal(elements.get("settings-panel").classList.contains("open"), false);
            assert.equal(elements.get("settings-backdrop").hidden, true);

            elements.get("more-button").click();
            elements.get("more-context-button").click();
            await flushAsyncWork();
            assert.equal(elements.get("context-sheet").hidden, false);
            assert.equal(elements.get("context-sheet").classList.contains("open"), true);
            assert.equal(elements.get("context-total-tokens").textContent, "12");
            assert.equal(elements.get("context-rendered").textContent, "Rendered context");

            elements.get("threads-button").click();
            await flushAsyncWork();
            assert.equal(elements.get("threads-sheet").hidden, false);
            assert.equal(elements.get("threads-list").children.length, 1);
            assert.equal(elements.get("threads-summary").textContent, "1 conversation");

            elements.get("message-input").value = "New prompt";
            const submitPromise = elements.get("composer").requestSubmit();
            await waitUntil(() => fetchCalls.some((call) => call.url === "http://ui.test/threads/t1/run/stream"));
            assert.equal(elements.get("send-button").hidden, true);
            assert.equal(elements.get("send-button").disabled, true);
            assert.equal(elements.get("stop-button").hidden, false);
            assert.equal(elements.get("stop-button").disabled, false);
            completeRunStream();
            await submitPromise;
            await flushAsyncWork();
            assert.equal(elements.get("send-button").hidden, false);
            assert.equal(elements.get("send-button").disabled, false);
            assert.equal(elements.get("stop-button").hidden, true);
            assert.equal(elements.get("stop-button").disabled, true);
            assert.equal(elements.get("message-input").value, "");
            assert.equal(elements.get("messages").textContent.includes("New prompt"), true);
            assert.equal(elements.get("messages").textContent.includes("Assistant reply"), true);
            assert.equal(
              fetchCalls.find((call) => call.url === "http://ui.test/threads/t1/messages" && call.method === "POST").body,
              JSON.stringify({{ content: "New prompt" }})
            );

            nextRunEvents = [{{ type: "run.error", status_code: 503, detail: "Backend unavailable" }}];
            elements.get("message-input").value = "Failure prompt";
            const previousStreamCallCount = fetchCalls.filter(
              (call) => call.url === "http://ui.test/threads/t1/run/stream"
            ).length;
            const failedSubmitPromise = elements.get("composer").requestSubmit();
            await waitUntil(
              () => fetchCalls.filter((call) => call.url === "http://ui.test/threads/t1/run/stream").length > previousStreamCallCount
            );
            assert.equal(elements.get("send-button").hidden, true);
            assert.equal(elements.get("stop-button").hidden, false);
            completeRunStream();
            await failedSubmitPromise;
            await flushAsyncWork();
            assert.equal(elements.get("send-button").hidden, false);
            assert.equal(elements.get("send-button").disabled, false);
            assert.equal(elements.get("stop-button").hidden, true);
            assert.equal(elements.get("stop-button").disabled, true);
            assert.equal(elements.get("message-input").value, "");
            assert.equal(elements.get("messages").textContent.includes("Failure prompt"), true);
            assert.equal(elements.get("messages").textContent.includes("Run failed: 503 Backend unavailable"), true);
            assert.equal(elements.get("status").textContent, "503 Backend unavailable");
            assert.equal(elements.get("status").classList.contains("error"), true);
            const postedBodies = fetchCalls
              .filter((call) => call.url === "http://ui.test/threads/t1/messages" && call.method === "POST")
              .map((call) => call.body);
            assert.deepEqual(postedBodies, [
              JSON.stringify({{ content: "New prompt" }}),
              JSON.stringify({{ content: "Failure prompt" }}),
            ]);

            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "GET http://ui.test/threads/t1/messages",
              "GET http://ui.test/threads/t1/context/raw",
              "GET http://ui.test/threads?limit=50",
              "POST http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads/t1/run/stream",
              "GET http://ui.test/threads?limit=50",
              "POST http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads/t1/run/stream",
            ]);
            """,
    )


def test_web_client_thread_picker_selects_thread(tmp_path: Path) -> None:
    """Smoke-test selecting a different conversation from the thread picker."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_thread_picker.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            {WEB_CLIENT_DOM_CLASSES}
            {WEB_CLIENT_BROWSER_HARNESS}

            const payloads = {{
              "/execution-options": {{ skills: {{ default: "default", items: [] }}, capability_profiles: {{ default: "default", items: [] }} }},
              "/threads/t1/messages": [{{ role: "user", content: "t1 prompt" }}],
              "/threads/t2/messages": [{{ role: "assistant", content: "t2 answer" }}],
              "/threads?limit=50": {{ total: 2, threads: [
                {{ thread_id: "t1", title: "Current thread", message_count: 1 }},
                {{ thread_id: "t2", title: "Other thread", message_count: 1 }},
              ] }},
            }};
            const {{ elements, localStorageStore, fetchCalls, context, document }} = createWebClientHarness({{
              storageValue: JSON.stringify({{ baseUrl: "http://ui.test", threadId: "t1" }}),
              fetchImpl: async (_url, _options, path) => {{
                return {{ ok: true, status: 200, json: async () => payloads[path] ?? {{}}, text: async () => "" }};
              }},
            }});
            {WEB_CLIENT_ASYNC_HELPERS}
            vm.runInContext(fs.readFileSync({str(app_path)!r}, "utf8"), context, {{ filename: "app.js" }});
            await flushAsyncWork();
            assert.equal(elements.get("messages").textContent.includes("t1 prompt"), true);

            elements.get("threads-button").click();
            await flushAsyncWork();
            assert.equal(elements.get("threads-sheet").hidden, false);
            assert.equal(elements.get("threads-list").children.length, 2);
            assert.equal(elements.get("threads-summary").textContent, "2 conversations");
            assert.equal(elements.get("threads-list").children[0].children[0].className, "thread-item current");

            elements.get("threads-list").children[1].children[0].click();
            await flushAsyncWork();
            assert.equal(elements.get("threads-sheet").hidden, true);
            assert.equal(JSON.parse(localStorageStore.get("minigent.webClient.v1")).threadId, "t2");
            assert.equal(elements.get("status").textContent, "Thread t2");
            assert.equal(elements.get("messages").textContent.includes("t1 prompt"), false);
            assert.equal(elements.get("messages").textContent.includes("t2 answer"), true);
            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "GET http://ui.test/threads/t1/messages",
              "GET http://ui.test/threads?limit=50",
              "GET http://ui.test/threads/t2/messages",
            ]);
            """,
    )


def test_web_client_new_thread_send_flow(tmp_path: Path) -> None:
    """Smoke-test first-message send when the browser has no active thread."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_new_thread.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            {WEB_CLIENT_DOM_CLASSES}
            {WEB_CLIENT_BROWSER_HARNESS}

            let completeRunStream = () => {{}};
            function ndjsonResponse(events) {{
              const runStreamCanFinish = new Promise((resolve) => {{
                completeRunStream = resolve;
              }});
              const chunks = [new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\\n") + "\\n")];
              return {{
                ok: true,
                status: 200,
                body: {{
                  getReader() {{
                    return {{
                      async read() {{
                        await runStreamCanFinish;
                        const value = chunks.shift();
                        return value ? {{ value, done: false }} : {{ value: undefined, done: true }};
                      }},
                    }};
                  }},
                }},
                text: async () => "",
              }};
            }}
            const payloads = {{
              "/execution-options": {{ skills: {{ default: "default", items: [] }}, capability_profiles: {{ default: "default", items: [] }} }},
              "/threads/t-new/messages": [],
            }};
            const {{ elements, localStorageStore, fetchCalls, context, document }} = createWebClientHarness({{
              storageValue: JSON.stringify({{ baseUrl: "http://ui.test" }}),
              fetchImpl: async (_url, _options, path) => {{
                if (path === "/threads") return {{ ok: true, status: 200, json: async () => ({{ thread_id: "t-new" }}), text: async () => "" }};
                if (path === "/threads/t-new/run/stream") return ndjsonResponse([{{ type: "assistant.message", content: "New assistant reply" }}]);
                return {{ ok: true, status: 200, json: async () => payloads[path] ?? {{}}, text: async () => "" }};
              }},
            }});
            {WEB_CLIENT_ASYNC_HELPERS}
            vm.runInContext(fs.readFileSync({str(app_path)!r}, "utf8"), context, {{ filename: "app.js" }});
            await flushAsyncWork();
            assert.equal(elements.get("context-button").hidden, true);

            elements.get("message-input").value = "Start thread";
            const submitPromise = elements.get("composer").requestSubmit();
            await waitUntil(() => fetchCalls.some((call) => call.url === "http://ui.test/threads/t-new/run/stream"));
            assert.equal(elements.get("send-button").hidden, true);
            assert.equal(elements.get("stop-button").hidden, false);
            assert.equal(elements.get("context-button").hidden, false);
            assert.equal(JSON.parse(localStorageStore.get("minigent.webClient.v1")).threadId, "t-new");
            completeRunStream();
            await submitPromise;
            await flushAsyncWork();

            assert.equal(elements.get("send-button").hidden, false);
            assert.equal(elements.get("stop-button").hidden, true);
            assert.equal(elements.get("message-input").value, "");
            assert.equal(elements.get("messages").textContent.includes("Start thread"), true);
            assert.equal(elements.get("messages").textContent.includes("New assistant reply"), true);
            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "POST http://ui.test/threads",
              "POST http://ui.test/threads/t-new/messages",
              "POST http://ui.test/threads/t-new/run/stream",
            ]);
            assert.equal(fetchCalls.find((call) => call.url === "http://ui.test/threads/t-new/messages").body, JSON.stringify({{ content: "Start thread" }}));
            """,
    )


def test_web_client_new_thread_button_clears_state_then_sends(tmp_path: Path) -> None:
    """Smoke-test clearing the active thread before sending a fresh first prompt."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_new_thread_button.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            {WEB_CLIENT_DOM_CLASSES}
            {WEB_CLIENT_BROWSER_HARNESS}

            let completeRunStream = () => {{}};
            function ndjsonResponse(events) {{
              const runStreamCanFinish = new Promise((resolve) => {{
                completeRunStream = resolve;
              }});
              const chunks = [new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\\n") + "\\n")];
              return {{
                ok: true,
                status: 200,
                body: {{
                  getReader() {{
                    return {{
                      async read() {{
                        await runStreamCanFinish;
                        const value = chunks.shift();
                        return value ? {{ value, done: false }} : {{ value: undefined, done: true }};
                      }},
                    }};
                  }},
                }},
                text: async () => "",
              }};
            }}
            const payloads = {{
              "/execution-options": {{ skills: {{ default: "default", items: [] }}, capability_profiles: {{ default: "default", items: [] }} }},
              "/threads/t1/messages": [{{ role: "user", content: "old prompt" }}],
            }};
            const {{ elements, localStorageStore, fetchCalls, context, document }} = createWebClientHarness({{
              storageValue: JSON.stringify({{ baseUrl: "http://ui.test", threadId: "t1" }}),
              fetchImpl: async (_url, _options, path) => {{
                if (path === "/threads") return {{ ok: true, status: 200, json: async () => ({{ thread_id: "t-fresh" }}), text: async () => "" }};
                if (path === "/threads/t-fresh/run/stream") return ndjsonResponse([{{ type: "assistant.message", content: "Fresh reply" }}]);
                return {{ ok: true, status: 200, json: async () => payloads[path] ?? {{}}, text: async () => "" }};
              }},
            }});
            {WEB_CLIENT_ASYNC_HELPERS}
            vm.runInContext(fs.readFileSync({str(app_path)!r}, "utf8"), context, {{ filename: "app.js" }});
            await flushAsyncWork();
            assert.equal(elements.get("messages").textContent.includes("old prompt"), true);
            assert.equal(elements.get("context-button").hidden, false);

            elements.get("new-thread-button").click();
            assert.equal(JSON.parse(localStorageStore.get("minigent.webClient.v1")).threadId, "");
            assert.equal(elements.get("messages").children.length, 1);
            assert.equal(elements.get("messages").textContent, "Start a thread from this browser.");
            assert.equal(elements.get("status").textContent, "Ready");
            assert.equal(elements.get("context-button").hidden, true);
            assert.equal(elements.get("more-context-button").hidden, true);
            assert.equal(elements.get("context-summary-line").textContent, "No context loaded");
            assert.equal(elements.get("activity-button").hidden, true);
            assert.equal(document.activeElement, elements.get("message-input"));

            elements.get("message-input").value = "Fresh prompt";
            const submitPromise = elements.get("composer").requestSubmit();
            await waitUntil(() => fetchCalls.some((call) => call.url === "http://ui.test/threads/t-fresh/run/stream"));
            assert.equal(elements.get("send-button").hidden, true);
            assert.equal(elements.get("stop-button").hidden, false);
            assert.equal(elements.get("context-button").hidden, false);
            assert.equal(JSON.parse(localStorageStore.get("minigent.webClient.v1")).threadId, "t-fresh");
            completeRunStream();
            await submitPromise;
            await flushAsyncWork();

            assert.equal(elements.get("send-button").hidden, false);
            assert.equal(elements.get("stop-button").hidden, true);
            assert.equal(elements.get("message-input").value, "");
            assert.equal(elements.get("messages").textContent.includes("old prompt"), false);
            assert.equal(elements.get("messages").textContent.includes("Fresh prompt"), true);
            assert.equal(elements.get("messages").textContent.includes("Fresh reply"), true);
            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "GET http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads",
              "POST http://ui.test/threads/t-fresh/messages",
              "POST http://ui.test/threads/t-fresh/run/stream",
            ]);
            assert.equal(
              fetchCalls.find((call) => call.url === "http://ui.test/threads/t-fresh/messages").body,
              JSON.stringify({{ content: "Fresh prompt" }})
            );
            """,
    )


def test_web_client_stop_button_aborts_active_run(tmp_path: Path) -> None:
    """Smoke-test cancelling an active streaming run from the Stop button."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_stop_button.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            {WEB_CLIENT_DOM_CLASSES}
            {WEB_CLIENT_BROWSER_HARNESS}

            let abortCalls = 0;
            class AbortController {{
              constructor() {{
                const listeners = [];
                this.signal = {{
                  aborted: false,
                  addEventListener(type, listener) {{
                    if (type === "abort") listeners.push(listener);
                  }},
                }};
                this.listeners = listeners;
              }}
              abort() {{
                abortCalls += 1;
                this.signal.aborted = true;
                for (const listener of this.listeners) listener();
              }}
            }}
            function abortError() {{
              const error = new Error("The operation was aborted.");
              error.name = "AbortError";
              return error;
            }}
            let runStreamSignal = null;
            let finishCancelRequest = () => {{}};
            const payloads = {{
              "/execution-options": {{ skills: {{ default: "default", items: [] }}, capability_profiles: {{ default: "default", items: [] }} }},
              "/threads/t1/messages": [{{ role: "assistant", content: "Existing reply" }}],
            }};
            const {{ elements, fetchCalls, context, document }} = createWebClientHarness({{
              storageValue: JSON.stringify({{ baseUrl: "http://ui.test", threadId: "t1" }}),
              abortController: AbortController,
              fetchImpl: async (_url, options, path) => {{
                if (path === "/threads/t1/run/stream") {{
                  runStreamSignal = options.signal;
                  return {{
                    ok: true,
                    status: 200,
                    body: {{
                      getReader() {{
                        return {{
                          async read() {{
                            if (runStreamSignal.aborted) throw abortError();
                            await new Promise((resolve) => runStreamSignal.addEventListener("abort", resolve));
                            throw abortError();
                          }},
                        }};
                      }},
                    }},
                    text: async () => "",
                  }};
                }}
                if (path === "/threads/t1/run/cancel") {{
                  await new Promise((resolve) => {{
                    finishCancelRequest = resolve;
                  }});
                  return {{ ok: true, status: 200, json: async () => ({{}}), text: async () => "" }};
                }}
                return {{ ok: true, status: 200, json: async () => payloads[path] ?? {{}}, text: async () => "" }};
              }},
            }});
            {WEB_CLIENT_ASYNC_HELPERS}
            vm.runInContext(fs.readFileSync({str(app_path)!r}, "utf8"), context, {{ filename: "app.js" }});
            await flushAsyncWork();

            elements.get("message-input").value = "Stop me";
            const submitPromise = elements.get("composer").requestSubmit();
            await waitUntil(() => fetchCalls.some((call) => call.url === "http://ui.test/threads/t1/run/stream"));
            assert.equal(elements.get("send-button").hidden, true);
            assert.equal(elements.get("send-button").disabled, true);
            assert.equal(elements.get("stop-button").hidden, false);
            assert.equal(elements.get("stop-button").disabled, false);
            assert.equal(elements.get("activity-button").hidden, false);
            assert.equal(elements.get("run-status-label").textContent, "Thinking…");

            elements.get("stop-button").click();
            await waitUntil(() => fetchCalls.some((call) => call.url === "http://ui.test/threads/t1/run/cancel"));
            assert.equal(elements.get("stop-button").disabled, true);
            assert.equal(elements.get("run-status-label").textContent, "Cancelling…");
            assert.equal(elements.get("run-status-hint").textContent, "Activity");
            assert.equal(elements.get("activity-list").textContent.includes("Cancellation requested"), true);
            finishCancelRequest();
            await waitUntil(() => abortCalls === 1);
            await submitPromise;
            await flushAsyncWork();

            assert.equal(elements.get("send-button").hidden, false);
            assert.equal(elements.get("send-button").disabled, false);
            assert.equal(elements.get("stop-button").hidden, true);
            assert.equal(elements.get("stop-button").disabled, true);
            assert.equal(elements.get("status").textContent, "Thread t1");
            assert.equal(elements.get("run-status-label").textContent, "Activity");
            assert.equal(elements.get("run-status-hint").textContent, "Cancelled");
            assert.equal(elements.get("messages").textContent.includes("Stop me"), true);
            assert.equal(elements.get("messages").textContent.includes("Run cancelled."), true);
            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "GET http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads/t1/run/stream",
              "POST http://ui.test/threads/t1/run/cancel",
            ]);
            """,
    )


def test_web_client_markdown_renderer_stays_safe(tmp_path: Path) -> None:
    """Exercise assistant markdown rendering without a browser dependency."""

    repo_root = Path(__file__).parents[1]
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    _run_node_script(
        tmp_path,
        repo_root,
        "web_client_markdown.mjs",
        f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            class Element {{
              constructor(tagName) {{
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {{}};
                this.attributes = {{}};
                this.textContent = "";
              }}
              append(...nodes) {{
                this.children.push(...nodes);
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }}
              replaceChildren(...nodes) {{
                this.children = [...nodes];
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }}
              set href(value) {{ this.attributes.href = value; }}
              get href() {{ return this.attributes.href; }}
              set target(value) {{ this.attributes.target = value; }}
              get target() {{ return this.attributes.target; }}
              set rel(value) {{ this.attributes.rel = value; }}
              get rel() {{ return this.attributes.rel; }}
            }}
            class TextNode {{ constructor(text) {{ this.textContent = text; }} }}

            const context = {{
              document: {{
                createElement(tagName) {{ return new Element(tagName); }},
                createTextNode(text) {{ return new TextNode(text); }},
              }},
            }};
            vm.createContext(context);
            const source = fs.readFileSync({str(app_path)!r}, "utf8");
            const rendererSource = source.slice(
              source.indexOf("function renderMarkdownBlocks"),
              source.indexOf("function appendNotice")
            );
            vm.runInContext(rendererSource, context, {{ filename: "renderer.js" }});

            const nodes = context.renderMarkdownBlocks(
              "Hello **bold** `code` [site](https://example.test)\\n```js\\nconsole.log(1)\\n```"
            );
            assert.equal(nodes.some((node) => node.tagName === "STRONG" && node.textContent === "bold"), true);
            assert.equal(nodes.some((node) => node.tagName === "CODE" && node.textContent === "code"), true);
            const link = nodes.find((node) => node.tagName === "A");
            assert.equal(link.href, "https://example.test");
            assert.equal(link.target, "_blank");
            assert.equal(link.rel, "noopener noreferrer");
            const block = nodes.find((node) => node.tagName === "PRE");
            assert.equal(block.children[0].dataset.language, "js");
            assert.equal(block.children[0].textContent, "console.log(1)");
            assert.equal(nodes.some((node) => node.tagName === "SCRIPT"), false);
            """,
    )
