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

async function installWorkspaceMocks(page: Page) {
  const messages = new Map<string, Array<Record<string, string>>>([
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
        const content = (request.postDataJSON() as { content: string }).content;
        const message = {
          id: `message-${String((messages.get(threadId)?.length ?? 0) + 1)}`,
          thread_id: threadId,
          role: "user",
          content,
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
    const runMatch = path.match(/^\/threads\/([^/]+)\/run\/stream$/);
    if (runMatch) {
      const threadId = runMatch[1];
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
  await page.getByLabel("Message Minigent").fill("Prepare the release");
  await page.getByRole("button", { name: "Send message" }).click();

  await expect(page.getByText("The deployment plan is ready.")).toBeVisible();
  await expect(page.locator(".activity-tray summary").getByText("Run completed")).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
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
