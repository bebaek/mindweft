import { describe, expect, it } from "vitest";
import type { AudioInputConfig, DocumentInputConfig, ImageInputConfig } from "../api/client";
import {
  classifyAttachmentFiles,
  unsupportedAttachmentMessage,
  validateQueueAddition,
} from "./workspaceAttachments";

const audioConfig: AudioInputConfig = {
  enabled: true,
  max_bytes: 20,
  max_audio_files: 2,
  max_total_bytes: 30,
  max_duration_seconds: 60,
  allowed_mime_types: ["audio/wav"],
};

const imageConfig: ImageInputConfig = {
  enabled: true,
  max_bytes: 10,
  max_images: 2,
  max_total_bytes: 15,
  max_pixels: 1_000,
  max_dimension: 100,
  allowed_mime_types: ["image/png"],
};

const documentConfig: DocumentInputConfig = {
  enabled: true,
  max_bytes: 20,
  max_documents: 2,
  max_total_bytes: 30,
  max_pages: 100,
  max_text_bytes: 10,
  allowed_mime_types: ["application/pdf", "text/plain"],
};

function file(name: string, type: string, size = 1): File {
  return new File([new Uint8Array(size)], name, { type });
}

describe("classifyAttachmentFiles", () => {
  it("classifies a mixed image and PDF batch", () => {
    const image = file("diagram.png", "image/png");
    const document = file("requirements.pdf", "application/pdf");

    const result = classifyAttachmentFiles(
      [image, document],
      imageConfig,
      documentConfig,
    );

    expect(result.images).toEqual([image]);
    expect(result.documents).toEqual([document]);
    expect(result.unsupported).toEqual([]);
  });

  it("infers the PDF MIME type from a case-insensitive extension", () => {
    const result = classifyAttachmentFiles(
      [file("SPEC.PDF", "")],
      imageConfig,
      documentConfig,
    );

    expect(result.documents).toHaveLength(1);
    expect(result.documents[0].type).toBe("application/pdf");
    expect(result.documents[0].name).toBe("SPEC.PDF");
  });

  it("canonicalizes supported text document extensions and MIME aliases", () => {
    const result = classifyAttachmentFiles(
      [
        file("notes.md", "text/markdown"),
        file("report.csv", "text/csv"),
        file("debug.log", ""),
      ],
      imageConfig,
      documentConfig,
    );

    expect(result.documents.map((document) => [document.name, document.type])).toEqual([
      ["notes.md", "text/plain"],
      ["report.csv", "text/plain"],
      ["debug.log", "text/plain"],
    ]);
    expect(result.unsupported).toEqual([]);
  });

  it("canonicalizes WAV aliases and extension-only files", () => {
    const result = classifyAttachmentFiles(
      [file("voice.wav", "audio/x-wav"), file("meeting.WAV", "")],
      imageConfig,
      documentConfig,
      audioConfig,
    );

    expect(result.audio.map((audio) => [audio.name, audio.type])).toEqual([
      ["voice.wav", "audio/wav"],
      ["meeting.WAV", "audio/wav"],
    ]);
    expect(result.unsupported).toEqual([]);
  });

  it("reports files that match neither configured modality", () => {
    const archive = file("bundle.zip", "application/zip");
    const result = classifyAttachmentFiles([archive], imageConfig, documentConfig);

    expect(result.unsupported).toEqual([archive]);
    expect(unsupportedAttachmentMessage(result.unsupported)).toBe(
      "Unsupported attachment: bundle.zip.",
    );
  });
});

describe("validateQueueAddition", () => {
  const labels = { singular: "image", plural: "images" };
  const limits = {
    allowed_mime_types: imageConfig.allowed_mime_types,
    max_bytes: imageConfig.max_bytes,
    max_items: imageConfig.max_images,
    max_total_bytes: imageConfig.max_total_bytes,
  };

  it("enforces count limits across queued and incoming files", () => {
    expect(validateQueueAddition(
      [file("one.png", "image/png")],
      [file("two.png", "image/png"), file("three.png", "image/png")],
      limits,
      labels,
    )).toBe("A message can include at most 2 images.");
  });

  it("enforces aggregate and per-file byte limits", () => {
    expect(validateQueueAddition(
      [file("one.png", "image/png", 7)],
      [file("two.png", "image/png", 9)],
      limits,
      labels,
    )).toBe("Selected images exceed the total message size limit.");

    expect(validateQueueAddition(
      [],
      [file("large.png", "image/png", 11)],
      limits,
      labels,
    )).toBe("large.png exceeds the per-image size limit.");
  });

  it("accepts a batch within every configured limit", () => {
    expect(validateQueueAddition(
      [file("one.png", "image/png", 5)],
      [file("two.png", "image/png", 5)],
      limits,
      labels,
    )).toBeNull();
  });
});
