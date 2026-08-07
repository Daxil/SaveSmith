/**
 * Somewhere for the window to write things down.
 *
 * A webview's console goes nowhere anybody can find. When this interface threw
 * during a render, React unmounted the whole tree and the user was left with a
 * black window — no message, no trace, nothing to report and nothing to fix
 * from. That is not a bug that should ever be diagnosed by guessing twice.
 *
 * So: every uncaught error and every rejected promise goes to the shell, which
 * prints it beside the backend's own output. In a browser tab it falls back to
 * the developer console, which there is a real place.
 */

import { packaged } from "./rpc";

type Level = "info" | "error";

export function note(level: Level, ...parts: unknown[]): void {
  const message = parts.map(describe).join(" ");
  if (!packaged()) {
    (level === "error" ? console.error : console.log)("[savesmith]", message);
    return;
  }
  void import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke("log", { level, message }))
    // Losing a log line must never be what breaks the window.
    .catch(() => undefined);
}

function describe(value: unknown): string {
  if (value instanceof Error) {
    return `${value.name}: ${value.message}\n${value.stack ?? ""}`;
  }
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Catch what nobody caught, once, on the way in. */
export function reportEverythingUncaught(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("error", (event) => {
    note("error", "uncaught", event.error ?? event.message);
  });
  window.addEventListener("unhandledrejection", (event) => {
    note("error", "unhandled rejection", event.reason);
  });
}
