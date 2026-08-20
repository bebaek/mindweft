import { expect, test } from "@playwright/test";
import { installDemoWorkspaceMocks } from "../fixtures/demo-api-mocks";

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
