from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def test_web_client_mobile_navigation_interactions(tmp_path: Path) -> None:
    """Smoke-test browser UI wiring with a tiny dependency-free DOM harness."""

    repo_root = Path(__file__).parents[1]
    runner = tmp_path / "web_client_interactions.mjs"
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    runner.write_text(
        textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import fs from "node:fs";
            import vm from "node:vm";

            class ClassList {{
              constructor(element) {{
                this.element = element;
                this.values = new Set((element.className || "").split(/\\s+/).filter(Boolean));
              }}
              add(...names) {{
                for (const name of names) this.values.add(name);
                this.sync();
              }}
              remove(...names) {{
                for (const name of names) this.values.delete(name);
                this.sync();
              }}
              contains(name) {{
                return this.values.has(name);
              }}
              toggle(name, force) {{
                const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
                if (enabled) this.values.add(name);
                else this.values.delete(name);
                this.sync();
                return enabled;
              }}
              sync() {{
                this.element.className = [...this.values].join(" ");
              }}
            }}

            class Element {{
              constructor(tagName = "div", id = "") {{
                this.tagName = tagName.toUpperCase();
                this.id = id;
                this.className = "";
                this.classList = new ClassList(this);
                this.children = [];
                this.dataset = {{}};
                this.eventListeners = new Map();
                this.hidden = false;
                this.disabled = false;
                this.value = "";
                this.placeholder = "";
                this.scrollHeight = 0;
                this.scrollTop = 0;
                this.style = {{ setProperty(name, value) {{ this[name] = value; }} }};
                this.type = "";
              }}
              addEventListener(type, listener) {{
                const listeners = this.eventListeners.get(type) || [];
                listeners.push(listener);
                this.eventListeners.set(type, listeners);
              }}
              dispatchEvent(event) {{
                event.target ??= this;
                event.preventDefault ??= () => {{ event.defaultPrevented = true; }};
                event.stopPropagation ??= () => {{ event.propagationStopped = true; }};
                const results = [];
                for (const listener of this.eventListeners.get(event.type) || []) results.push(listener(event));
                return Promise.all(results);
              }}
              click() {{
                this.dispatchEvent({{ type: "click" }});
              }}
              focus() {{
                document.activeElement = this;
                this.dispatchEvent({{ type: "focus" }});
              }}
              append(...nodes) {{
                this.children.push(...nodes);
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }}
              replaceChildren(...nodes) {{
                this.children = [...nodes];
                this.textContent = this.children.map((node) => node.textContent || "").join("");
              }}
              get firstElementChild() {{
                return this.children.find((node) => node instanceof Element) || null;
              }}
              requestSubmit() {{
                return this.dispatchEvent({{ type: "submit" }});
              }}
            }}

            class TextNode {{
              constructor(text) {{
                this.textContent = text;
              }}
            }}

            const ids = [
              "status", "threads-button", "context-button", "more-button", "more-backdrop",
              "more-sheet", "more-close", "more-context-button", "more-settings-button",
              "settings-button", "settings-close", "settings-backdrop", "new-thread-button",
              "settings-panel", "base-url", "api-token", "user-id", "tenant-id", "skill",
              "skill-options", "capability-profile", "capability-profile-options",
              "execution-options-status", "messages", "activity-button", "run-status-label",
              "run-status-hint", "activity-backdrop", "activity-sheet", "activity-close",
              "activity-summary", "activity-list", "context-backdrop", "context-sheet",
              "context-close", "context-summary-line", "context-total-tokens",
              "context-message-count", "context-summarized-count", "context-unsummarized-count",
              "context-summary", "context-rendered", "compact-context-button", "threads-backdrop",
              "threads-sheet", "threads-close", "threads-summary", "threads-list",
              "threads-new-button", "threads-refresh-button", "composer", "message-input",
              "send-button", "stop-button",
            ];
            const elements = new Map(ids.map((id) => [id, new Element("div", id)]));
            elements.get("context-button").hidden = true;
            elements.get("more-context-button").hidden = true;
            for (const id of ["more-backdrop", "more-sheet", "settings-backdrop", "activity-backdrop",
              "activity-sheet", "context-backdrop", "context-sheet", "threads-backdrop", "threads-sheet",
              "activity-button", "stop-button"]) {{
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
            let completeRunStream;
            const runStreamCanFinish = new Promise((resolve) => {{
              completeRunStream = resolve;
            }});
            function ndjsonResponse(events) {{
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
                return ndjsonResponse([{{ type: "assistant.message", content: "Assistant reply" }}]);
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
            async function flushAsyncWork() {{
              for (let index = 0; index < 8; index += 1) await Promise.resolve();
            }}
            async function waitUntil(predicate) {{
              for (let index = 0; index < 20; index += 1) {{
                await Promise.resolve();
                if (predicate()) return;
              }}
              assert.fail("Condition was not met before timeout");
            }}
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

            assert.deepEqual(fetchCalls.map((call) => `${{call.method}} ${{call.url}}`), [
              "GET http://ui.test/execution-options",
              "GET http://ui.test/threads/t1/messages",
              "GET http://ui.test/threads/t1/context/raw",
              "GET http://ui.test/threads?limit=50",
              "POST http://ui.test/threads/t1/messages",
              "POST http://ui.test/threads/t1/run/stream",
              "GET http://ui.test/threads?limit=50",
            ]);
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(["node", str(runner)], cwd=repo_root, check=True)


def test_web_client_markdown_renderer_stays_safe(tmp_path: Path) -> None:
    """Exercise assistant markdown rendering without a browser dependency."""

    repo_root = Path(__file__).parents[1]
    runner = tmp_path / "web_client_markdown.mjs"
    app_path = repo_root / "app" / "static" / "web" / "app.js"
    runner.write_text(
        textwrap.dedent(
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
            """
        ),
        encoding="utf-8",
    )

    subprocess.run(["node", str(runner)], cwd=repo_root, check=True)
