import type { Page } from "@playwright/test";

export async function installDemoWorkspaceMocks(page: Page) {
  const principal = { user_id: "demo-user", tenant_id: "demo-tenant", is_admin: false };
  const thread = {
    thread_id: "thread-1",
    title: "Plan the product launch",
    status: "idle",
    message_count: 2,
    created_at: "2026-08-07T12:00:00Z",
    updated_at: new Date().toISOString(),
  };
  const messages = [
    {
      id: "message-1",
      thread_id: "thread-1",
      role: "user",
      content: "Help me plan the product launch.",
      created_at: "2026-08-07T12:00:00Z",
    },
    {
      id: "message-2",
      thread_id: "thread-1",
      role: "assistant",
      content:
        "## Product launch plan\n\nHere is a focused launch sequence:\n\n- Confirm the target audience\n- Prepare the announcement\n- Coordinate the release checklist\n\nI can help turn this into a detailed timeline.",
      created_at: "2026-08-07T12:01:00Z",
    },
  ];
  const fulfill = (body: unknown) => ({
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/auth/session", (route) =>
    route.fulfill(fulfill({ enabled: true, authenticated: true, principal })),
  );
  await page.route("**/health/ready", (route) =>
    route.fulfill(fulfill({ status: "ready", checks: { lifecycle: "ok" } })),
  );
  await page.route("**/tenant-context", (route) =>
    route.fulfill(
      fulfill({
        tenant_id: "demo-tenant",
        principal,
        features: {},
        limits: {},
        user_role: "owner",
        user_status: "active",
        membership_metadata: {},
      }),
    ),
  );
  await page.route("**/config", (route) =>
    route.fulfill(
      fulfill({
        image_input: {
          enabled: false,
          allowed_mime_types: [],
          max_images: 0,
          max_bytes: 0,
          max_total_bytes: 0,
        },
      }),
    ),
  );
  await page.route("**/execution-options", (route) =>
    route.fulfill(
      fulfill({
        tenant_id: "demo-tenant",
        skills: { items: [] },
        capability_profiles: { items: [] },
        llm_profiles: { items: [] },
        agents: {
          default: "user:personal-assistant",
          items: [
            {
              id: "user:personal-assistant",
              name: "user:personal-assistant",
              display_name: "Personal assistant",
            },
            { id: "user:researcher", name: "user:researcher", display_name: "Researcher" },
          ],
        },
      }),
    ),
  );
  await page.route("**/threads**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/threads" && route.request().method() === "GET") {
      await route.fulfill(fulfill({ threads: [thread], total: 1, limit: 50, offset: 0 }));
      return;
    }
    await route.continue();
  });
  await page.route("**/threads/thread-1/messages", (route) =>
    route.fulfill(fulfill(messages)),
  );
  await page.route("**/threads/thread-1/private-value-consents/pending", (route) =>
    route.fulfill(fulfill([])),
  );
}
