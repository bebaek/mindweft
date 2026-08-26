import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ExecutionOptionsResponse } from "../api/client";
import { WorkspacePage } from "./WorkspacePage";

const createObjectUrl = vi.fn(() => "blob:preview");
const revokeObjectUrl = vi.fn();

const mockApi = {
  getPublicConfig: vi.fn(() => Promise.resolve({
    document_input: {
      enabled: true,
      max_bytes: 2_000,
      max_documents: 3,
      max_total_bytes: 4_000,
      max_pages: 100,
      allowed_mime_types: ["application/pdf"],
    },
    image_input: {
      enabled: true,
      max_bytes: 2_000,
      max_images: 3,
      max_total_bytes: 4_000,
      max_pixels: 10_000,
      max_dimension: 1_000,
      allowed_mime_types: ["image/png"],
    },
  })),
  getExecutionOptions: vi.fn<() => Promise<ExecutionOptionsResponse>>(() => Promise.resolve({
    tenant_id: "tenant-1",
    skills: { items: [] },
    capability_profiles: { items: [] },
    llm_profiles: {
      items: [],
      effective_default: {
        name: "default",
        image_input_allowed: true,
        document_input_allowed: true,
        capability_declared: true,
      },
    },
    agents: { items: [] },
  })),
  listThreads: vi.fn(() => Promise.resolve({
    threads: [],
    total: 0,
    limit: 50,
    offset: 0,
  })),
};

vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({
    api: mockApi,
    authentication: { mode: "development" },
  }),
}));

beforeAll(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrl,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderWorkspace() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkspacePage />
    </QueryClientProvider>,
  );
}

async function readyComposer(): Promise<HTMLFormElement> {
  const input = await screen.findByRole("textbox", { name: /Message/ });
  await waitFor(() => expect(screen.getByTitle("Attach images")).toBeInTheDocument());
  const form = input.closest("form");
  if (!(form instanceof HTMLFormElement)) throw new Error("Composer form was not rendered");
  return form;
}

describe("workspace attachment intake", () => {
  it("queues a mixed image and PDF drop and shows accessible drag feedback", async () => {
    renderWorkspace();
    const composer = await readyComposer();
    const image = new File(["png"], "diagram.png", { type: "image/png" });
    const document = new File(["pdf"], "requirements.pdf", { type: "application/pdf" });
    const transfer = { types: ["Files"], files: [image, document], dropEffect: "none" };

    fireEvent.dragEnter(composer, { dataTransfer: transfer });
    expect(screen.getByRole("status")).toHaveTextContent("Drop images or PDFs to attach them");

    fireEvent.drop(composer, { dataTransfer: transfer });

    expect(await screen.findByAltText("diagram.png")).toBeInTheDocument();
    expect(screen.getByText("requirements.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Drop images or PDFs to attach them")).not.toBeInTheDocument();
  });

  it("queues a PDF supplied as a clipboard file", async () => {
    renderWorkspace();
    await readyComposer();
    const input = screen.getByRole("textbox", { name: /Message/ });
    const document = new File(["pdf"], "clipboard.pdf", { type: "application/pdf" });

    fireEvent.paste(input, {
      clipboardData: { files: [document], items: [] },
    });

    expect(await screen.findByText("clipboard.pdf")).toBeInTheDocument();
  });

  it("blocks a document when the effective profile does not accept documents", async () => {
    mockApi.getExecutionOptions.mockResolvedValueOnce({
      tenant_id: "tenant-1",
      skills: { items: [] },
      capability_profiles: { items: [] },
      llm_profiles: {
        items: [],
        effective_default: {
          name: "text-only",
          image_input_allowed: true,
          document_input_allowed: false,
          document_input_reason: "profile_unsupported" as const,
          capability_declared: true,
        },
      },
      agents: { items: [] },
    });
    renderWorkspace();
    const composer = await readyComposer();
    const document = new File(["pdf"], "blocked.pdf", { type: "application/pdf" });

    fireEvent.drop(composer, {
      dataTransfer: { types: ["Files"], files: [document], dropEffect: "none" },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The selected model profile does not accept documents.",
    );
    expect(screen.queryByText("blocked.pdf")).not.toBeInTheDocument();
  });

  it("releases image preview URLs when a queued image is removed", async () => {
    renderWorkspace();
    const composer = await readyComposer();
    const image = new File(["png"], "temporary.png", { type: "image/png" });

    fireEvent.drop(composer, {
      dataTransfer: { types: ["Files"], files: [image], dropEffect: "none" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Remove temporary.png" }));

    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:preview");
    expect(screen.queryByAltText("temporary.png")).not.toBeInTheDocument();
  });

  it("queues supported files while reporting unsupported files in the same drop", async () => {
    renderWorkspace();
    const composer = await readyComposer();
    const image = new File(["png"], "diagram.png", { type: "image/png" });
    const archive = new File(["zip"], "bundle.zip", { type: "application/zip" });

    fireEvent.drop(composer, {
      dataTransfer: { types: ["Files"], files: [image, archive], dropEffect: "none" },
    });

    expect(await screen.findByAltText("diagram.png")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Unsupported attachment: bundle.zip.");
  });
});
