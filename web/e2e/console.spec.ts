import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import type { AdminAuditRecord } from "../src/api/client";

async function installApiMocks(page: Page, onExecutionOptions?: (route: Route) => void) {
  await page.route("**/health/ready", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "ready",
        checks: {
          lifecycle: "ok",
          thread_store: "ok",
          attachment_store: "ok",
        },
      }),
    });
  });
  await page.route("**/config", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        image_input: {
          enabled: true,
          max_bytes: 5_000_000,
          max_images: 4,
          max_total_bytes: 10_000_000,
          max_pixels: 20_000_000,
          max_dimension: 8_000,
          allowed_mime_types: ["image/png", "image/jpeg"],
        },
      }),
    });
  });
  await page.route("**/execution-options", async (route) => {
    onExecutionOptions?.(route);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "tenant-1",
        skills: { default: "default", items: [{ name: "default" }] },
        capability_profiles: { default: "standard", items: [{ name: "standard" }] },
        llm_profiles: { default: "primary", items: [{ name: "primary" }] },
        agents: { items: [] },
      }),
    });
  });
}

async function installWorkspaceMocks(
  page: Page,
  options: { consent?: boolean; uncertainResume?: boolean } = {},
) {
  const messages = new Map<string, Array<Record<string, unknown>>>([
    [
      "thread-1",
      [
        {
          id: "message-1",
          thread_id: "thread-1",
          role: "user",
          content: "Review the deployment plan",
          created_at: "2026-01-01T00:00:00Z",
        },
        {
          id: "message-2",
          thread_id: "thread-1",
          role: "assistant",
          content: "## Deployment review\n\n| Item | Status |\n| --- | --- |\n| Tests | Passing |\n\n**Run** `make test` before deployment.",
          created_at: "2026-01-01T00:01:00Z",
        },
      ],
    ],
  ]);

  await page.route("**/threads**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/threads" && request.method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          threads: [
            {
              thread_id: "thread-1",
              title: "Review the deployment plan",
              status: "idle",
              message_count: messages.get("thread-1")?.length ?? 0,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: new Date().toISOString(),
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      });
      return;
    }
    if (path === "/threads" && request.method() === "POST") {
      messages.set("thread-new", []);
      await route.fulfill({ contentType: "application/json", body: '{"thread_id":"thread-new"}' });
      return;
    }
    const messageMatch = path.match(/^\/threads\/([^/]+)\/messages$/);
    if (messageMatch) {
      const threadId = messageMatch[1];
      if (request.method() === "POST") {
        const body = request.postDataJSON() as { content: string; parts?: unknown[] };
        const message = {
          id: `message-${String((messages.get(threadId)?.length ?? 0) + 1)}`,
          thread_id: threadId,
          role: "user",
          content: body.content,
          ...(body.parts ? { parts: body.parts } : {}),
          created_at: new Date().toISOString(),
        };
        messages.get(threadId)?.push(message);
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(message) });
      } else {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(messages.get(threadId) ?? []),
        });
      }
      return;
    }
    const attachmentMatch = path.match(/^\/threads\/([^/]+)\/attachments(?:\/binary|\/([^/]+))$/);
    if (attachmentMatch) {
      if (path.endsWith("/binary")) {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            attachment_id: "attachment-1",
            thread_id: attachmentMatch[1],
            mime_type: "image/png",
            size_bytes: 68,
            created_at: new Date().toISOString(),
          }),
        });
      } else if (request.method() === "GET") {
        await route.fulfill({
          contentType: "image/png",
          body: Buffer.from(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "base64",
          ),
        });
      } else {
        await route.fulfill({ status: 204, body: "" });
      }
      return;
    }
    const pendingConsentMatch = path.match(
      /^\/threads\/([^/]+)\/private-value-consents\/pending$/,
    );
    if (pendingConsentMatch) {
      await route.fulfill({ contentType: "application/json", body: "[]" });
      return;
    }
    const consentMatch = path.match(
      /^\/threads\/([^/]+)\/private-value-consents\/([^/]+)(\/resume)?$/,
    );
    if (consentMatch) {
      if (consentMatch[3]) {
        if (options.uncertainResume) {
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: '{"detail":"connection lost after action claim"}',
          });
        } else {
          messages.get(consentMatch[1])?.push({
            id: "assistant-consent",
            thread_id: consentMatch[1],
            role: "assistant",
            content: "Sent.",
            created_at: new Date().toISOString(),
          });
          await route.fulfill({ contentType: "application/json", body: '{"reply":"Sent."}' });
        }
      } else {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({ consent_id: consentMatch[2], status: "approved" }),
        });
      }
      return;
    }
    const actionsMatch = path.match(/^\/threads\/([^/]+)\/private-value-actions(?:\/([^/]+))?$/);
    if (actionsMatch) {
      if (request.method() === "DELETE") {
        await route.fulfill({ contentType: "application/json", body: '{"discarded":true}' });
      } else {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify([
            {
              consent_id: "consent-1",
              thread_id: actionsMatch[1],
              tool_name: "trusted.send",
              state: "executing",
              expires_at: Date.now() / 1000 + 600,
            },
          ]),
        });
      }
      return;
    }
    const runMatch = path.match(/^\/threads\/([^/]+)\/run\/stream$/);
    if (runMatch) {
      const threadId = runMatch[1];
      if (options.consent) {
        messages.get(threadId)?.push({
          id: "assistant-approval",
          thread_id: threadId,
          role: "assistant",
          content: "Approval is required.",
          created_at: new Date().toISOString(),
        });
        await route.fulfill({
          contentType: "application/x-ndjson",
          body: [
            JSON.stringify({ type: "run.started" }),
            JSON.stringify({
              type: "private_value.consent_required",
              name: "trusted.send",
              request: {
                consent_id: "consent-1",
                thread_id: threadId,
                tool_name: "trusted.send",
                argument_fingerprint: "fingerprint-1",
                status: "pending",
                one_shot: true,
                expires_at: Date.now() / 1000 + 600,
                disclosures: [{ path: "recipient.email", kind: "email", count: 1 }],
              },
            }),
            JSON.stringify({ type: "assistant.message", content: "Approval is required." }),
            JSON.stringify({ type: "run.completed" }),
            "",
          ].join("\n"),
        });
        return;
      }
      messages.get(threadId)?.push({
        id: "assistant-1",
        thread_id: threadId,
        role: "assistant",
        content: "## Deployment ready\n\nThe deployment plan is ready.\n\n- Review changes\n- Deploy safely",
        created_at: new Date().toISOString(),
      });
      await route.fulfill({
        contentType: "application/x-ndjson",
        body: [
          JSON.stringify({ type: "run.started" }),
          JSON.stringify({ type: "llm.request" }),
          JSON.stringify({ type: "assistant.message", content: "## Deployment ready\n\nThe deployment plan is ready.\n\n- Review changes\n- Deploy safely" }),
          JSON.stringify({ type: "run.completed" }),
          "",
        ].join("\n"),
      });
      return;
    }
    const contextMatch = path.match(/^\/threads\/([^/]+)\/context\/raw$/);
    if (contextMatch) {
      const threadId = contextMatch[1];
      const threadMessages = messages.get(threadId) ?? [];
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          thread_id: threadId,
          summary: "Release planning and production readiness.",
          summarized_message_count: 1,
          messages: threadMessages,
          rendered: "Thread summary:\nRelease planning and production readiness.",
          usage: {
            estimated: true,
            total_tokens: 1240,
            summary_tokens: 80,
            message_tokens: 1160,
            message_count: threadMessages.length,
            summarized_message_count: 1,
            unsummarized_message_count: Math.max(0, threadMessages.length - 1),
          },
        }),
      });
      return;
    }
    const compactMatch = path.match(/^\/threads\/([^/]+)\/compact$/);
    if (compactMatch) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          thread_id: compactMatch[1],
          summary: "Compacted release plan.",
          compacted_message_count: 2,
          message_count: 2,
          usage_before: { total_tokens: 1240 },
          usage: { total_tokens: 460 },
        }),
      });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
}

async function navigateToWorkspace(page: Page) {
  const menu = page.getByRole("button", { name: "Open navigation" });
  if ((page.viewportSize()?.width ?? Infinity) <= 900) {
    await expect(menu).toBeVisible();
    await menu.click();
    await expect(page.locator(".sidebar")).toHaveClass(/is-open/);
  }
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
}

async function openConversations(page: Page) {
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) {
    await conversations.click();
    await expect(page.locator(".thread-rail")).toHaveClass(/is-open/);
  }
}

async function installAdminMocks(page: Page) {
  let acmeStatus = "active";
  const now = new Date().toISOString();
  await page.route("**/admin/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/admin/tenants") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          tenants: [
            { id: "tenant-acme", slug: "acme", name: "Acme Corporation", status: acmeStatus, plan: "enterprise", region: "us-east", metadata: {}, created_at: now, updated_at: now },
            { id: "tenant-beta", slug: "beta-labs", name: "Beta Labs", status: "provisioning", plan: "starter", region: "eu-west", metadata: {}, created_at: now, updated_at: now },
          ],
          total: 2,
          limit: 200,
          offset: 0,
        }),
      });
      return;
    }
    const transition = path.match(/^\/admin\/tenants\/([^/]+)\/(activate|suspend|archive)$/);
    if (transition) {
      acmeStatus = transition[2] === "activate" ? "active" : transition[2] === "suspend" ? "suspended" : "archived";
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ id: transition[1], slug: "acme", name: "Acme Corporation", status: acmeStatus, plan: "enterprise", region: "us-east", metadata: {}, created_at: now, updated_at: now }),
      });
      return;
    }
    const tenantId = path.split("/")[3];
    if (path.endsWith("/users")) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ tenant_id: tenantId, users: [
          { id: "membership-1", tenant_id: tenantId, user_id: "owner-1", email: "owner@example.test", display_name: "Alex Morgan", role: "owner", status: "active", created_at: now, updated_at: now },
          { id: "membership-2", tenant_id: tenantId, user_id: "member-1", email: "member@example.test", display_name: "Jordan Lee", role: "member", status: "invited", created_at: now, updated_at: now },
        ], total: 2, limit: 200, offset: 0 }),
      });
      return;
    }
    if (path.endsWith("/domains")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: tenantId, domains: [
        { id: "domain-1", tenant_id: tenantId, domain: tenantId === "tenant-acme" ? "acme.example" : "beta.example", verified: true, created_at: now },
      ] }) });
      return;
    }
    if (path.endsWith("/attachments/statistics")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: tenantId, total_count: 24, total_bytes: 4_194_304, pending_count: 1, pending_bytes: 1024, referenced_count: 23, referenced_bytes: 4_193_280, max_count: 1000, max_bytes: 104_857_600 }) });
      return;
    }
    if (path.endsWith("/run-concurrency")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: tenantId, active_runs: 3, active_users: 2, tenant_capacity: 20, user_capacity: 5 }) });
      return;
    }
    if (path.endsWith("/threads")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: tenantId, threads: [], limit: 10, offset: 0, total: 0, next_offset: null }) });
      return;
    }
    if (path.endsWith("/audit-records")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: tenantId, audit_records: [], limit: 10, offset: 0, total: 0, next_offset: null }) });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' });
  });
}

async function navigateToAdmin(page: Page) {
  let admin = page.getByRole("button", { name: "Administration", exact: true });
  if (await admin.count() === 0) {
    await page.getByRole("button", { name: /Secure session/ }).click();
    const dialog = page.getByRole("dialog", { name: "Authentication" });
    await dialog.getByLabel("Development headers").check();
    await dialog.getByLabel("Tenant ID").fill("tenant-acme");
    await dialog.getByLabel("User ID").fill("admin-e2e");
    await dialog.getByLabel("Administrator").check();
    await dialog.getByRole("button", { name: "Use connection" }).click();
    admin = page.getByRole("button", { name: "Administration", exact: true });
  }
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) {
    await menu.click();
    await expect(page.locator(".sidebar")).toHaveClass(/is-open/);
  }
  await admin.click();
}

test("loads the production console and passes an accessibility scan", async ({ page }) => {
  await installApiMocks(page);
  await page.goto("./");

  await expect(page).toHaveTitle("Mindweft Console");
  await expect(page.getByRole("heading", { name: "Build, observe, and govern your agents." })).toBeVisible();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  await expect(page.getByText("3 checks passing")).toBeVisible();

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("uses readable typography tokens for chat and controls", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");
  await navigateToWorkspace(page);
  await openConversations(page);
  await page.getByRole("button", { name: "Review the deployment plan" }).click();
  await expect(page.getByRole("heading", { name: "Deployment review" })).toBeVisible();

  const typography = await page.locator("html").evaluate((element) => {
    const styles = getComputedStyle(element);
    return {
      xs: styles.getPropertyValue("--text-xs").trim(),
      control: styles.getPropertyValue("--text-control").trim(),
      body: styles.getPropertyValue("--text-body").trim(),
      chat: styles.getPropertyValue("--text-chat").trim(),
    };
  });
  expect(typography).toEqual({ xs: "12px", control: "14px", body: "15px", chat: "16px" });
  await expect(page.locator(".chat-message.assistant .message-content").first()).toHaveCSS("font-size", "16px");
  await expect(page.locator(".message-author").first()).toHaveCSS("font-size", "13px");
  await expect(page.locator(".markdown-content code")).toHaveCSS("font-size", "13px");
  await expect(page.locator(".markdown-content strong")).toHaveCSS("font-weight", "650");
  const threadTitle = page.locator(".thread-title").first();
  await expect(threadTitle).toHaveText("Review the deployment plan");
  await expect(threadTitle).toHaveCSS("color", "rgb(23, 33, 27)");
  const titleBox = await threadTitle.boundingBox();
  expect(titleBox?.width ?? 0).toBeGreaterThan(40);
  expect(titleBox?.height ?? 0).toBeGreaterThan(10);
  const fontFamilies = await page.evaluate(() => {
    const composer = document.querySelector<HTMLTextAreaElement>(".chat-composer textarea");
    return {
      body: getComputedStyle(document.body).fontFamily,
      composer: composer ? getComputedStyle(composer).fontFamily : "",
    };
  });
  expect(fontFamilies.composer).toBe(fontFamilies.body);

  const undersizedControls = await page.locator("button:visible, input:visible, textarea:visible, select:visible").evaluateAll((elements) => elements.flatMap((element) => {
    const size = Number.parseFloat(getComputedStyle(element).fontSize);
    return size < 13 ? [`${element.tagName.toLowerCase()}.${element.className}: ${String(size)}px`] : [];
  }));
  expect(undersizedControls).toEqual([]);

  if ((page.viewportSize()?.width ?? 0) <= 620) {
    await expect(page.getByLabel(/^Message /)).toHaveCSS("font-size", "16px");
  }
});

test("gives the chat message input the full composer row", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");
  await navigateToWorkspace(page);
  await openConversations(page);
  await page.getByRole("button", { name: "Review the deployment plan" }).click();

  const composerBox = await page.locator(".chat-composer").boundingBox();
  const selectorBox = await page.locator(".composer-runtime-selectors").boundingBox();
  const inputBox = await page.getByLabel(/^Message /).boundingBox();

  expect(composerBox).not.toBeNull();
  expect(selectorBox).not.toBeNull();
  expect(inputBox).not.toBeNull();
  expect(inputBox!.width).toBeGreaterThan(composerBox!.width * 0.7);
  expect(inputBox!.y).toBeGreaterThanOrEqual(selectorBox!.y + selectorBox!.height - 1);
});

test("keeps overview, authentication, and administration legible in dark mode", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  await page.route("**/admin/tenants/tenant-acme/execution-config", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "tenant-acme",
        version: 1,
        config: {
          llm: { provider: "mock", model: "test-model" },
          tools: { allowed_local_tools: ["echo"], mcp_servers: [{ name: "docs", url: "https://docs.example/mcp" }, { name: "search", url: "https://search.example/mcp" }] },
          agent_backend: { type: "native" },
          skills: { items: [] },
          capability_profiles: { default_profile: "safe", items: [{ name: "safe", mcp_server_names: ["docs"] }] },
          agents: { items: [] },
        },
      }),
    });
  });
  await page.route("**/admin/tenants/tenant-acme/mcp-server-catalog", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], managed: false, allow_custom_mcp_servers: true }) });
  });
  await page.addInitScript(() => window.localStorage.setItem("minigent-theme", "dark"));
  await page.goto("./");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await page.getByRole("button", { name: /Secure session/ }).click();
  const dialog = page.getByRole("dialog", { name: "Authentication" });
  await dialog.getByLabel("Development headers").check();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await dialog.getByLabel("Tenant ID").fill("tenant-e2e");
  await dialog.getByLabel("User ID").fill("admin-e2e");
  await dialog.getByLabel("Administrator").check();
  await dialog.getByRole("button", { name: "Use connection" }).click();

  await navigateToAdmin(page);
  await expect(page.getByRole("heading", { name: "Acme Corporation" })).toBeVisible();
  await page.waitForTimeout(250);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await page.getByRole("button", { name: "Edit configuration" }).click();
  const executionDialog = page.getByRole("dialog", { name: "Edit execution configuration" });
  await expect(executionDialog).toBeVisible();
  for (const tab of ["LLM", "Tools", "Runtime", "Skills", "Presets", "Advanced"]) {
    await executionDialog.getByRole("button", { name: tab, exact: true }).click();
    expect((await new AxeBuilder({ page }).include(".execution-config-dialog").analyze()).violations).toEqual([]);
  }
});

test("keeps workspace dialogs legible in dark mode", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page, { consent: true });
  await page.addInitScript(() => window.localStorage.setItem("minigent-theme", "dark"));
  await page.goto("./");
  await navigateToWorkspace(page);

  await openConversations(page);
  await page.getByRole("button", { name: "Review the deployment plan" }).click();
  await expect(page.getByRole("heading", { name: "Deployment review" })).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await page.getByRole("button", { name: "Context" }).click();
  const contextDialog = page.getByRole("dialog", { name: "Thread context" });
  await expect(contextDialog.getByText("1,240")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await contextDialog.getByRole("button", { name: "Close context" }).click();

  await page.getByLabel(/^Message /).fill("Send this to my contact");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("dialog", { name: "Allow this exact tool action?" })).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("applies development credentials without persisting them", async ({ page }) => {
  const executionHeaders: Record<string, string>[] = [];
  await installApiMocks(page, (route) => executionHeaders.push(route.request().headers()));
  await page.goto("./");

  await page.getByRole("button", { name: /Secure session/ }).click();
  const dialog = page.getByRole("dialog", { name: "Authentication" });
  await dialog.getByLabel("Development headers").check();
  await dialog.getByLabel("Tenant ID").fill("tenant-e2e");
  await dialog.getByLabel("User ID").fill("user-e2e");
  await dialog.getByLabel("Administrator").check();
  await dialog.getByRole("button", { name: "Use connection" }).click();

  await expect(page.getByRole("button", { name: /Development/ })).toBeVisible();
  await expect.poll(() => executionHeaders.at(-1)?.["x-minigent-tenant-id"]).toBe("tenant-e2e");
  expect(executionHeaders.at(-1)?.["x-minigent-user-id"]).toBe("user-e2e");
  expect(executionHeaders.at(-1)?.["x-minigent-admin"]).toBe("true");

  const storage = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));
  expect(storage).toEqual({ local: ["minigent-theme"], session: [] });
});

test("runs a streamed conversation without accessibility violations", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");
  await navigateToWorkspace(page);

  await openConversations(page);
  await page.getByRole("button", { name: "Review the deployment plan" }).click();
  await expect(page.getByText("Review the deployment plan", { exact: true }).last()).toBeVisible();
  await openConversations(page);
  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("Attach images").setInputFiles({
    name: "release-diagram.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
      "base64",
    ),
  });
  await expect(page.getByRole("img", { name: "release-diagram.png" })).toBeVisible();
  await page.getByLabel("Image detail for release-diagram.png").selectOption("high");
  await page.getByLabel("Message Mindweft").fill("Prepare the release");
  await page.getByRole("button", { name: "Send message" }).click();

  await expect(page.getByText("The deployment plan is ready.").last()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deployment ready" })).toBeVisible();
  await expect(page.getByText("Review changes")).toBeVisible();
  await expect(page.getByRole("img", { name: "User attachment" })).toBeVisible();
  await expect(page.locator(".activity-tray summary").getByText("Run completed")).toBeVisible();

  await page.getByRole("button", { name: "Context" }).click();
  const contextDialog = page.getByRole("dialog", { name: "Thread context" });
  await expect(contextDialog.getByText("1,240")).toBeVisible();
  await contextDialog.getByRole("button", { name: "Compact thread context" }).click();
  await contextDialog.getByRole("button", { name: "Confirm compaction" }).click();
  await expect(contextDialog.getByText("Compacted 2 messages.")).toBeVisible();

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("approves one-time private-value disclosure and resumes the action", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page, { consent: true });
  await page.goto("./");
  await navigateToWorkspace(page);
  await page.getByLabel("Message Mindweft").fill("Send this to my contact");
  await page.getByRole("button", { name: "Send message" }).click();

  const consent = page.getByRole("dialog", { name: "Allow this exact tool action?" });
  await expect(consent.getByText("trusted.send")).toBeVisible();
  await expect(consent.getByText("recipient.email")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await consent.getByRole("button", { name: "Approve once and continue" }).click();

  await expect(consent).not.toBeVisible();
  await expect(page.getByText("Sent.")).toBeVisible();
  await expect(
    page.locator(".activity-tray summary").getByText("Private-value disclosure approved; action completed"),
  ).toBeVisible();
});

test("denies private-value disclosure without resuming the action", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page, { consent: true });
  await page.goto("./");
  await navigateToWorkspace(page);
  await page.getByLabel("Message Mindweft").fill("Do not disclose this");
  await page.getByRole("button", { name: "Send message" }).click();
  const consent = page.getByRole("dialog", { name: "Allow this exact tool action?" });

  await consent.getByRole("button", { name: "Deny" }).click();

  await expect(consent).not.toBeVisible();
  await expect(
    page.locator(".activity-tray summary").getByText("Private-value disclosure denied"),
  ).toBeVisible();
});

test("requires reconciliation when an approved action outcome is unknown", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page, { consent: true, uncertainResume: true });
  await page.goto("./");
  await navigateToWorkspace(page);
  await page.getByLabel("Message Mindweft").fill("Send this to my contact");
  await page.getByRole("button", { name: "Send message" }).click();
  const consent = page.getByRole("dialog");
  await consent.getByRole("button", { name: "Approve once and continue" }).click();

  await expect(consent.getByRole("heading", { name: "Check the external system before continuing." })).toBeVisible();
  await expect(consent.getByText(/does not undo an external action/)).toBeVisible();
  await consent.getByRole("button", { name: "I checked — discard local record" }).click();
  await expect(consent).not.toBeVisible();
  await expect(
    page.locator(".activity-tray summary").getByText("Uncertain private action record discarded"),
  ).toBeVisible();
});

test("inspects tenant operations and confirms lifecycle changes", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  await page.goto("./");
  await navigateToAdmin(page);

  await expect(page.getByRole("heading", { name: "Tenant operations" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Acme Corporation" })).toBeVisible();
  await expect(page.getByText("owner@example.test")).toBeVisible();
  await expect(page.getByText("acme.example")).toBeVisible();
  await expect(page.getByText("4.0 MB").first()).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  await page.getByRole("button", { name: "Suspend" }).click();
  const confirmation = page.getByRole("alertdialog", { name: "Confirm tenant status change" });
  await expect(confirmation.getByText(/Change Acme Corporation to suspended/)).toBeVisible();
  await confirmation.getByRole("button", { name: "Confirm change" }).click();
  await expect(page.getByRole("button", { name: "Activate" })).toBeVisible();

  await page.getByPlaceholder("Search tenants…").fill("Beta");
  await page.locator(".tenant-directory-list > button").click();
  await expect(page.getByRole("heading", { name: "Beta Labs" })).toBeVisible();
});

test("creates a tenant and reports provisioning conflicts", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  const now = new Date().toISOString();
  const created = { id: "tenant-northwind", slug: "northwind", name: "Northwind", status: "provisioning", plan: "growth", region: "us-west", metadata: {}, created_at: now, updated_at: now };
  let includeCreated = false;
  let createRequest: Record<string, unknown> | null = null;
  await page.route("**/admin/tenants**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      createRequest = body;
      if (body.slug === "acme") {
        await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"Tenant id or slug already exists"}' });
      } else {
        includeCreated = true;
        await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
      }
      return;
    }
    if (includeCreated) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenants: [created], total: 1, limit: 200, offset: 0 }) });
      return;
    }
    await route.fallback();
  });
  await page.goto("./");
  await navigateToAdmin(page);

  await page.getByRole("button", { name: "New tenant" }).click();
  const dialog = page.getByRole("dialog", { name: "Create tenant" });
  await expect(dialog.getByText("Starter agent included")).toBeVisible();
  await dialog.getByLabel("Slug").fill("acme");
  await dialog.getByLabel("Name").fill("Duplicate Acme");
  await dialog.getByRole("button", { name: "Create tenant" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Tenant id or slug already exists");

  await dialog.getByLabel("Slug").fill("northwind");
  await dialog.getByLabel("Name").fill("Northwind");
  await dialog.getByLabel("Plan").fill("growth");
  await dialog.getByLabel("Region").fill("us-west");
  await dialog.getByRole("button", { name: "Create tenant" }).click();
  await expect(dialog).not.toBeVisible();
  expect(includeCreated).toBe(true);
  expect(createRequest).toMatchObject({
    slug: "northwind",
    name: "Northwind",
    provisioning_profile: "generic-v1",
  });
});

test("manages tenant users and domains with destructive confirmations", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  const now = new Date().toISOString();
  const memberships: Array<Record<string, unknown>> = [];
  const tenantDomains: Array<Record<string, unknown>> = [];
  await page.route("**/admin/tenants/tenant-acme/users**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const userId = path.split("/")[5];
    if (request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      const created = { ...body, id: "membership-new", tenant_id: "tenant-acme", created_at: now, updated_at: now };
      memberships.push(created);
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
    } else if (request.method() === "PATCH") {
      const user = memberships.find((item) => item.id === userId)!;
      Object.assign(user, request.postDataJSON());
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(user) });
    } else if (request.method() === "DELETE") {
      memberships.splice(memberships.findIndex((item) => item.id === userId), 1);
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ deleted: true }) });
    } else {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", users: memberships, total: memberships.length, limit: 200, offset: 0 }) });
    }
  });
  await page.route("**/admin/tenants/tenant-acme/domains**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const domainId = path.split("/")[5];
    if (request.method() === "POST" && path.endsWith("/verify")) {
      const domain = tenantDomains.find((item) => item.id === domainId)!;
      domain.verified = true;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(domain) });
    } else if (request.method() === "POST") {
      const body = request.postDataJSON() as { domain: string };
      const created = { id: "domain-new", tenant_id: "tenant-acme", domain: body.domain, verified: false, created_at: now };
      tenantDomains.push(created);
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
    } else if (request.method() === "DELETE") {
      tenantDomains.splice(tenantDomains.findIndex((item) => item.id === domainId), 1);
      await route.fulfill({ status: 204, body: "" });
    } else {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", domains: tenantDomains }) });
    }
  });
  await page.goto("./");
  await navigateToAdmin(page);

  await page.getByRole("button", { name: "Add user" }).click();
  const userDialog = page.getByRole("dialog", { name: "Add tenant user" });
  await userDialog.getByLabel("User ID").fill("sam-1");
  await userDialog.getByLabel("Display name").fill("Sam Rivera");
  await userDialog.getByLabel("Role").selectOption("admin");
  await userDialog.getByRole("button", { name: "Add user" }).click();
  await expect(page.getByText("Sam Rivera")).toBeVisible();

  await page.getByRole("button", { name: "Edit Sam Rivera" }).click();
  const editUserDialog = page.getByRole("dialog", { name: "Edit tenant user" });
  await editUserDialog.getByLabel("Role").selectOption("viewer");
  await editUserDialog.getByLabel("Status").selectOption("active");
  await editUserDialog.getByRole("button", { name: "Save user" }).click();
  await expect(page.getByText("sam-1 · active")).toBeVisible();

  await page.getByRole("button", { name: "Remove Sam Rivera" }).click();
  const userConfirmation = page.getByRole("dialog", { name: "Remove tenant user?" });
  await expect(userConfirmation.getByText(/immediately lose access/)).toBeVisible();
  await userConfirmation.getByRole("button", { name: "Remove" }).click();
  await expect(userConfirmation).not.toBeVisible();
  await expect(page.getByText("Sam Rivera", { exact: true })).not.toBeVisible();

  await page.getByPlaceholder("company.example").fill("northwind.example");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const domainRow = page.locator(".domain-list li").filter({ hasText: "northwind.example" });
  await domainRow.getByRole("button", { name: "Verify" }).click();
  await expect(domainRow.locator(".domain-status")).toHaveClass(/verified/);
  await domainRow.getByRole("button", { name: "Remove northwind.example" }).click();
  const domainConfirmation = page.getByRole("dialog", { name: "Remove tenant domain?" });
  await expect(domainConfirmation.getByText(/no longer route users/)).toBeVisible();
  await domainConfirmation.getByRole("button", { name: "Remove" }).click();
  await expect(domainConfirmation).not.toBeVisible();
  await expect(page.getByText("northwind.example", { exact: true })).not.toBeVisible();
});

test("validates, saves, and resets tenant entitlements", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  const now = new Date().toISOString();
  let saved: Record<string, unknown> | null = null;
  await page.route("**/admin/tenants/tenant-acme/entitlements**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/validate")) {
      const input = request.postDataJSON() as { features: Record<string, boolean>; limits: Record<string, unknown> };
      const invalid = "reserved" in input.features;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ valid: !invalid, features: { ok: !invalid, errors: invalid ? ["Feature 'reserved' is not available"] : [] }, limits: { ok: true, errors: [] } }) });
    } else if (request.method() === "PUT") {
      const input = request.postDataJSON() as Record<string, unknown>;
      saved = { tenant_id: "tenant-acme", ...input, version: 1, updated_at: now };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(saved) });
    } else if (request.method() === "DELETE") {
      saved = null;
      await route.fulfill({ status: 204, body: "" });
    } else if (saved) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(saved) });
    } else {
      await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"No entitlements"}' });
    }
  });
  await page.goto("./");
  await navigateToAdmin(page);

  await expect(page.getByText("No tenant-specific entitlements")).toBeVisible();
  await page.getByRole("button", { name: "Configure entitlements" }).click();
  const dialog = page.getByRole("dialog", { name: "Configure entitlements" });
  await dialog.getByRole("button", { name: "Add feature" }).click();
  const featureRow = dialog.locator(".entitlement-row.feature");
  await featureRow.getByLabel("Feature name").fill("reserved");
  await dialog.getByRole("button", { name: "Add limit" }).click();
  const limitRow = dialog.locator(".entitlement-row.limit");
  await limitRow.getByLabel("Limit name").fill("max_threads");
  await limitRow.getByLabel("Value").fill("100");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await dialog.getByRole("button", { name: "Validate and save" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Feature 'reserved' is not available");

  await featureRow.getByLabel("Feature name").fill("mcp");
  await dialog.getByRole("button", { name: "Validate and save" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText("Version 1")).toBeVisible();
  await expect(page.getByText("mcp", { exact: true })).toBeVisible();
  await expect(page.getByText("100", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Reset to defaults" }).click();
  const confirmation = page.getByRole("dialog", { name: "Reset entitlements?" });
  await expect(confirmation.getByText(/Runtime defaults will apply immediately/)).toBeVisible();
  await confirmation.getByRole("button", { name: "Reset to defaults" }).click();
  await expect(confirmation).not.toBeVisible();
  await expect(page.getByText("No tenant-specific entitlements")).toBeVisible();
});

test("validates and applies execution configuration without exposing stored secrets", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  let stored: Record<string, unknown> | null = {
    llm: { provider: "mock", model: "initial-model", api_key: "<redacted>", has_api_key: true },
    tools: { allowed_local_tools: ["echo"], mcp_servers: [] },
    agent_backend: { type: "native" },
    skills: { items: [] },
    capability_profiles: { items: [] },
    agents: { items: [] },
  };
  let version = 1;
  let lastApplied: Record<string, unknown> | null = null;
  await page.route("**/admin/tenants/tenant-acme/execution-config**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/validate")) {
      const body = request.postDataJSON() as { config: Record<string, unknown> };
      const llm = body.config.llm as Record<string, unknown>;
      const invalid = llm.model === "invalid-model";
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ valid: !invalid, config_shape: { ok: true, errors: [] }, llm: { ok: !invalid, provider: llm.provider, model: llm.model, base_url: null, errors: invalid ? ["Selected model is not available"] : [] }, tools: { ok: true, errors: [], local_tools: ["echo", "current_time"], unknown_local_tools: [], mcp_servers: [] } }) });
    } else if (request.method() === "PUT") {
      const body = request.postDataJSON() as { config: Record<string, unknown> };
      lastApplied = body.config;
      stored = body.config;
      version += 1;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", version, config: stored }) });
    } else if (request.method() === "DELETE") {
      stored = null;
      await route.fulfill({ status: 204, body: "" });
    } else if (stored) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", version, config: stored }) });
    } else {
      await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"No execution configuration"}' });
    }
  });
  await page.goto("./");
  await navigateToAdmin(page);

  await expect(page.getByText("Version 1").last()).toBeVisible();
  await page.getByRole("button", { name: "Edit configuration" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit execution configuration" });
  const apiKeyInput = dialog.getByRole("textbox", { name: "API key", exact: true });
  await expect(apiKeyInput).toHaveValue("");
  await expect(apiKeyInput).toHaveAttribute("placeholder", /Stored secret/);
  await dialog.getByLabel("Model").fill("invalid-model");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  const discard = dialog.getByRole("alertdialog", { name: "Discard execution configuration changes" });
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "Keep editing" }).click();

  await dialog.getByRole("button", { name: "Tools" }).click();
  await dialog.getByLabel("Allowed local tools").fill("echo, current_time");
  await dialog.getByRole("button", { name: "Skills" }).click();
  await dialog.getByLabel("Skills configuration").fill(JSON.stringify({ default_skill: "review", items: [{ name: "review", system_prompt: "Review carefully" }] }, null, 2));
  await dialog.getByRole("button", { name: "Presets" }).click();
  await dialog.getByText("Advanced preset JSON", { exact: true }).click();
  await dialog.getByRole("textbox", { name: /^Capability profiles/ }).fill(JSON.stringify({ default_profile: "safe", items: [{ name: "safe", allowed_local_tools: ["echo"] }] }, null, 2));
  await dialog.getByRole("textbox", { name: /^Agent presets/ }).fill(JSON.stringify({ items: [{ name: "reviewer", skill_name: "review", capability_profile: "safe" }] }, null, 2));
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await dialog.getByRole("button", { name: "Validate and apply" }).click();
  await expect(dialog.getByText("Validation needs attention")).toBeVisible();
  await expect(dialog.getByText("Selected model is not available")).toBeVisible();

  await dialog.getByRole("button", { name: "LLM" }).click();
  await dialog.getByLabel("Model").fill("production-model");
  await dialog.getByRole("button", { name: "Validate and apply" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText("Version 2").last()).toBeVisible();
  const applied = (lastApplied ?? {}) as Record<string, unknown>;
  expect((applied.llm as Record<string, unknown>).api_key).toBe("<redacted>");
  expect((applied.skills as { items: unknown[] }).items).toHaveLength(1);
  expect((applied.agents as { items: unknown[] }).items).toHaveLength(1);

  await page.getByRole("button", { name: "Reset configuration" }).click();
  const confirmation = page.getByRole("dialog", { name: "Reset execution configuration?" });
  await confirmation.getByRole("button", { name: "Reset configuration" }).click();
  await expect(confirmation).not.toBeVisible();
  await expect(page.getByText("No tenant-specific execution configuration")).toBeVisible();
});

test("inspects, deletes, prunes, and audits tenant threads", async ({ page }) => {
  await installApiMocks(page);
  await installAdminMocks(page);
  const now = new Date().toISOString();
  let threads = [
    { thread_id: "thread-old-review", tenant_id: "tenant-acme", status: "idle", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-02T00:00:00Z", skill_name: "review", skill_names: ["review"], capability_profile: "safe", message_count: 2 },
    { thread_id: "thread-old-research", tenant_id: "tenant-acme", status: "error", created_at: "2026-01-03T00:00:00Z", updated_at: "2026-01-04T00:00:00Z", skill_name: "research", skill_names: ["research"], capability_profile: "default", message_count: 1 },
  ];
  let audit: AdminAuditRecord[] = [{ audit_id: "audit-config", tenant_id: "tenant-acme", actor_user_id: "admin-user", action: "tenant_execution_config.put", affected_count: 1, thread_ids: [], resource_type: "tenant_execution_config", resource_id: "tenant-acme", old_values: null, new_values: { llm: { provider: "mock" } }, metadata: null, created_at: now }];
  await page.route("**/admin/tenants/tenant-acme/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith("/threads/prune")) {
      const candidates = threads.map((thread) => thread.thread_id);
      const dryRun = url.searchParams.get("dry_run") === "true";
      if (!dryRun) {
        threads = [];
        audit = [{ audit_id: "audit-prune", tenant_id: "tenant-acme", actor_user_id: "admin-user", action: "threads.prune", affected_count: candidates.length, thread_ids: candidates, resource_type: null, resource_id: null, old_values: null, new_values: null, metadata: null, created_at: new Date().toISOString() }, ...audit];
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", deleted_count: dryRun ? 0 : candidates.length, updated_before: url.searchParams.get("updated_before"), dry_run: dryRun, candidate_thread_ids: candidates }) });
      return;
    }
    if (path.endsWith("/audit-records")) {
      const action = url.searchParams.get("action");
      const records = action ? audit.filter((record) => record.action === action) : audit;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", audit_records: records, limit: 10, offset: 0, total: records.length, next_offset: null }) });
      return;
    }
    if (path.endsWith("/threads")) {
      const skill = url.searchParams.get("skill");
      const filtered = skill ? threads.filter((thread) => thread.skill_names.includes(skill)) : threads;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "tenant-acme", threads: filtered, limit: 10, offset: 0, total: filtered.length, next_offset: null }) });
      return;
    }
    const match = path.match(/\/threads\/([^/]+)$/);
    if (match) {
      const thread = threads.find((item) => item.thread_id === match[1]);
      if (!thread) {
        await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Thread not found"}' });
        return;
      }
      if (request.method() === "DELETE") {
        threads = threads.filter((item) => item.thread_id !== match[1]);
        audit = [{ audit_id: "audit-delete", tenant_id: "tenant-acme", actor_user_id: "admin-user", action: "threads.delete", affected_count: 1, thread_ids: [match[1]], resource_type: null, resource_id: null, old_values: null, new_values: null, metadata: null, created_at: new Date().toISOString() }, ...audit];
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({ deleted: true, tenant_id: "tenant-acme", thread_id: match[1] }) });
      } else {
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...thread, context: { summary: "Reviewed deployment readiness and remaining risks.", summarized_message_count: 3, updated_at: thread.updated_at }, messages: [{ id: "message-user", thread_id: thread.thread_id, role: "user", content: "Review the deployment plan", parts: null, metadata: null, tool_name: null, tool_call_id: null, tool_arguments: null, created_at: thread.created_at }, { id: "message-tool", thread_id: thread.thread_id, role: "tool", content: "Checks passed", parts: null, metadata: { duration_ms: 42 }, tool_name: "deployment.check", tool_call_id: "call-1", tool_arguments: { environment: "staging" }, created_at: thread.updated_at }] }) });
      }
      return;
    }
    await route.fallback();
  });

  await page.goto("./");
  await navigateToAdmin(page);
  await expect(page.getByText("2 threads", { exact: true })).toBeVisible();
  await page.getByLabel("Skill", { exact: true }).fill("review");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("1 thread", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /thread-old-review/ }).click();
  const detail = page.getByRole("dialog", { name: /thread-o/ });
  await expect(detail.getByText("Reviewed deployment readiness and remaining risks.")).toBeVisible();
  await expect(detail.getByText("Review the deployment plan")).toBeVisible();
  await detail.getByText("Tool arguments").click();
  await expect(detail.getByText(/staging/)).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await detail.getByRole("button", { name: "Delete thread" }).click();
  const deletion = detail.getByRole("alertdialog", { name: "Delete thread confirmation" });
  await deletion.getByRole("button", { name: "Delete thread" }).click();
  await expect(detail).not.toBeVisible();
  await expect(page.getByText("Thread deleted and recorded in the audit log.")).toBeVisible();

  await page.getByRole("button", { name: "Clear" }).click();
  await page.getByRole("button", { name: "Prune threads" }).click();
  const prune = page.getByRole("dialog", { name: "Prune stale threads" });
  await prune.getByRole("button", { name: "Preview candidates" }).click();
  await expect(prune.getByText("1 candidate", { exact: true })).toBeVisible();
  await prune.getByRole("button", { name: "Delete 1 thread" }).click();
  await expect(prune).not.toBeVisible();
  await expect(page.getByText("1 thread deleted and recorded in the audit log.")).toBeVisible();

  await page.getByRole("button", { name: "Audit log" }).click();
  await expect(page.getByText("3 audit records", { exact: true })).toBeVisible();
  const pruneAudit = page.getByRole("button", { name: /Threads · Prune/ });
  await pruneAudit.click();
  await expect(page.getByText("thread-old-research")).toBeVisible();
  await page.getByLabel("Action").fill("threads.delete");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("1 audit record", { exact: true })).toBeVisible();
});

test("supports mobile navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");

  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  await expect(page.locator(".sidebar")).not.toHaveClass(/is-open/);
  await expect(page.locator(".thread-rail")).not.toHaveClass(/is-open/);

  await page.getByRole("button", { name: "Show conversations" }).click();
  await expect(page.locator(".thread-rail")).toHaveClass(/is-open/);
  await page.getByRole("button", { name: "Close conversations" }).click();
  await expect(page.locator(".thread-rail")).not.toHaveClass(/is-open/);
});
