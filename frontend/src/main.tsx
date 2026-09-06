import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installGlobalErrorHandlers, logger } from "./lib/logger";
import { registerSciStudioToolsWithRetry, subscribeToProjectChanges } from "./webmcp/register";
import "./index.css";

// #1741: install global error handlers + wrap the app in an ErrorBoundary so
// frontend crashes/rejections are logged and refluxed to the backend instead of
// disappearing into the DevTools console no beta tester opens.
installGlobalErrorHandlers();
logger.info("app starting");

// ADR-055 Spec 1 (FR-010): expose SciStudio's tools to a browser AI agent via
// WebMCP. Fire-and-forget on purpose — registration must never block app boot,
// the app is fully usable without it, and on a browser without the capability
// this resolves to 0 registrations and says so.
void registerSciStudioToolsWithRetry();
// Re-register on active-project change so the bridge's project snapshot stays
// current (PR #2275 review P1); non-blocking store subscription.
subscribeToProjectChanges();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
