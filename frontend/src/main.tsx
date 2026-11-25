import { Component, StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 3,
      retryDelay: (attempt) => Math.min(1500 * 2 ** attempt, 8000),
    },
  },
});

class AppErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error(error);
  }

  render() {
    if (this.state.failed) {
      return (
        <div style={{
          display: "flex", height: "100vh", alignItems: "center", justifyContent: "center",
          flexDirection: "column", gap: 10, color: "#6b6b6b", fontFamily: "system-ui, sans-serif",
        }}>
          <p style={{ margin: 0, fontSize: 14 }}>界面加载失败，请重启应用</p>
          <button
            onClick={() => window.location.reload()}
            style={{ border: "1px solid #e5e5e5", borderRadius: 8, background: "#fff", padding: "6px 12px", cursor: "pointer" }}
          >
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppErrorBoundary>
          <App />
        </AppErrorBoundary>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>
);
