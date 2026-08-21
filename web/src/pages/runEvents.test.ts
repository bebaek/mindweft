import { describe, expect, it } from "vitest";
import { runErrorMessage } from "./runEvents";

describe("runErrorMessage", () => {
  it("extracts the user-facing message from a structured provider error", () => {
    expect(
      runErrorMessage({
        type: "provider_auth_failed",
        message: "Generic OAuth authentication failed. Check provider credentials.",
        provider: "generic-oauth",
      }),
    ).toBe("Generic OAuth authentication failed. Check provider credentials.");
  });

  it("preserves string error details", () => {
    expect(runErrorMessage("Backend unavailable")).toBe("Backend unavailable");
  });

  it("uses a safe fallback for malformed details", () => {
    expect(runErrorMessage({ type: "provider_auth_failed" })).toBe("The run failed");
    expect(runErrorMessage(null, "Run failed")).toBe("Run failed");
  });
});
