import { expect, test } from "@playwright/test";
import { installDemoWorkspaceMocks } from "../fixtures/demo-api-mocks";

test("tenant Netwise token replacement preview", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1200 });
  await installDemoWorkspaceMocks(page);
  const server = { name: "netwise", url: "https://netwise.example/mcp/", headers: { Authorization: "<redacted>" }, allowed_tools: ["summarize_financial_position"] };
  await page.route("**/admin/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = { users: [], domains: [], events: [], items: [], total: 0, features: {}, limits: {}, metadata: {} };
    if (path.endsWith("/demo-tenant")) body = { id: "demo-tenant", name: "Demo household", slug: "demo", status: "active", metadata: {}, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z" };
    if (path.endsWith("/execution-config")) body = { tenant_id: "demo-tenant", version: 1, config: { llm: { provider: "mock" }, tools: { allowed_local_tools: ["echo", "current_time", "calculator"], mcp_servers: [server] } } };
    if (path.endsWith("/mcp-server-catalog")) body = { managed: true, allow_custom_mcp_servers: false, items: [{ id: "netwise", title: "Netwise", description: "Summarize financial positions, projections, assumptions, and data freshness.", detail: "Internal service · 9 tools", tenant_credential: "bearer", server }] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  let lastCheck: { status: string; checked_at: string; tools: string[] } | null = null;
  let connectionTests = 0;
  await page.route("**/mcp-connections", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({ version: 1, items: [{ name: "netwise", enabled: true, credential_owner: "tenant", bearer_configured: true, last_check: lastCheck }] }) }));
  await page.route("**/mcp-servers/netwise/test", route => {
    connectionTests += 1;
    lastCheck = { status: "succeeded", checked_at: new Date().toISOString(), tools: ["netwise.summarize_financial_position"] };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(lastCheck) });
  });
  await page.addInitScript(() => localStorage.setItem("minigent-theme", "dark"));
  await page.goto("/console/", { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Tenant settings", exact: true }).click();
  await page.getByRole("button", { name: "Edit configuration", exact: true }).click();
  await page.getByRole("button", { name: "Tools", exact: true }).click();
  await expect(page.getByText("Saved connection: enabled", { exact: true })).toBeVisible();
  await expect(page.getByText("No check recorded for the current configuration", { exact: true })).toBeVisible();
  expect(connectionTests).toBe(0);
  await page.getByRole("button", { name: "Test Netwise connection" }).click();
  await expect(page.getByText(/Last check succeeded/)).toBeVisible();
  await page.getByText("Permitted tools discovered (1)", { exact: true }).click();
  await expect(page.getByText("netwise.summarize_financial_position", { exact: true })).toBeVisible();
  expect(connectionTests).toBe(1);
  await page.getByRole("button", { name: "Replace Netwise token" }).click();
  await expect(page.getByLabel("New Netwise token")).toBeVisible();
  await page.getByLabel("New Netwise token").fill("synthetic-test-token");
  await expect(page.getByRole("button", { name: "Validate and save token" })).toBeEnabled();
  await page.getByRole("button", { name: "Cancel token replacement" }).click();
  await page.getByRole("button", { name: "Replace Netwise token" }).click();
  await expect(page.getByLabel("New Netwise token")).toHaveValue("");
  await expect(page.getByText("This tenant is restricted to assigned catalog services.", { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("netwise-token-replacement.png"), fullPage: false });
  await page.route("**/mcp-servers/netwise/credential", route => route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "validation failed" }) }));
  await page.getByLabel("New Netwise token").fill("synthetic-test-token");
  await page.getByRole("button", { name: "Validate and save token" }).click();
  await expect(page.getByRole("alert")).toContainText("Token was not saved");
  await expect(page.getByLabel("New Netwise token")).toBeEnabled();
  await page.route("**/mcp-servers/netwise/credential", async route => {
    expect(route.request().postDataJSON()).toEqual({ token: "synthetic-test-token", expected_version: 1 });
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ tenant_id: "demo-tenant", version: 2, config: { llm: { provider: "mock" }, tools: { mcp_servers: [server] } } }) });
  });
  await page.getByRole("button", { name: "Validate and save token" }).click();
  await expect(page.getByRole("dialog", { name: "Edit execution configuration" })).toBeHidden();
  await expect(page.getByText("Version 2", { exact: true })).toBeVisible();
});
