import type { DocumentInputConfig, ImageInputConfig } from "../api/client";

export interface ClassifiedAttachmentFiles {
  documents: File[];
  images: File[];
  unsupported: File[];
}

interface QueueLimitConfig {
  allowed_mime_types: string[];
  max_bytes: number;
  max_total_bytes: number;
}

function normalizedMimeTypes(mimeTypes: string[]): Set<string> {
  return new Set(mimeTypes.map((mimeType) => mimeType.toLowerCase()));
}

const textDocumentExtensions = [".txt", ".md", ".csv", ".log"];
const textDocumentMimeTypes = new Set(["text/plain", "text/markdown", "text/csv"]);

function withMimeType(file: File, type: string): File {
  if (file.type.toLowerCase() === type) return file;
  return new File([file], file.name, { lastModified: file.lastModified, type });
}

function normalizeDocumentMimeType(file: File): File {
  const name = file.name.toLowerCase();
  const mimeType = file.type.toLowerCase();
  if (
    name.endsWith(".pdf") &&
    (!mimeType || mimeType === "application/pdf" || textDocumentMimeTypes.has(mimeType))
  ) {
    return withMimeType(file, "application/pdf");
  }
  if (
    textDocumentMimeTypes.has(mimeType) ||
    textDocumentExtensions.some((extension) => name.endsWith(extension))
  ) {
    return withMimeType(file, "text/plain");
  }
  return file;
}

export function classifyAttachmentFiles(
  files: Iterable<File>,
  imageConfig: ImageInputConfig | undefined,
  documentConfig: DocumentInputConfig | undefined,
): ClassifiedAttachmentFiles {
  const imageMimeTypes = normalizedMimeTypes(imageConfig?.allowed_mime_types ?? []);
  const documentMimeTypes = normalizedMimeTypes(documentConfig?.allowed_mime_types ?? []);
  const classified: ClassifiedAttachmentFiles = { documents: [], images: [], unsupported: [] };

  for (const original of files) {
    const file = normalizeDocumentMimeType(original);
    const mimeType = file.type.toLowerCase();
    if (imageMimeTypes.has(mimeType)) {
      classified.images.push(file);
    } else if (documentMimeTypes.has(mimeType)) {
      classified.documents.push(file);
    } else {
      classified.unsupported.push(file);
    }
  }
  return classified;
}

export function validateQueueAddition(
  existing: File[],
  candidates: File[],
  config: QueueLimitConfig & { max_items: number },
  labels: { singular: string; plural: string },
): string | null {
  if (existing.length + candidates.length > config.max_items) {
    return `A message can include at most ${String(config.max_items)} ${labels.plural}.`;
  }
  const totalBytes = [...existing, ...candidates].reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > config.max_total_bytes) {
    return `Selected ${labels.plural} exceed the total message size limit.`;
  }
  const allowedMimeTypes = normalizedMimeTypes(config.allowed_mime_types);
  for (const file of candidates) {
    if (!allowedMimeTypes.has(file.type.toLowerCase())) {
      return `Unsupported ${labels.singular} type: ${file.type || file.name}`;
    }
    if (file.size > config.max_bytes) {
      return `${file.name} exceeds the per-${labels.singular} size limit.`;
    }
  }
  return null;
}

export function unsupportedAttachmentMessage(files: File[]): string {
  const names = files.map((file) => file.name || file.type || "unnamed file");
  if (names.length === 1) return `Unsupported attachment: ${names[0]}.`;
  return `Unsupported attachments: ${names.join(", ")}.`;
}
