import { afterEach, expect, test } from "vitest";
import { captureOAuthReturn, clearOAuthReturn, pendingOAuthReturn, oauthReturnError } from "./oauthReturn";

afterEach(() => { clearOAuthReturn(); window.history.replaceState(null, "", "/"); });
test("removes callback secrets before requests and keeps them in memory only", () => {
  window.history.replaceState(null, "", "/console/#oauth_state=state&oauth_code=code&local_ticket=ticket");
  captureOAuthReturn();
  expect(pendingOAuthReturn).toEqual({ state: "state", code: "code" });
  expect(window.location.hash).toBe("#local_ticket=ticket");
  expect(window.sessionStorage.length).toBe(0);
  expect(window.localStorage.length).toBe(0);
});
test("does not expose provider error text", () => {
  window.history.replaceState(null, "", "/console/#oauth_error=private-provider-detail");
  captureOAuthReturn();
  expect(window.location.hash).toBe("");
  expect(pendingOAuthReturn).toBeNull();
  expect(oauthReturnError).not.toContain("private-provider-detail");
});
