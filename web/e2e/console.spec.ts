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
