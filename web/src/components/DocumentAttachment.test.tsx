import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { DocumentPart } from "../api/client";
import { DocumentAttachment } from "./DocumentAttachment";

const getAttachmentBlob = vi.fn<(
  threadId: string,
  attachmentId: string,
  signal?: AbortSignal,
) => Promise<Blob>>();
const createObjectUrl = vi.fn(() => "blob:pdf-preview");
const revokeObjectUrl = vi.fn();
const anchorClick = vi.fn(function (this: HTMLAnchorElement) {
  return { download: this.download, href: this.href };
});

vi.mock("../auth/auth-context", () => ({
  useAuth: () => ({ api: { getAttachmentBlob } }),
}));

const document: DocumentPart = {
  type: "document",
  mime_type: "application/pdf",
  attachment_id: "attachment-1",
  filename: "requirements.pdf",
};

beforeAll(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrl,
  });
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
  Object.defineProperty(HTMLAnchorElement.prototype, "click", {
    configurable: true,
    value: anchorClick,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DocumentAttachment", () => {
  it("loads lazily, previews once, reuses the blob for download, and cleans up", async () => {
    getAttachmentBlob.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));
    const view = render(<DocumentAttachment threadId="thread-1" document={document} />);
    const previewButton = screen.getByRole("button", { name: "Preview" });

    expect(getAttachmentBlob).not.toHaveBeenCalled();
    fireEvent.click(previewButton);

    const frame = await screen.findByTitle("Preview of requirements.pdf");
    expect(frame).toHaveAttribute("src", "blob:pdf-preview");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(getAttachmentBlob).toHaveBeenCalledTimes(1);
    expect(getAttachmentBlob).toHaveBeenCalledWith(
      "thread-1",
      "attachment-1",
      expect.any(AbortSignal),
    );

    fireEvent.click(screen.getAllByRole("button", { name: "Download" })[0]);
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(getAttachmentBlob).toHaveBeenCalledTimes(1);
    expect(anchorClick.mock.results[0].value).toEqual({
      download: "requirements.pdf",
      href: "blob:pdf-preview",
    });

    fireEvent.click(screen.getByRole("button", { name: "Close preview of requirements.pdf" }));
    await waitFor(() => expect(previewButton).toHaveFocus());

    view.unmount();
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:pdf-preview");
  });

  it("dismisses the preview through cancel and backdrop interactions", async () => {
    getAttachmentBlob.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));
    render(<DocumentAttachment threadId="thread-1" document={document} />);
    const previewButton = screen.getByRole("button", { name: "Preview" });

    fireEvent.click(previewButton);
    await screen.findByTitle("Preview of requirements.pdf");
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("open");

    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    await waitFor(() => expect(dialog).not.toHaveAttribute("open"));

    fireEvent.click(previewButton);
    await waitFor(() => expect(dialog).toHaveAttribute("open"));
    fireEvent.click(dialog);
    await waitFor(() => expect(dialog).not.toHaveAttribute("open"));
  });

  it("shows retrieval failures and retries the preview", async () => {
    getAttachmentBlob
      .mockRejectedValueOnce(new Error("unavailable"))
      .mockResolvedValueOnce(new Blob(["pdf"], { type: "application/pdf" }));
    render(<DocumentAttachment threadId="thread-1" document={document} />);

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("The PDF could not be loaded.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry preview" }));

    expect(await screen.findByTitle("Preview of requirements.pdf")).toHaveAttribute(
      "src",
      "blob:pdf-preview",
    );
    expect(getAttachmentBlob).toHaveBeenCalledTimes(2);
  });

  it("aborts an active retrieval when removed", async () => {
    let retrievalSignal: AbortSignal | undefined;
    getAttachmentBlob.mockImplementation((_threadId, _attachmentId, signal) => {
      retrievalSignal = signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    });
    const view = render(<DocumentAttachment threadId="thread-1" document={document} />);

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(getAttachmentBlob).toHaveBeenCalledTimes(1));
    view.unmount();

    expect(retrievalSignal?.aborted).toBe(true);
    expect(revokeObjectUrl).not.toHaveBeenCalled();
  });

  it("keeps multiple historical documents lazy and independent", async () => {
    getAttachmentBlob.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));
    render(
      <>
        <DocumentAttachment threadId="thread-1" document={document} />
        <DocumentAttachment
          threadId="thread-1"
          document={{ ...document, attachment_id: "attachment-2", filename: "notes.pdf" }}
        />
      </>,
    );

    expect(getAttachmentBlob).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "Preview" })[1]);

    await screen.findByTitle("Preview of notes.pdf");
    expect(getAttachmentBlob).toHaveBeenCalledTimes(1);
    expect(getAttachmentBlob).toHaveBeenCalledWith(
      "thread-1",
      "attachment-2",
      expect.any(AbortSignal),
    );
  });
});
