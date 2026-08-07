import { test } from "@playwright/test";
import { installDemoWorkspaceMocks } from "../fixtures/demo-api-mocks";

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
