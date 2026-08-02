import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

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
        content: "The deployment plan is ready.",
        created_at: new Date().toISOString(),
      });
      await route.fulfill({
        contentType: "application/x-ndjson",
        body: [
          JSON.stringify({ type: "run.started" }),
          JSON.stringify({ type: "llm.request" }),
          JSON.stringify({ type: "assistant.message", content: "The deployment plan is ready." }),
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
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
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
    await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' });
  });
}

async function navigateToAdmin(page: Page) {
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Administration", exact: true }).click();
}

test("loads the production console and passes an accessibility scan", async ({ page }) => {
  await installApiMocks(page);
  await page.goto("./");

  await expect(page).toHaveTitle("Minigent Console");
  await expect(page.getByRole("heading", { name: "Build, observe, and govern your agents." })).toBeVisible();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  await expect(page.getByText("3 checks passing")).toBeVisible();

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
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
  expect(storage).toEqual({ local: [], session: [] });
});

test("runs a streamed conversation without accessibility violations", async ({ page }) => {
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");
  await navigateToWorkspace(page);

  await page.getByRole("button", { name: "Review the deployment plan" }).click();
  await expect(page.getByText("Review the deployment plan", { exact: true }).last()).toBeVisible();
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
  await page.getByLabel("Message Minigent").fill("Prepare the release");
  await page.getByRole("button", { name: "Send message" }).click();

  await expect(page.getByText("The deployment plan is ready.").last()).toBeVisible();
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
  await page.getByLabel("Message Minigent").fill("Send this to my contact");
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
  await page.getByLabel("Message Minigent").fill("Do not disclose this");
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
  await page.getByLabel("Message Minigent").fill("Send this to my contact");
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
  await page.route("**/admin/tenants**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON() as { slug: string };
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

test("supports mobile navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installApiMocks(page);
  await installWorkspaceMocks(page);
  await page.goto("./");

  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  await expect(page.locator(".sidebar")).not.toHaveClass(/is-open/);
});
