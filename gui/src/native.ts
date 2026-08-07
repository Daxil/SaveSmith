/**
 * The two things only the packaged app can do.
 *
 * Both are loaded on demand, so the browser build never pulls in code it cannot
 * run — the interface has to stay openable in a plain tab, or it stops being
 * possible to work on it without Rust installed.
 */

import { packaged } from "./rpc";

/**
 * The native folder picker.
 *
 * Typing a path is the step where people give up: on Windows it is long and
 * full of backslashes, and there is no way to know whether it was typed right
 * until something fails. Returns null when the dialog is dismissed.
 */
export async function browseForFolder(): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({
    directory: true,
    multiple: false,
    title: "Папка, куда установлена игра",
  });
  return typeof picked === "string" ? picked : null;
}

/**
 * The game itself: an ``.exe`` on Windows, an application on a Mac.
 *
 * A separate dialog from the folder one because a native open panel is either
 * choosing files or choosing folders, and on macOS an application is a folder
 * that the panel insists on treating as a file. Any file will do, in fact — a
 * save file pointed at directly is understood as well.
 */
export async function browseForGame(): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const windows = typeof navigator !== "undefined" && /win/i.test(navigator.userAgent);
  const picked = await open({
    multiple: false,
    title: windows ? "Программа игры (.exe)" : "Игра",
    filters: windows ? [{ name: "Программа", extensions: ["exe"] }] : undefined,
  });
  return typeof picked === "string" ? picked : null;
}

/**
 * A folder dragged onto the window.
 *
 * The HTML drop event cannot be used for this: a webview is given the file's
 * name and never its path, on purpose. The shell reports the real one, which is
 * the only form the backend can do anything with.
 */
export async function onFolderDropped(
  handle: (path: string) => void,
): Promise<() => void> {
  if (!packaged()) return () => {};
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  return await getCurrentWebview().onDragDropEvent((event) => {
    if (event.payload.type !== "drop") return;
    // Several at once is a mis-drop, not a request to open several games.
    const [first] = event.payload.paths;
    if (first) handle(first);
  });
}

/**
 * Progress from long calls — `scan`, `discover`.
 *
 * The shell forwards the backend's notifications as an event. Nothing depends
 * on them arriving, so in a browser this quietly subscribes to nothing.
 */
export async function onProgress(
  listen: (payload: unknown) => void,
): Promise<() => void> {
  if (!packaged()) return () => {};
  const { listen: subscribe } = await import("@tauri-apps/api/event");
  return await subscribe("progress", (event) => listen(event.payload));
}
