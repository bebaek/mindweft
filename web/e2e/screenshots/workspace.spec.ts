import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { installDemoWorkspaceMocks } from "../fixtures/demo-api-mocks";

test("searches, pins, archives, and restores conversations", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
  async function showConversations() {
    const toggle = page.getByRole("button", { name: "Show conversations" });
    if (await toggle.isVisible()) await toggle.click();
  }
  await showConversations();

  await page.getByRole("searchbox", { name: "Search conversations" }).fill("product launch");
  await expect(page.getByRole("button", { name: /Plan the product launch/ })).toBeVisible();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
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

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();

  await page.getByRole("combobox", { name: "Conversation search scope" }).selectOption("all");
  await page.getByRole("searchbox", { name: "Search conversations" }).fill("focused launch sequence");
  const result = page.getByRole("button", { name: /Plan the product launch/ });
  await expect(result).toContainText("focused launch sequence");
  await result.click();

  await expect(page.locator("#message-message-2")).toHaveClass(/search-highlight/);
});

test("selects an explicit model profile for a new conversation", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
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
    const menu = page.getByRole("button", { name: "Open navigation" });
    if (await menu.isVisible()) await menu.click();
    await page.getByRole("button", { name: "Workspace" }).click();
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

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
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

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
  const conversations = page.getByRole("button", { name: "Show conversations" });
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole("button", { name: /Plan the product launch/ }).click();
  await page.waitForTimeout(300);

  await page.screenshot({
    path: `test-results/screenshots/workspace-${testInfo.project.name}.png`,
    fullPage: true,
  });
});

test("applies distinct curated branch hover colors in light and dark themes", async ({ page }) => {
  await installDemoWorkspaceMocks(page);
  await page.goto("/", { waitUntil: "networkidle" });

  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Workspace" }).click();
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
