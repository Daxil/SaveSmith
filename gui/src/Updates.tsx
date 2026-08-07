/**
 * "There is a newer version."
 *
 * Three rules, all of them about not surprising somebody who is in the middle
 * of editing a save file:
 *
 * 1. **Nothing happens without being asked for.** The check is automatic; the
 *    download, the install and the restart are three separate consequences of
 *    one click. A program that rewrites save files and silently replaced
 *    itself between two edits would not be the program the user agreed to run.
 * 2. **A failed check is not news.** No network, an old release page, a
 *    development build with no updater configured at all — none of that is
 *    something to interrupt somebody with. It goes to the log.
 * 3. **The restart is the user's.** The new version is installed and then the
 *    banner says so; it does not close a window with unsaved changes in it.
 */

import { useEffect, useState } from "react";

import { note } from "./log";

/** What the updater told us, in the little of it this screen shows. */
interface Available {
  version: string;
  notes?: string;
  download(onEvent?: (progress: unknown) => void): Promise<void>;
  install(): Promise<void>;
}

type Stage = "idle" | "downloading" | "ready" | "failed";

export function Updates() {
  const [update, setUpdate] = useState<Available | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        // Imported here rather than at the top so that a browser opened at the
        // dev server — where there is no Tauri at all — still renders.
        const { check } = await import("@tauri-apps/plugin-updater");
        const found = await check();
        if (found && current) {
          note("info", `есть версия ${found.version}`);
          setUpdate(found as unknown as Available);
        }
      } catch (problem: unknown) {
        // Offline, or a build with no updater configured. Neither is news.
        note("info", "обновления не проверились:", problem);
      }
    })();
    return () => {
      current = false;
    };
  }, []);

  if (!update) return null;

  const install = async () => {
    setStage("downloading");
    setFailure(null);
    try {
      await update.download();
      await update.install();
      setStage("ready");
    } catch (problem: unknown) {
      setStage("failed");
      setFailure(problem instanceof Error ? problem.message : String(problem));
    }
  };

  const restart = async () => {
    const { relaunch } = await import("@tauri-apps/plugin-process");
    await relaunch();
  };

  return (
    <div className="update">
      {stage === "ready" ? (
        <>
          <span>
            Версия {update.version} установлена. Перезапусти, когда закончишь — правки,
            которые ты не записал, при перезапуске потеряются.
          </span>
          <button onClick={restart}>Перезапустить</button>
        </>
      ) : (
        <>
          <span>Вышла версия {update.version}.</span>
          <button disabled={stage === "downloading"} onClick={install}>
            {stage === "downloading" ? "Качаю…" : "Обновить"}
          </button>
        </>
      )}
      {failure && <span className="note">Не получилось: {failure}</span>}
    </div>
  );
}
