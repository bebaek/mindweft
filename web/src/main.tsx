import { OAuthReturnBanner } from "./components/OAuthReturnBanner";
import { captureOAuthReturn } from "./auth/oauthReturn";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { bootstrapLocalConnection } from "./auth/localConnection";
import { AuthProvider } from "./auth/AuthProvider";
import "@radix-ui/colors/sage.css";
import "@radix-ui/colors/sage-dark.css";
import "@radix-ui/colors/green.css";
import "@radix-ui/colors/green-dark.css";
import "@radix-ui/colors/green-alpha.css";
import "@radix-ui/colors/green-dark-alpha.css";
import "@radix-ui/colors/red.css";
import "@radix-ui/colors/red-dark.css";
import "@radix-ui/colors/amber.css";
import "@radix-ui/colors/amber-dark.css";
import "@radix-ui/colors/lime.css";
import "@radix-ui/colors/lime-dark.css";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 15_000,
      refetchOnWindowFocus: true,
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("Application root was not found");

async function start() {
  try {
    captureOAuthReturn();
    await bootstrapLocalConnection();
  } catch (error) {
    root!.textContent = error instanceof Error ? error.message : "Unable to connect. Reload to retry.";
    return;
  }
  createRoot(root!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <AuthProvider><OAuthReturnBanner /><App /></AuthProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}
void start();
