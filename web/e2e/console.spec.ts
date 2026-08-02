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

test("supports mobile navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installApiMocks(page);
  await page.goto("./");

  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();

  await expect(page.getByRole("heading", { name: "A calmer place to run agents." })).toBeVisible();
  await expect(page.locator(".sidebar")).not.toHaveClass(/is-open/);
});
