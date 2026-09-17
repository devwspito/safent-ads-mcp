import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/api/queryClient";
import { App } from "@/App";
import "@/styles/tokens.css";
import "@/styles/global.css";

const mocksEnabled = import.meta.env.VITE_API_MOCK === "1" || (import.meta.env.DEV && import.meta.env.VITE_API_MOCK !== "0");

async function enableMocking() {
  if (!mocksEnabled) return;
  const { worker } = await import("@/mocks/browser");
  await worker.start({ onUnhandledRequest: "bypass" });
}

void enableMocking().then(() => {
  createRoot(document.getElementById("root") as HTMLElement).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
});
