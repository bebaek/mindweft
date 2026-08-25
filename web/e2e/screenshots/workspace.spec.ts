import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { installDemoWorkspaceMocks } from "../fixtures/demo-api-mocks";

test("keeps the message composer ready for the next turn", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.route("**/threads/thread-1/run/stream", (route) => route.fulfill({
    contentType: "application/x-ndjson",
    body: `${JSON.stringify({ type: "assistant.message", content: "Ready for the next question." })}\n`,
  }));
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();

  const composer = page.getByRole("textbox", { name: /Message/ });
  await expect(composer).toBeFocused();

  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  await expect(composer).toBeFocused();

  await composer.fill("What should I do next?");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(composer).toBeEnabled();
  await expect(composer).toBeFocused();
});

test("searches, pins, archives, and restores conversations", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  async function showConversations() {
    const toggle = page.getByRole("button", { name: "Show conversations" });
    if (await toggle.isVisible()) await toggle.click();
  }
  await showConversations();

  await page.getByRole("searchbox", { name: "Search conversations" }).fill("product launch");
  await expect(page.getByRole("button", { name: /Plan the product launch/ })).toBeVisible();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("button", { name: "Pin", exact: true }).click();
  await expect(page.getByRole("button", { name: "Unpin", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Archive", exact: true }).click();
  await showConversations();
  await expect(page.getByText("No matching conversations.")).toBeVisible();
  await page.getByRole("button", { name: "Archived", exact: true }).click();
  await expect(page.getByRole("button", { name: /Plan the product launch/ })).toBeVisible();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  await page.getByRole("button", { name: "Restore", exact: true }).click();
  await showConversations();
  await expect(page.getByText("No matching conversations.")).toBeVisible();
});

test("searches message content and navigates to the matching message", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();

  await page.getByRole("combobox", { name: "Conversation search scope" }).selectOption("all");
  await page.getByRole("searchbox", { name: "Search conversations" }).fill("focused launch sequence");
  const result = page.getByRole("button", { name: /Plan the product launch/ });
  await expect(result).toContainText("focused launch sequence");
  await result.click();

  await expect(page.locator("#message-message-2")).toHaveClass(/search-highlight/);
});

test("adds pasted clipboard images without blocking text paste", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.route("**/config", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      image_input: {
        enabled: true,
        allowed_mime_types: ["image/png", "image/jpeg"],
        max_images: 4,
        max_bytes: 5_000_000,
        max_total_bytes: 10_000_000,
      },
    }),
  }));
  await page.goto("/", { waitUntil: "networkidle" });

  const composer = page.getByRole("textbox", { name: /Message/ });
  await composer.fill("Keep this draft");
  const defaultAllowed = await composer.evaluate((element) => {
    const clipboard = new DataTransfer();
    clipboard.setData("text/plain", " and paste this text");
    clipboard.items.add(new File(
      [new Uint8Array([137, 80, 78, 71])],
      "clipboard-image.png",
      { type: "image/png" },
    ));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: clipboard });
    return element.dispatchEvent(event);
  });

  expect(defaultAllowed).toBe(true);
  await expect(composer).toHaveValue("Keep this draft");
  await expect(page.getByRole("img", { name: "clipboard-image.png" })).toBeVisible();
  await expect(page.getByLabel("Image detail for clipboard-image.png")).toHaveValue("auto");
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled();
});

test("keeps queued images visible when switching to a text-only profile", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.route("**/config", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      image_input: {
        enabled: true,
        allowed_mime_types: ["image/png"],
        max_images: 4,
        max_bytes: 5_000_000,
        max_total_bytes: 10_000_000,
      },
    }),
  }));
  await page.route("**/execution-options", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      tenant_id: "demo-tenant",
      skills: { items: [] },
      capability_profiles: { items: [] },
      llm_profiles: {
        default: "vision",
        effective_default: {
          name: "vision",
          image_input_allowed: true,
          capability_declared: true,
        },
        items: [
          {
            name: "vision",
            display_name: "Vision",
            input_modalities: ["text", "image"],
            image_input_allowed: true,
            capability_declared: true,
          },
          {
            name: "text-only",
            display_name: "Fast text",
            input_modalities: ["text"],
            image_input_allowed: false,
            image_input_reason: "profile_unsupported",
            capability_declared: true,
          },
        ],
      },
      agents: { items: [] },
    }),
  }));
  await page.goto("/", { waitUntil: "networkidle" });

  const composer = page.getByRole("textbox", { name: /Message/ });
  await composer.evaluate((element) => {
    const clipboard = new DataTransfer();
    clipboard.items.add(new File(
      [new Uint8Array([137, 80, 78, 71])],
      "queued.png",
      { type: "image/png" },
    ));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: clipboard });
    element.dispatchEvent(event);
  });
  await expect(page.getByRole("img", { name: "queued.png" })).toBeVisible();

  await page.getByLabel("Model profile").selectOption("text-only");
  await expect(page.getByText(/selected model profile only accepts text/i)).toBeVisible();
  await expect(page.getByRole("img", { name: "queued.png" })).toBeVisible();
  await composer.fill("Text remains editable");
  await expect(page.getByRole("button", { name: "Send message" })).toBeDisabled();

  await page.getByRole("button", { name: "Remove queued.png" }).click();
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled();
  await expect(page.getByText(/selected model profile only accepts text/i)).toBeVisible();
});

test("selects an explicit model profile for a new conversation", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: "New conversation" }).click();

  await page.getByRole("combobox", { name: "Agent" }).selectOption("user:researcher");
  await page.getByRole("combobox", { name: "Model profile" }).selectOption("claude");
  await expect(page.getByRole("combobox", { name: "Model profile" })).toHaveValue("claude");

  const createThread = page.waitForRequest((request) =>
    request.url().endsWith("/threads") && request.method() === "POST",
  );
  await page.getByRole("textbox", { name: /Message/ }).fill("Use the selected model profile.");
  await page.getByRole("button", { name: "Send message" }).click();

  const request = await createThread;
  expect(request.postDataJSON()).toEqual({
    agent_name: "user:researcher",
    llm_profile: "claude",
  });
});

test("persists the selected agent as the user default", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  let defaultAgent = "user:personal-assistant";
  let savedRequest: { config: Record<string, unknown>; expected_version?: number } | null = null;
  const personalConfig = {
    defaults: { agent_ref: defaultAgent, skill_refs: ["user:research"] },
    skills: { items: [{ id: "user:research", name: "Research" }] },
    agents: { items: [{ id: "user:researcher", name: "Researcher" }] },
  };
  await page.route("**/execution-options", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      tenant_id: "demo-tenant",
      skills: { items: [] },
      capability_profiles: { items: [] },
      llm_profiles: { default: "claude", items: [{ name: "claude", display_name: "Claude Sonnet" }] },
      agents: {
        default: defaultAgent,
        items: [
          { id: "user:personal-assistant", name: "user:personal-assistant", display_name: "Personal assistant" },
          { id: "user:researcher", name: "user:researcher", display_name: "Researcher" },
        ],
      },
    }),
  }));
  await page.route("**/me/execution-config", async (route) => {
    if (route.request().method() === "PUT") {
      savedRequest = route.request().postDataJSON() as typeof savedRequest;
      defaultAgent = String((savedRequest?.config.defaults as Record<string, unknown>).agent_ref);
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        tenant_id: "demo-tenant",
        user_id: "demo-user",
        config: savedRequest?.config ?? personalConfig,
        version: savedRequest ? 4 : 3,
        created_at: "2026-08-07T12:00:00Z",
        updated_at: "2026-08-07T12:00:00Z",
      }),
    });
  });

  async function openNewConversation() {
    await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
    const conversations = page.getByRole("button", { name: "Show conversations" });
    if (await conversations.isVisible()) await conversations.click();
    await page.getByRole("button", { name: "New conversation" }).click();
  }

  await page.goto("/", { waitUntil: "networkidle" });
  await openNewConversation();
  await page.getByRole("combobox", { name: "Agent" }).selectOption("user:researcher");
  await page.getByRole("button", { name: "Make default" }).click();
  await expect(page.getByRole("status")).toHaveText("Default saved for future conversations.");
  expect(savedRequest).toEqual({
    config: {
      ...personalConfig,
      defaults: { agent_ref: "user:researcher", skill_refs: ["user:research"] },
    },
    expected_version: 3,
  });

  await page.reload({ waitUntil: "networkidle" });
  await openNewConversation();
  await expect(page.getByRole("combobox", { name: "Agent" })).toHaveValue("user:researcher");
  await expect(page.getByRole("button", { name: "Make default" })).toHaveCount(0);
});

test("shows parent, sibling, and child lineage without exposing ids", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.route("**/threads/thread-1/lineage", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      thread: { thread_id: "thread-1", title: "Current branch", parent_thread_id: "thread-parent", status: "idle", message_count: 2, created_at: "2026-08-07T12:00:00Z", updated_at: "2026-08-07T12:01:00Z" },
      parent: { thread_id: "thread-parent", title: "Original launch plan", status: "idle", message_count: 4, created_at: "2026-08-06T12:00:00Z", updated_at: "2026-08-07T12:00:00Z" },
      siblings: [{ thread_id: "thread-sibling", title: "Partner launch", status: "idle", message_count: 3, created_at: "2026-08-07T12:00:00Z", updated_at: "2026-08-07T12:00:00Z" }],
      children: [{ thread_id: "thread-child", title: "Launch timeline", status: "idle", message_count: 3, created_at: "2026-08-08T12:00:00Z", updated_at: "2026-08-08T12:00:00Z" }],
    }),
  }));
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();

  const lineage = page.getByRole("navigation", { name: "Conversation branches" });
  await expect(lineage.getByText("Branched from")).toBeVisible();
  await expect(lineage.getByRole("button", { name: "Original launch plan" })).toBeVisible();
  await lineage.getByText("2 related branches").click();
  await expect(lineage.getByText("Sibling branches")).toBeVisible();
  await expect(lineage.getByRole("button", { name: "Partner launch" })).toBeVisible();
  await expect(lineage.getByText("Child branches")).toBeVisible();
  await expect(lineage.getByRole("button", { name: "Launch timeline" })).toBeVisible();
  await expect(lineage).not.toContainText("thread-parent");
  expect((await new AxeBuilder({ page }).include(".thread-lineage").analyze()).violations).toEqual([]);
});

test("captures the workspace with a demo chat", async ({ page }, testInfo) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  await page.waitForTimeout(300);

  const sidebarAccessibility = await new AxeBuilder({ page })
    .include(".sidebar-utilities")
    .withRules(["color-contrast"])
    .analyze();
  expect(sidebarAccessibility.violations).toEqual([]);

  await page.screenshot({
    path: `test-results/screenshots/workspace-${testInfo.project.name}.png`,
    fullPage: false,
  });
});

test("applies distinct curated branch hover colors in light and dark themes", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "What are we working on?" })).toBeVisible();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  const branchAction = page.getByRole("button", { name: "Branch from here" }).first();
  await branchAction.hover();
  await page.waitForTimeout(200);

  const lightStyles = await branchAction.evaluate((element) => {
    const styles = getComputedStyle(element);
    return { color: styles.color, backgroundColor: styles.backgroundColor };
  });
  const lightAccessibility = await new AxeBuilder({ page })
    .include(".message-actions")
    .withRules(["color-contrast"])
    .analyze();
  expect(lightAccessibility.violations).toEqual([]);
  await page.getByRole("button", { name: "Switch to dark mode" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await branchAction.hover();
  await page.waitForTimeout(200);
  const darkStyles = await branchAction.evaluate((element) => {
    const styles = getComputedStyle(element);
    return { color: styles.color, backgroundColor: styles.backgroundColor };
  });

  expect(lightStyles.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
  expect(darkStyles.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
  expect(darkStyles).not.toEqual(lightStyles);
  const darkAccessibility = await new AxeBuilder({ page })
    .include(".message-actions")
    .withRules(["color-contrast"])
    .analyze();
  expect(darkAccessibility.violations).toEqual([]);
});
