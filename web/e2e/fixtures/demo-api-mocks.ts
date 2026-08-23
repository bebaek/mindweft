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
    pinned_at: null as string | null,
    archived_at: null as string | null,
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
        llm_profiles: {
          default: "claude",
          items: [
            { name: "claude", display_name: "Claude Sonnet" },
            { name: "gpt-4.1", display_name: "GPT-4.1" },
          ],
        },
        agents: {
          default: "user:personal-assistant",
          items: [
            {
              id: "user:personal-assistant",
              name: "user:personal-assistant",
              display_name: "Personal assistant",
              llm_profile: "claude",
            },
            { id: "user:researcher", name: "user:researcher", display_name: "Researcher" },
          ],
        },
      }),
    ),
  );
  await page.route("**/threads**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/search/threads" && route.request().method() === "GET") {
      const query = url.searchParams.get("q")?.toLowerCase() ?? "";
      const matchingMessages = messages.filter((message) =>
        ["user", "assistant"].includes(message.role)
        && message.content.toLowerCase().includes(query),
      );
      const matches = matchingMessages.length > 0
        && Boolean(thread.archived_at) === (url.searchParams.get("archived") === "true");
      await route.fulfill(fulfill({
        results: matches ? [{
          thread,
          match_count: matchingMessages.length,
          matches: matchingMessages.slice(0, 3).map((message) => ({
            message_id: message.id,
            role: message.role,
            snippet: message.content.replaceAll("\n", " ").slice(0, 180),
            created_at: message.created_at,
          })),
        }] : [],
        total: matches ? 1 : 0,
        limit: 20,
        offset: 0,
      }));
      return;
    }
    if (url.pathname === "/threads" && route.request().method() === "POST") {
      await route.fulfill(fulfill({ thread_id: "thread-new" }));
      return;
    }
    if (url.pathname === "/threads" && route.request().method() === "GET") {
      const wantsArchived = url.searchParams.get("archived") === "true";
      const query = url.searchParams.get("q")?.toLowerCase() ?? "";
      const matches = Boolean(thread.archived_at) === wantsArchived
        && (!query || thread.title.toLowerCase().includes(query));
      await route.fulfill(fulfill({
        threads: matches ? [thread] : [],
        total: matches ? 1 : 0,
        limit: 50,
        offset: 0,
      }));
      return;
    }
    if (
      url.pathname === "/threads/thread-1/organization"
      && route.request().method() === "PATCH"
    ) {
      const body = route.request().postDataJSON() as { pinned?: boolean; archived?: boolean };
      if (body.pinned !== undefined) {
        thread.pinned_at = body.pinned ? new Date().toISOString() : null;
      }
      if (body.archived !== undefined) {
        thread.archived_at = body.archived ? new Date().toISOString() : null;
      }
      await route.fulfill(fulfill(thread));
      return;
    }
    if (url.pathname === "/threads/thread-1/lineage") {
      await route.fulfill(fulfill({ thread, parent: null, children: [], siblings: [] }));
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
