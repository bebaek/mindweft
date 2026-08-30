import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ThreadDeleteDialog } from "./ThreadDeleteDialog";

const mockApi = {
  getImportedThreadLineage: vi.fn(),
  deleteImportedThreadLineage: vi.fn(),
  deleteThread: vi.fn(),
};

vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({
    api: mockApi,
    authentication: { mode: "development" },
  }),
}));

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
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onDeleted = vi.fn();
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <ThreadDeleteDialog
        open
        threadId="thread-1"
        threadTitle="Deployment review"
        onClose={onClose}
        onDeleted={onDeleted}
      />
    </QueryClientProvider>,
  );
  return { onDeleted, onClose };
}

describe("ThreadDeleteDialog", () => {
  it("confirms and deletes a standalone conversation", async () => {
    mockApi.getImportedThreadLineage.mockRejectedValue(
      new ApiError("not an imported lineage", 404, { detail: "not found" }),
    );
    mockApi.deleteThread.mockResolvedValue(undefined);
    const { onDeleted } = renderDialog();

    const confirm = screen.getByRole("button", { name: "Delete conversation" });
    await waitFor(() => expect(confirm).toBeEnabled());
    expect(screen.getByText(/Deployment review/)).toBeInTheDocument();
    fireEvent.click(confirm);

    await waitFor(() => expect(mockApi.deleteThread).toHaveBeenCalledWith("thread-1"));
    expect(mockApi.deleteImportedThreadLineage).not.toHaveBeenCalled();
    expect(onDeleted).toHaveBeenCalledWith({
      deletedCount: 1,
      deletedThreadIds: ["thread-1"],
      lineage: false,
    });
  });

  it("requires deleting a complete multi-thread imported lineage", async () => {
    mockApi.getImportedThreadLineage.mockResolvedValue({
      archive_id: "lineage-1",
      requested_thread_id: "thread-1",
      thread_count: 3,
      message_count: 9,
      attachment_count: 2,
      dry_run: false,
      warnings: [],
    });
    mockApi.deleteImportedThreadLineage.mockResolvedValue({
      deleted_thread_ids: ["thread-1", "thread-2", "thread-3"],
      deleted_count: 3,
    });
    const { onDeleted } = renderDialog();

    const confirm = await screen.findByRole("button", { name: "Delete all 3 conversations" });
    expect(screen.getByRole("heading", { name: "Delete imported lineage?" })).toBeInTheDocument();
    expect(screen.getByText(/requires deleting the complete imported lineage together/)).toBeInTheDocument();
    fireEvent.click(confirm);

    await waitFor(() => expect(mockApi.deleteImportedThreadLineage).toHaveBeenCalledWith("thread-1"));
    expect(mockApi.deleteThread).not.toHaveBeenCalled();
    expect(onDeleted).toHaveBeenCalledWith({
      deletedCount: 3,
      deletedThreadIds: ["thread-1", "thread-2", "thread-3"],
      lineage: true,
    });
  });

  it("blocks deletion when its scope cannot be determined", async () => {
    mockApi.getImportedThreadLineage.mockRejectedValue(
      new ApiError("server unavailable", 503, { detail: "unavailable" }),
    );
    renderDialog();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not determine the deletion scope. Nothing has been deleted.",
    );
    expect(screen.getByRole("button", { name: "Delete conversation" })).toBeDisabled();
    expect(mockApi.deleteThread).not.toHaveBeenCalled();
  });

  it("keeps the dialog open and reports a deletion conflict", async () => {
    mockApi.getImportedThreadLineage.mockRejectedValue(
      new ApiError("not an imported lineage", 404, { detail: "not found" }),
    );
    mockApi.deleteThread.mockRejectedValue(
      new ApiError("Conversation belongs to an imported lineage", 409, { detail: "conflict" }),
    );
    const { onDeleted } = renderDialog();

    const confirm = screen.getByRole("button", { name: "Delete conversation" });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Conversation belongs to an imported lineage",
    );
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
