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
