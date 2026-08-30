import type { ComponentProps } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { ArchiveTransferDialog } from "./ArchiveTransferDialog";

const mockApi = {
  exportThreadArchive: vi.fn(),
  exportThreadLineageArchive: vi.fn(),
  importThreadArchive: vi.fn(),
  importThreadLineageArchive: vi.fn(),
};

vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({ api: mockApi }),
}));

const createObjectUrl = vi.fn(() => "blob:archive-download");
const revokeObjectUrl = vi.fn();
const anchorClick = vi.fn();

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute("open");
    },
  });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrl,
  });
  Object.defineProperty(HTMLAnchorElement.prototype, "click", {
    configurable: true,
    value: anchorClick,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(
  properties: Partial<ComponentProps<typeof ArchiveTransferDialog>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onImported = vi.fn();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ArchiveTransferDialog
        open
        threadId="thread-1"
        threadTitle="Project plan"
        onClose={vi.fn()}
        onImported={onImported}
        {...properties}
      />
    </QueryClientProvider>,
  );
  return { ...result, onImported };
}

function archiveFile(name: string, payload: Record<string, unknown>): File {
  const file = new File([JSON.stringify(payload)], name, { type: "application/json" });
  Object.defineProperty(file, "text", {
    configurable: true,
    value: () => Promise.resolve(JSON.stringify(payload)),
  });
  return file;
}

describe("ArchiveTransferDialog", () => {
  it("downloads single-thread and lineage archives", async () => {
    mockApi.exportThreadArchive.mockResolvedValue({
      schema: "mindweft.thread-archive",
      archive_id: "archive-1",
    });
    mockApi.exportThreadLineageArchive.mockResolvedValue({
      schema: "mindweft.thread-lineage-archive",
      archive_id: "lineage-1",
    });
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Download this conversation" }));
    await waitFor(() => expect(mockApi.exportThreadArchive).toHaveBeenCalledWith("thread-1"));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Download complete lineage" }));
    await waitFor(() => expect(mockApi.exportThreadLineageArchive).toHaveBeenCalledWith("thread-1"));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(2));

    expect(createObjectUrl).toHaveBeenCalledTimes(2);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:archive-download");
  });

  it("dry-runs a thread archive with the selected destination policies", async () => {
    mockApi.importThreadArchive.mockResolvedValue({
      thread_id: null,
      message_count: 3,
      attachment_count: 1,
      dry_run: true,
      warnings: [{ code: "profile", message: "Destination defaults would be used." }],
    });
    const { onImported } = renderDialog({ threadId: null });
    const file = archiveFile("thread.mindweft.json", {
      schema: "mindweft.thread-archive",
      archive_id: "archive-1",
    });

    fireEvent.change(screen.getByLabelText("Archive file"), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("Execution profiles"), { target: { value: "strict" } });
    fireEvent.change(screen.getByLabelText("Organization"), { target: { value: "preserve" } });
    fireEvent.click(screen.getByLabelText(/Validate only/));
    fireEvent.submit(screen.getByRole("button", { name: "Validate archive" }).closest("form")!);

    expect(await screen.findByText("Archive validation passed.")).toBeInTheDocument();
    expect(screen.getByText("Destination defaults would be used.")).toBeInTheDocument();
    expect(mockApi.importThreadArchive).toHaveBeenCalledWith(
      expect.objectContaining({ schema: "mindweft.thread-archive" }),
      {
        profilePolicy: "strict",
        organizationPolicy: "preserve",
        timestampPolicy: "reset",
        dryRun: true,
      },
    );
    expect(onImported).not.toHaveBeenCalled();
  });

  it("imports a lineage archive and opens its requested conversation", async () => {
    const response = {
      requested_thread_id: "restored-requested",
      root_thread_id: "restored-root",
      thread_count: 2,
      message_count: 8,
      attachment_count: 2,
      dry_run: false,
      warnings: [],
    };
    mockApi.importThreadLineageArchive.mockResolvedValue(response);
    const { onImported } = renderDialog();
    const file = archiveFile("lineage.mindweft.json", {
      schema: "mindweft.thread-lineage-archive",
      archive_id: "lineage-1",
    });

    fireEvent.change(screen.getByLabelText("Archive file"), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("Timestamps"), { target: { value: "preserve" } });
    fireEvent.submit(screen.getByRole("button", { name: "Import archive" }).closest("form")!);

    expect(await screen.findByText("Archive imported.")).toBeInTheDocument();
    expect(screen.getByText(/2 conversations, 8 messages, 2 attachments/)).toBeInTheDocument();
    expect(mockApi.importThreadLineageArchive).toHaveBeenCalledWith(
      expect.objectContaining({ schema: "mindweft.thread-lineage-archive" }),
      {
        profilePolicy: "available",
        organizationPolicy: "reset",
        timestampPolicy: "preserve",
        dryRun: false,
      },
    );
    expect(onImported).toHaveBeenCalledWith("restored-requested", response);
  });

  it("rejects unsupported files before contacting the server", async () => {
    renderDialog();
    const file = archiveFile("other.json", { schema: "other.archive" });

    fireEvent.change(screen.getByLabelText("Archive file"), { target: { files: [file] } });
    fireEvent.submit(screen.getByRole("button", { name: "Import archive" }).closest("form")!);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unsupported archive schema: other.archive.",
    );
    expect(mockApi.importThreadArchive).not.toHaveBeenCalled();
    expect(mockApi.importThreadLineageArchive).not.toHaveBeenCalled();
  });
});
